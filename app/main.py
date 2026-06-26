import os
import re
import math
import time
import logging
from threading import Lock

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizableTextQuery, QueryType
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

# ── Non-sensitive config — stays in .env ──────────────────────────
SEARCH_ENDPOINT       = os.environ["SEARCH_ENDPOINT"]
SEARCH_INDEX          = os.environ["SEARCH_INDEX"]
SEMANTIC_CONFIG_NAME  = os.environ["SEMANTIC_CONFIG_NAME"]
SPREAD_THRESHOLD      = float(os.environ.get("SPREAD_THRESHOLD", "0.4"))

# ── Score bands on the 0–4 semantic re-ranker scale ───────────────
# These two thresholds define the routing of get_kb_candidates. Every
# response carries `followup_required` and `top_score`, so the agent always
# knows what to do. There are three outcomes:
#
#   top_score > CONFIDENT_SCORE  AND  compute_spread == 'low' (clear winner)
#       -> RESOLVE     : followup_required=False, kb_id, top_score, message
#                        (confident, unambiguous match — present it, no follow-up)
#
#   top_score > CONFIDENT_SCORE  AND  compute_spread == 'high' (near-tie,
#                                     top-two gap < SPREAD_THRESHOLD)
#       -> falls through to the follow-up band below (ambiguous: two strong,
#          near-equal articles — ask a disambiguating question, don't guess)
#
#   FOLLOWUP_FLOOR <= top_score <= CONFIDENT_SCORE  AND symptoms found
#       -> ASK SYMPTOMS: followup_required=True, top_score, discriminating_symptoms
#
#   sub-confident AND no usable symptoms  (valid match w/ no symptom metadata,
#                                          OR top_score below the floor)
#       -> ASK FREEFORM: followup_required=True, top_score,
#                        discriminating_symptoms=[], message
#                        (agent asks its OWN Outlook-scoped follow-up; it can
#                         read top_score to gauge off-topic vs valid-but-bare)
#
# The 3-round off-topic close is enforced in the agent prompt, not here.
# Tune the thresholds in .env during testing.
CONFIDENT_SCORE       = float(os.environ.get("CONFIDENT_SCORE", "3.3"))   # final-answer threshold
FOLLOWUP_FLOOR        = float(os.environ.get("FOLLOWUP_FLOOR", "0.9"))    # below = no symptom selection

# Agent-facing messages (env-overridable). RESOLVE_MESSAGE explicitly tells the
# agent no follow-up is needed; NO_SYMPTOMS_MESSAGE tells it to ask its own.
RESOLVE_MESSAGE       = os.environ.get(
    "RESOLVE_MESSAGE",
    "Confident match. Present this article to the user — no follow-up needed.",
)
NO_SYMPTOMS_MESSAGE   = os.environ.get(
    "NO_SYMPTOMS_MESSAGE",
    "No discriminating symptoms are available. Ask one focused follow-up "
    "question based on the user's own description, staying strictly on "
    "Outlook issues.",
)

TOP_K                 = int(os.environ.get("TOP_K", "10"))
RETURN_K              = int(os.environ.get("RETURN_K", "3"))
TOP_SYMPTOMS_K        = int(os.environ.get("TOP_SYMPTOMS_K", "3"))   # max discriminating symptoms returned

# Relevance floor for a single symptom's combined score (final ∈ [0, 1],
# where final = 0.5·rel + 0.5·dist). A symptom scoring below this is treated
# as not relevant enough to ask about and is dropped. If every symptom is
# dropped, select_discriminating_symptoms returns [] -> the endpoint takes the
# ASK FREEFORM path (agent asks its own follow-up) rather than surfacing a
# weak/irrelevant symptom. Default 0.5: with rel=0 (no lexical overlap with the
# user's words) final maxes at 0.5, so this keeps symptoms only when relevance
# and discriminative power together clear half. Lower it (~0.4) if 0.5 proves
# too strict on short queries; raise it to demand stronger matches.
MIN_SYMPTOM_SCORE     = float(os.environ.get("MIN_SYMPTOM_SCORE", "0.5"))

# ── Round cap (conclude instead of looping forever) ───────────────
# A conversation may take at most MAX_ROUNDS follow-up calls. Counting needs a
# stable `session_id` on every request (see CandidateRequest). On the call
# whose round count reaches MAX_ROUNDS, the tool stops asking and concludes:
#   - top_score >= MIN_DISPLAY_SCORE  -> present the best candidate (no follow-up),
#                                        even though it never cleared CONFIDENT_SCORE
#   - top_score <  MIN_DISPLAY_SCORE  -> conclude with no match (agent closes/hands off)
# A confident match (> CONFIDENT_SCORE) still resolves on any round, before the cap.
# MIN_DISPLAY_SCORE sits between FOLLOWUP_FLOOR and CONFIDENT_SCORE (0.9 < 2.5 < 3.3).
MAX_ROUNDS            = int(os.environ.get("MAX_ROUNDS", "7"))
MIN_DISPLAY_SCORE     = float(os.environ.get("MIN_DISPLAY_SCORE", "2.5"))
SESSION_TTL_SECONDS   = int(os.environ.get("SESSION_TTL_SECONDS", "3600"))  # prune idle sessions after 1h

DISPLAY_AT_CAP_MESSAGE = os.environ.get(
    "DISPLAY_AT_CAP_MESSAGE",
    "Reached the follow-up limit. Present this best-available match to the "
    "user — no further follow-up.",
)
CONCLUDED_NO_MATCH_MESSAGE = os.environ.get(
    "CONCLUDED_NO_MATCH_MESSAGE",
    "Reached the follow-up limit with no sufficiently confident match. Stop "
    "asking and close or hand off per policy.",
)

# ── Sensitive API keys — loaded from Azure Key Vault ──────────────
try:
    KV_URL        = os.environ["AZURE_KEY_VAULT_URL"]
    credential    = DefaultAzureCredential()
    kv_client     = SecretClient(vault_url=KV_URL, credential=credential)

    SEARCH_API_KEY = kv_client.get_secret("search-api-key").value
    TOOL_API_KEY   = kv_client.get_secret("tool-api-key").value

    print("✅ API keys successfully loaded from Key Vault")

except KeyError as e:
    raise RuntimeError(
        f"❌ Missing environment variable: {e}. "
        "Make sure AZURE_KEY_VAULT_URL is set in .env"
    )
except Exception as e:
    raise RuntimeError(
        f"❌ Failed to load secrets from Key Vault: {type(e).__name__}: {e}\n"
        "  → Local dev: run 'az login' in terminal\n"
        "  → Azure hosted: enable Managed Identity and assign Key Vault Secrets User role"
    )

# ── Logging setup ─────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("kb-tool")

# ── Azure Search client ───────────────────────────────────────────
search = SearchClient(SEARCH_ENDPOINT, SEARCH_INDEX, AzureKeyCredential(SEARCH_API_KEY))

app = FastAPI(title="KB Candidates Tool")


# ── Request model ─────────────────────────────────────────────────
class CandidateRequest(BaseModel):
    description: str
    # Stable conversation/thread id. REQUIRED for the round cap to work. If the
    # agent omits it, rounds can't be counted and the tool never force-concludes
    # (it just keeps returning follow-ups until a confident match appears).
    session_id: str | None = None


# ── Round tracking (in-memory, per-instance) ──────────────────────
# Counts follow-up calls per session so the tool can conclude after MAX_ROUNDS.
# NOTE: in-memory state is per-process — correct on a single App Service
# instance (vinnyclasifiervm). If you scale out to 2+ instances, move this to
# Redis / Azure Table storage or the counter will split across instances.
_rounds_lock = Lock()
_round_state: dict[str, dict] = {}   # session_id -> {"rounds": int, "ts": float}


def _bump_round(session_id: str | None) -> int:
    """Increment and return this session's round count. Returns 1 (never caps)
    when no session_id is supplied, since rounds can't be tracked."""
    if not session_id:
        logger.warning("No session_id — round cap cannot be enforced this call.")
        return 1
    now = time.monotonic()
    with _rounds_lock:
        stale = [s for s, st in _round_state.items()
                 if now - st["ts"] > SESSION_TTL_SECONDS]
        for s in stale:
            _round_state.pop(s, None)
        st = _round_state.setdefault(session_id, {"rounds": 0, "ts": now})
        st["rounds"] += 1
        st["ts"] = now
        return st["rounds"]


def _clear_round(session_id: str | None) -> None:
    """Drop a session's counter once the conversation concludes (resolved or capped)."""
    if not session_id:
        return
    with _rounds_lock:
        _round_state.pop(session_id, None)


# ── Helpers ───────────────────────────────────────────────────────
def compute_spread(scores: list[float]) -> str:
    """
    Spread gate. Returns 'high' when there is NO clear, strong winner and
    'low' when there is. 'high' triggers on either condition:
      - weak_absolute : top score is below CONFIDENT_SCORE (not confident), OR
      - weak_dominance: top beats #2 by less than SPREAD_THRESHOLD (near-tie).
    Used in two places:
      - /get_kb_candidates RESOLVE gate — only resolve when spread is 'low'
        (confident AND a clear winner). A confident near-tie (e.g. 3.50 vs 3.53)
        yields 'high' via weak_dominance, blocking RESOLVE so the call falls
        through to a disambiguating follow-up.
      - /debug_search — reported as `spread_on_returned`.
    `scores` must be sorted desc (deduped distinct-article scores).
    """
    if not scores:
        return "low"
    weak_absolute  = scores[0] < CONFIDENT_SCORE
    weak_dominance = len(scores) > 1 and (scores[0] - scores[1]) < SPREAD_THRESHOLD
    if weak_absolute or weak_dominance:
        return "high"
    return "low"


def dedupe_by_kb(rows):
    """Keep only the best-scoring chunk per kb_id, so the agent sees
    distinct KB articles (not multiple chunks of the same article)."""
    best = {}
    for row in rows:
        kb_id = row.get("kb_id", "")
        if kb_id not in best or row["score"] > best[kb_id]["score"]:
            best[kb_id] = row
    return sorted(best.values(), key=lambda c: c["score"], reverse=True)


# ── Symptom selection: rank by discriminating power ───────────────
# When in the follow-up band we pre-select the 2-3 symptoms that best split
# the candidate set, so the agent asks targeted questions instead of inventing
# them. Two signals: (a) relevance of the symptom to the user's description,
# (b) cross-candidate distinctiveness (presence mass closest to 50%).

def _tfidf_like_vector(text: str) -> dict[str, float]:
    """Lightweight bag-of-words TF vector over cleaned tokens (no embedding call)."""
    tokens = re.findall(r"[a-z]+", text.lower())
    STOP = {
        "a","an","the","is","it","in","on","of","to","and","or","for",
        "with","this","that","was","are","be","at","by","has","have",
        "had","not","but","from","as","do","did","will","would","can",
        "could","my","your","their","its","i","you","he","she","they",
        "we","our","no","so","if","when","how","what","which","where",
        "there","been","more","also","than","then","all","any","some",
        "were","should","may","might","over","after","before",
    }
    counts: dict[str, float] = {}
    for t in tokens:
        if t not in STOP and len(t) > 2:
            counts[t] = counts.get(t, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
    return {k: v / norm for k, v in counts.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity between two TF vectors (dicts)."""
    return sum(a.get(k, 0.0) * v for k, v in b.items())


def select_discriminating_symptoms(
    description: str,
    candidates: list[dict],
    top_k: int = 3,
    min_score: float = MIN_SYMPTOM_SCORE,
) -> list[str]:
    """
    Return the 2-3 symptoms that best discriminate between candidate KB
    articles so the agent can frame targeted follow-up questions.

      a) rel  = cosine(description, symptom)
      b) dist = 1 - |2 * carrier_mass_fraction - 1|   (≈1 when ~50% of mass carries it)
      final   = rel * 0.5 + dist * 0.5

    Symptoms scoring below `min_score` are dropped (relevance floor), so a
    weak/irrelevant symptom is never surfaced. Near-duplicate symptoms
    (cosine > 0.85) are collapsed.
    Returns up to `top_k` symptom strings, best-first; [] if none clear the floor.
    """
    if not candidates:
        return []

    symptom_sources: dict[str, list[float]] = {}
    for cand in candidates:
        for sym in (cand.get("key_symptoms") or []):
            sym = sym.strip()
            if not sym:
                continue
            symptom_sources.setdefault(sym, []).append(float(cand["score"]))

    if not symptom_sources:
        logger.info("select_discriminating_symptoms: no symptoms found in candidates")
        return []

    total_score_mass = sum(c["score"] for c in candidates)
    desc_vec = _tfidf_like_vector(description)

    scored: list[tuple[float, str]] = []
    for sym_text, carrier_scores in symptom_sources.items():
        sym_vec = _tfidf_like_vector(sym_text)
        rel = _cosine(desc_vec, sym_vec)
        carrier_mass = sum(carrier_scores) / total_score_mass if total_score_mass > 0 else 0.0
        dist = 1.0 - abs(2.0 * carrier_mass - 1.0)
        final = rel * 0.5 + dist * 0.5
        scored.append((final, sym_text))

    scored.sort(key=lambda x: -x[0])

    selected: list[str] = []
    selected_vecs: list[dict[str, float]] = []
    for final, sym_text in scored:
        # Relevance floor: list is sorted high→low, so the first symptom below
        # the floor means every remaining one is too — stop here. An empty
        # result then routes the caller to the ASK FREEFORM path.
        if final < min_score:
            break
        sym_vec = _tfidf_like_vector(sym_text)
        if any(_cosine(sym_vec, sv) > 0.85 for sv in selected_vecs):
            continue
        selected.append(sym_text)
        selected_vecs.append(sym_vec)
        if len(selected) >= top_k:
            break

    logger.info(
        "select_discriminating_symptoms: %d candidates -> %d selected "
        "(top_k=%d, min_score=%.2f)",
        len(scored), len(selected), top_k, min_score,
    )
    return selected


# ── Global error handler ──────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}"},
    )


# ── Health check ──────────────────────────────────────────────────
@app.get("/healthz")
def health():
    return {"status": "ok", "tools": ["get_kb_candidates"]}


# ── Main endpoint ─────────────────────────────────────────────────
@app.post("/get_kb_candidates")
def get_kb_candidates(
    body: CandidateRequest,
    x_api_key: str = Header(default=""),
):
    if x_api_key != TOOL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    description = (body.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="'description' is required")

    session_id = (body.session_id or "").strip() or None
    round_no = _bump_round(session_id)
    logger.info("get_kb_candidates | round=%d | session=%s | description=%r",
                round_no, session_id, description[:200])

    # Optional second vector field for symptom matching.
    symptoms_field = os.environ.get("SYMPTOMS_VECTOR_FIELD", "").strip()
    vector_queries = [VectorizableTextQuery(
        text=description,
        k_nearest_neighbors=20,
        fields="content_embedding",
    )]
    if symptoms_field:
        vector_queries.append(VectorizableTextQuery(
            text=description,
            k_nearest_neighbors=20,
            fields=symptoms_field,
        ))

    results = search.search(
        search_text=description,
        query_type=QueryType.SEMANTIC,
        semantic_configuration_name=SEMANTIC_CONFIG_NAME,
        vector_queries=vector_queries,
        select=[
            "kb_id",
            "content_text",
            "symptoms",
        ],
        top=TOP_K,
    )

    rows = []
    for r in results:
        score = float(
            r.get("@search.reranker_score") or r.get("@search.score") or 0.0
        )
        rows.append({
            "kb_id":        r.get("kb_id", ""),
            "summary":      (r.get("content_text") or "")[:300],
            "key_symptoms": r.get("symptoms") or [],
            "score":        round(score, 3),
        })

    candidates = dedupe_by_kb(rows)
    top_score = round(candidates[0]["score"], 3) if candidates else 0.0
    # Spread gate over the distinct-article scores: 'low' = clear strong winner,
    # 'high' = weak top OR near-tie with #2 (< SPREAD_THRESHOLD). Drives the
    # RESOLVE gate below and is also surfaced for debugging.
    spread = compute_spread([c["score"] for c in candidates])
    logger.info("Search -> %d chunks -> %d distinct articles | top_score=%.3f | spread=%s",
                len(rows), len(candidates), top_score, spread)

    # ───────────────── RESOLVE: confident AND a clear winner ─────────
    # Resolve only when the top score clears the confidence threshold AND the
    # spread gate is 'low' (clear winner). compute_spread returns 'high' when
    # the top two DISTINCT articles are within SPREAD_THRESHOLD (e.g. 3.50 vs
    # 3.53) — an ambiguous near-tie — so we do NOT resolve here. Instead the
    # call falls through to the follow-up machinery below (round cap, then
    # symptom/freeform) to ask a disambiguating question rather than guess
    # between two strong, near-equal matches. This spread gate layers on top of
    # the existing 0.9–3.3 follow-up band — together they mean: ask a follow-up
    # when score ∈ [0.9, 3.3] OR (confident but spread is 'high' / near-tie).
    if candidates and top_score > CONFIDENT_SCORE and spread == "low":
        kb_id = candidates[0]["kb_id"]
        _clear_round(session_id)
        logger.info("RESULT=resolve | kb_id=%s | top_score=%.3f | round=%d",
                    kb_id, top_score, round_no)
        return {
            "followup_required": False,
            "kb_id":             kb_id,
            "top_score":         top_score,
            "message":           RESOLVE_MESSAGE,
        }

    # ───────────────── ROUND CAP: stop looping, conclude ──────────────
    # Not confident, and we've hit the follow-up limit. Stop asking. Present
    # the best candidate if it clears the (lower) display threshold; otherwise
    # conclude with no match. Either way the conversation is over.
    if round_no >= MAX_ROUNDS:
        _clear_round(session_id)
        if candidates and top_score >= MIN_DISPLAY_SCORE:
            kb_id = candidates[0]["kb_id"]
            logger.info("RESULT=resolve_at_cap | kb_id=%s | top_score=%.3f | round=%d",
                        kb_id, top_score, round_no)
            return {
                "followup_required": False,
                "kb_id":             kb_id,
                "top_score":         top_score,
                "message":           DISPLAY_AT_CAP_MESSAGE,
            }
        logger.info("RESULT=concluded_no_match | top_score=%.3f | round=%d",
                    top_score, round_no)
        return {
            "followup_required": False,
            "kb_id":             None,
            "top_score":         top_score,
            "message":           CONCLUDED_NO_MATCH_MESSAGE,
        }

    # ───────────────── Sub-confident: a follow-up is needed ────────────
    # Under the round cap. Run symptom selection only when at/above the floor.
    # Below the floor we leave symptoms empty (treated as off-topic / no match).
    symptoms: list[str] = []
    if candidates and top_score >= FOLLOWUP_FLOOR:
        symptoms = select_discriminating_symptoms(
            description=description,
            candidates=candidates[:RETURN_K],
            top_k=TOP_SYMPTOMS_K,
        )

    # ASK SYMPTOMS — symptoms exist to disambiguate between candidates.
    if symptoms:
        logger.info("RESULT=ask_symptoms | top_score=%.3f | n=%d", top_score, len(symptoms))
        return {
            "followup_required":       True,
            "top_score":               top_score,
            "discriminating_symptoms": symptoms,
        }

    # ASK FREEFORM — no usable symptoms. This covers BOTH a valid mid-band
    # match whose KB has no symptom metadata AND a below-floor / off-topic
    # query. The message tells the agent to ask its own Outlook-scoped
    # follow-up; the agent can read top_score to judge which situation it is.
    logger.info("RESULT=ask_freeform | top_score=%.3f", top_score)
    return {
        "followup_required":       True,
        "top_score":               top_score,
        "discriminating_symptoms": [],
        "message":                 NO_SYMPTOMS_MESSAGE,
    }


# ── Diagnostic endpoint ───────────────────────────────────────────
@app.post("/debug_search")
def debug_search(
    body: CandidateRequest,
    x_api_key: str = Header(default=""),
):
    if x_api_key != TOOL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    description = (body.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="'description' is required")

    logger.info("debug_search | description=%r", description[:200])

    results = search.search(
        search_text=description,
        query_type=QueryType.SEMANTIC,
        semantic_configuration_name=SEMANTIC_CONFIG_NAME,
        vector_queries=[VectorizableTextQuery(
            text=description,
            k_nearest_neighbors=20,
            fields="content_embedding",
        )],
        select=[
            "kb_id",
            "content_text",
            "symptoms",
        ],
        top=TOP_K,
    )

    raw_results = []
    for idx, r in enumerate(results, start=1):
        score = float(
            r.get("@search.reranker_score") or r.get("@search.score") or 0.0
        )
        content_text = r.get("content_text") or ""
        raw_results.append({
            "rank":            idx,
            "score":           round(score, 3),
            "kb_id":           r.get("kb_id", ""),
            "symptoms":        r.get("symptoms") or [],
            "content_preview": content_text[:200],
            "content_length":  len(content_text),
        })

    deduped = dedupe_by_kb(raw_results)
    returned = deduped[:RETURN_K]
    return {
        "query":              description,
        "total_results":      len(raw_results),
        "results":            raw_results,
        "distinct_articles":  [
            {"kb_id": c["kb_id"], "score": c["score"]} for c in deduped
        ],
        "spread_on_returned": compute_spread([c["score"] for c in returned]),
    }