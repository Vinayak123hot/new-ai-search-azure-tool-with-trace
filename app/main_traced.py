"""
main_traced.py — drop-in traced version of the KB tool service.

ZERO changes to main.py: this module imports the existing app and wraps the
pipeline functions (Azure AI Search call, chunk dedup, spread computation,
discriminating-symptom selection) plus an HTTP middleware, so that every agent
tool call produces ONE human-readable, step-by-step trace block in
logs/trace_YYYY-MM-DD.log  (and a machine-readable twin in
trace_YYYY-MM-DD.jsonl).

Mirrors the candidates-only main.py: the guidance/docx path has been removed and
the response contract is followup_required / kb_id / top_score /
discriminating_symptoms / message (score bands + round cap), not the old
multi-candidate + meets_display_threshold shape.

Run it instead of main:app — behavior is identical:

    uvicorn app.main_traced:app --host 0.0.0.0 --port 8000 --workers 2
"""

import html
import json
import os
import re
import time

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.responses import Response

try:                       # repo root:  uvicorn app.main_traced:app
    from app import main
    from app import trace_kit as tk
except ImportError:        # app dir:    uvicorn main_traced:app
    import main
    import trace_kit as tk

app = main.app  # the very same FastAPI app — just instrumented below

TRACED_PATHS = ("/get_kb_candidates", "/debug_search")


# ════════════════════════════════════════════════════════════════════
# 1. Azure AI Search — log the exact query and every raw chunk returned
# ════════════════════════════════════════════════════════════════════
_orig_search = main.search.search

def _traced_search(*args, **kwargs):
    t = tk.current()
    t0 = time.perf_counter()
    results = list(_orig_search(*args, **kwargs))   # materialize the paged iterator
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    if t is not None:
        vq_desc = []
        for vq in kwargs.get("vector_queries") or []:
            vq_desc.append(
                f"fields={getattr(vq, 'fields', '?')}, k_nearest_neighbors={getattr(vq, 'k_nearest_neighbors', '?')}"
            )

        t.step("AZURE AI SEARCH — HYBRID RETRIEVAL + SEMANTIC RE-RANK", took_ms=round(elapsed_ms, 1))
        t.kv("index", main.SEARCH_INDEX)
        t.kv("search_text (keyword/BM25 part)", repr(tk.short(kwargs.get("search_text"), 200)))
        t.kv("query_type", str(kwargs.get("query_type")))
        t.kv("semantic_configuration", kwargs.get("semantic_configuration_name"))
        for i, d in enumerate(vq_desc, 1):
            t.kv(f"vector_query[{i}]", d)
        t.kv("top (max chunks requested)", kwargs.get("top"))
        t.kv("took", f"{elapsed_ms:.0f} ms")
        t.log("")
        t.note("How ranking works: keyword (BM25) + vector results are fused by RRF into "
               "'@search.score'; the semantic re-ranker then re-scores the fused top results "
               "on a 0–4 scale into '@search.reranker_score'. main.py uses reranker_score "
               "when present, otherwise @search.score.")
        t.log("")
        t.log(f"RAW CHUNKS RETURNED: {len(results)}")

        raw_rows = []
        for idx, r in enumerate(results, 1):
            rrk = r.get("@search.reranker_score")
            base = r.get("@search.score")
            used = float(rrk or base or 0.0)
            row = {
                "rank": idx,
                "kb_id": r.get("kb_id", ""),
                "reranker_score": round(float(rrk), 3) if rrk is not None else None,
                "base_score": round(float(base), 3) if base is not None else None,
                "score_used": round(used, 3),
                "symptoms": r.get("symptoms") or [],
                "guidance_troubleshoot": r.get("guidance_troubleshoot"),
                "preview": tk.short(r.get("content_text"), 110),
            }
            raw_rows.append(row)
            t.log(f"#{idx:>2}  kb_id={row['kb_id']:<14} reranker={row['reranker_score']} "
                  f"base={row['base_score']}  → score_used={row['score_used']}")
            if row["symptoms"]:
                t.log(f"      symptoms: {row['symptoms']}")
            t.log(f"      text: \"{row['preview']}\"")

        t.data(raw_chunks=raw_rows)
        t.meta["raw_chunk_count"] = len(results)

    return results

main.search.search = _traced_search


# ════════════════════════════════════════════════════════════════════
# 2. Dedup — show exactly which chunks were kept/dropped per KB article
# ════════════════════════════════════════════════════════════════════
_orig_dedupe = main.dedupe_by_kb

def _traced_dedupe(rows):
    out = _orig_dedupe(rows)
    t = tk.current()
    if t is not None:
        t.step("DEDUP — COLLAPSE CHUNKS INTO DISTINCT KB ARTICLES")
        t.note("Rule: when several chunks belong to the same kb_id, only the single "
               "best-scoring chunk survives; articles are then sorted by score (desc).")
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r.get("kb_id", ""), []).append(r)

        for kb_id, grp in sorted(groups.items(),
                                 key=lambda kv: -max(g["score"] for g in kv[1])):
            scores = sorted((g["score"] for g in grp), reverse=True)
            kept, dropped = scores[0], scores[1:]
            line = f"{kb_id:<14} {len(grp)} chunk(s) → kept best score {kept}"
            if dropped:
                line += f", dropped {len(dropped)} chunk(s) with scores {dropped}"
            t.log(line)

        t.log("")
        t.log(f"RESULT: {len(rows)} chunks → {len(out)} distinct KB articles "
              f"(order: {[c['kb_id'] for c in out]})")
        t.data(chunks_in=len(rows), articles_out=len(out),
               kept=[{"kb_id": c["kb_id"], "score": c["score"]} for c in out])

        # Preview of what main.py does right after dedupe — top score + the
        # score bands that route the response (RESOLVE / round-cap / follow-up).
        if out:
            top = out[0]["score"]
            t.meta["top_after_dedupe"] = top
            t.step("SCORE BANDS — HOW main.py ROUTES THIS (0–4 reranker scale)")
            t.kv("top score after dedupe", top)
            t.kv("CONFIDENT_SCORE (resolve threshold)", main.CONFIDENT_SCORE)
            t.kv("MIN_DISPLAY_SCORE (present-at-cap floor)", main.MIN_DISPLAY_SCORE)
            t.kv("FOLLOWUP_FLOOR (below = no symptom selection)", main.FOLLOWUP_FLOOR)
            if top > main.CONFIDENT_SCORE:
                band = "≥ CONFIDENT_SCORE → RESOLVE if spread is LOW (clear winner); else ask follow-up"
            elif top >= main.FOLLOWUP_FLOOR:
                band = "in [FOLLOWUP_FLOOR, CONFIDENT_SCORE] → ASK a follow-up (symptoms if any, else freeform)"
            else:
                band = "below FOLLOWUP_FLOOR → ASK FREEFORM (treated as off-topic / no usable match)"
            t.kv("band verdict", band)
            t.note("Routing also depends on the round cap (MAX_ROUNDS) and the spread gate, "
                   "computed next. RETURN_K limits how many distinct articles feed symptom "
                   f"selection ({main.RETURN_K}).")
            t.data(top_score=top, confident_score=main.CONFIDENT_SCORE,
                   followup_floor=main.FOLLOWUP_FLOOR, min_display_score=main.MIN_DISPLAY_SCORE,
                   return_k=main.RETURN_K)
    return out

main.dedupe_by_kb = _traced_dedupe


# ════════════════════════════════════════════════════════════════════
# 3. Spread — show the full high/low decision math
# ════════════════════════════════════════════════════════════════════
_orig_spread = main.compute_spread

def _traced_spread(scores):
    result = _orig_spread(scores)
    t = tk.current()
    if t is not None:
        t.step("SPREAD — RETRIEVAL-CONFIDENCE DECISION")
        t.kv("scores of returned candidates", scores)
        if scores:
            weak_abs = scores[0] < main.CONFIDENT_SCORE
            t.kv("check 1 · weak absolute match",
                 f"top {scores[0]} < CONFIDENT_SCORE {main.CONFIDENT_SCORE} ? → {weak_abs}")
            if len(scores) > 1:
                gap = round(scores[0] - scores[1], 3)
                weak_dom = gap < main.SPREAD_THRESHOLD
                t.kv("check 2 · weak dominance",
                     f"gap #1−#2 = {gap} < SPREAD_THRESHOLD {main.SPREAD_THRESHOLD} ? → {weak_dom}")
            else:
                t.kv("check 2 · weak dominance", "only one candidate → not applicable (False)")
        t.kv("spread verdict", result.upper())
        if result == "high":
            t.note("HIGH = NOT a clear, strong winner (weak top OR near-tie with #2). The "
                   "endpoint will NOT resolve; it returns followup_required=True so the agent "
                   "asks a disambiguating follow-up (grounded in the symptoms selected next).")
        else:
            t.note("LOW = confident, clear single winner. Combined with top_score > "
                   "CONFIDENT_SCORE this lets the endpoint RESOLVE (followup_required=False).")
        t.data(scores=scores, verdict=result,
               confident_score=main.CONFIDENT_SCORE, spread_threshold=main.SPREAD_THRESHOLD)
    return result

main.compute_spread = _traced_spread


# ════════════════════════════════════════════════════════════════════
# 4. Discriminating symptoms — full rel/mass/dist math per symptom
# ════════════════════════════════════════════════════════════════════
_orig_select = main.select_discriminating_symptoms

def _traced_select(description, candidates, top_k=3):
    t = tk.current()
    if t is not None:
        t.step("DISCRIMINATING-SYMPTOM SELECTION (spread was HIGH)")
        t.note("Each symptom gets: rel = cosine(description, symptom) on TF bag-of-words "
               "(stopwords removed, L2-normalized); mass = share of candidate score mass "
               "carrying the symptom; dist = 1 − |2·mass − 1| (peaks at mass = 0.5, i.e. a "
               "perfect yes/no split); final = 0.5·rel + 0.5·dist. Near-duplicates "
               "(cosine > 0.85) are collapsed; top "
               f"{top_k} survive.")

        # Replicate main.py's math exactly (same helper functions) purely for
        # logging — the actual return value still comes from the original below.
        carriers: dict[str, list] = {}
        for cand in candidates:
            for sym in (cand.get("key_symptoms") or []):
                sym = sym.strip()
                if sym:
                    carriers.setdefault(sym, []).append((cand.get("kb_id", "?"), float(cand["score"])))

        total_mass = sum(c["score"] for c in candidates)
        desc_vec = main._tfidf_like_vector(description)
        t.log("")
        t.kv("description", repr(tk.short(description, 200)))
        t.kv("description tokens (after stopword filter)", sorted(desc_vec.keys()))
        t.kv("total candidate score mass", round(total_mass, 3))
        t.log("")

        scored = []
        for sym, carrying in carriers.items():
            sym_vec = main._tfidf_like_vector(sym)
            rel = main._cosine(desc_vec, sym_vec)
            mass = (sum(s for _, s in carrying) / total_mass) if total_mass > 0 else 0.0
            dist = 1.0 - abs(2.0 * mass - 1.0)
            final = rel * 0.5 + dist * 0.5
            scored.append({"symptom": sym, "rel": round(rel, 3), "mass": round(mass, 3),
                           "dist": round(dist, 3), "final": round(final, 3),
                           "carried_by": [f"{k} ({s})" for k, s in carrying]})

        scored.sort(key=lambda x: -x["final"])
        for i, s in enumerate(scored, 1):
            t.log(f"#{i}  \"{tk.short(s['symptom'], 90)}\"")
            t.log(f"     carried by: {', '.join(s['carried_by'])}", indent=1)
            t.log(f"     rel={s['rel']}  mass={s['mass']}  dist={s['dist']}  →  final={s['final']}", indent=1)

        # Replay the greedy near-duplicate suppression with reasons
        t.log("")
        t.log("NEAR-DUPLICATE FILTER (greedy, cosine > 0.85 vs already-selected):")
        selected_log, selected_vecs = [], []
        for s in scored:
            v = main._tfidf_like_vector(s["symptom"])
            dup_against = None
            for prev, pv in zip(selected_log, selected_vecs):
                c = main._cosine(v, pv)
                if c > 0.85:
                    dup_against = (prev, c)
                    break
            if dup_against:
                t.log(f"SKIP  \"{tk.short(s['symptom'], 70)}\"  (cosine {dup_against[1]:.2f} "
                      f"with \"{tk.short(dup_against[0], 50)}\")")
                continue
            t.log(f"KEEP  \"{tk.short(s['symptom'], 70)}\"")
            selected_log.append(s["symptom"])
            selected_vecs.append(v)
            if len(selected_log) >= top_k:
                break
        t.data(symptom_scores=scored, selected=selected_log)

    result = _orig_select(description, candidates, top_k=top_k)

    if t is not None:
        t.log("")
        t.log(f"SELECTED ({len(result)}): {result}")
        t.note("These strings are returned as 'discriminating_symptoms' — the agent grounds "
               "its follow-up questions in them (Phase 2c: never KB titles, only symptoms).")
    return result

main.select_discriminating_symptoms = _traced_select


# (The former section 5 — get_kb_guidance docx-parsing tracer — was removed:
#  main.py is now candidates-only, so there is no _parse_kb_docx to wrap.)


# ════════════════════════════════════════════════════════════════════
# 6. HTTP middleware — opens/closes the trace, logs request & response,
#    and explains what the agent will do next per its protocol
# ════════════════════════════════════════════════════════════════════
@app.middleware("http")
async def _trace_middleware(request, call_next):
    if request.url.path not in TRACED_PATHS:
        return await call_next(request)

    body_bytes = await request.body()   # cached by Starlette; endpoint re-reads it fine
    tool_name = request.url.path.lstrip("/")
    t = tk.start(f"POST {request.url.path}")

    t.step(f"AGENT → TOOL CALL RECEIVED: {tool_name}")
    key = request.headers.get("x-api-key", "")
    t.kv("x-api-key", f"present ({key[:4]}…)" if key else "MISSING → request will be rejected 401")
    try:
        payload = json.loads(body_bytes.decode("utf-8") or "{}")
    except Exception:
        payload = {"_raw": body_bytes.decode("utf-8", "replace")}
    t.log("request body:")
    t.log(tk.pretty_json(payload), indent=2)
    t.data(request=payload)

    # ── turn-linkage metadata (used by trace_kit to group calls into one
    #    per-turn file named after the user's initial description) ──────
    turn_header = os.environ.get("TRACE_TURN_HEADER", "").strip().lower()
    if turn_header:
        t.meta["turn_key"] = request.headers.get(turn_header) or None
    if request.url.path == "/get_kb_candidates":
        t.meta["endpoint_kind"] = "candidates"
        t.meta["description"] = (payload.get("description") or "") if isinstance(payload, dict) else ""
        t.meta["session_id"] = (payload.get("session_id") or "") if isinstance(payload, dict) else ""

    status, error = 500, None
    try:
        response = await call_next(request)
        status = response.status_code

        chunks = [section async for section in response.body_iterator]
        resp_bytes = b"".join(chunks)
        try:
            resp_json = json.loads(resp_bytes.decode("utf-8"))
        except Exception:
            resp_json = None

        if isinstance(resp_json, dict) and request.url.path == "/get_kb_candidates":
            t.meta["query"] = t.meta.get("description", "")
            t.meta["followup_required"] = resp_json.get("followup_required")
            t.meta["top_score"] = resp_json.get("top_score")
            t.meta["kb_id"] = resp_json.get("kb_id")
            t.meta["discriminating_symptoms"] = resp_json.get("discriminating_symptoms")

        t.step(f"TOOL → AGENT RESPONSE (HTTP {status})")
        t.log(tk.pretty_json(resp_json if resp_json is not None
                             else resp_bytes.decode("utf-8", "replace")), indent=2)
        t.data(status=status, response=resp_json)

        if status == 200 and isinstance(resp_json, dict):
            _explain_next(t, request.url.path, resp_json)

        return Response(content=resp_bytes, status_code=status,
                        headers=dict(response.headers),
                        media_type=response.media_type)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        tk.finish(t, status=status, error=error)


def _log_desc_symptom_similarity(t, description: str, candidates: list):
    """For EVERY candidates response (HIGH or LOW spread), show how the user
    description overlaps each candidate's symptoms — the 'why this candidate'
    signal. Uses the same TF cosine that drives discriminating-symptom
    selection, so the numbers are comparable across the trace."""
    if not candidates:
        return
    t.step("DESCRIPTION ↔ CANDIDATE-SYMPTOM SIMILARITY (why each candidate)")
    t.note("Informational — the tool RANKS candidates by Azure reranker score (the "
           "'score' field); this panel additionally shows lexical overlap between the "
           "description and each candidate's symptoms. rel = cosine(description, symptom) "
           "over stopword-filtered, L2-normalized term-frequency vectors (main._cosine / "
           "main._tfidf_like_vector). best_rel = strongest single symptom; "
           "mean_rel = average across the candidate's symptoms.")

    desc_vec = main._tfidf_like_vector(description)
    t.log("")
    t.kv("description", repr(tk.short(description, 200)))
    t.kv("description tokens (after stopword filter)", sorted(desc_vec.keys()))
    t.log("")

    rows = []
    for c in candidates:
        syms = c.get("key_symptoms") or []
        pairs = sorted(
            ((round(main._cosine(desc_vec, main._tfidf_like_vector(s)), 3), s) for s in syms),
            reverse=True,
        )
        best = pairs[0][0] if pairs else 0.0
        mean = round(sum(r for r, _ in pairs) / len(pairs), 3) if pairs else 0.0
        rows.append({"kb_id": c.get("kb_id"), "score": c.get("score"),
                     "best_rel": best, "mean_rel": mean,
                     "symptom_rel": [{"rel": r, "symptom": s} for r, s in pairs]})
        t.log(f"{str(c.get('kb_id')):<14} reranker_score={c.get('score')}   "
              f"best_rel={best}   mean_rel={mean}")
        for r, s in pairs:
            t.log(f"rel={r:<6} \"{tk.short(s, 90)}\"", indent=2)

    top_by_score = rows[0]
    top_by_rel = max(rows, key=lambda r: r["best_rel"])
    t.log("")
    t.kv("top by reranker score", f"{top_by_score['kb_id']} (score={top_by_score['score']})")
    t.kv("top by description↔symptom overlap",
         f"{top_by_rel['kb_id']} (best_rel={top_by_rel['best_rel']})")
    if top_by_rel["kb_id"] != top_by_score["kb_id"]:
        t.note("⚠ The strongest text-overlap candidate is NOT the top-scored one. The agent "
               "selects the matched candidate from key_symptoms vs the user's situation, so "
               "this divergence is worth reviewing if the final pick looks wrong.")
    else:
        t.note("Top reranker candidate also has the strongest description↔symptom overlap — "
               "consistent signals.")
    t.data(desc_symptom_similarity=rows)


def _log_kb_selection(t, kb_id: str):
    """On a get_kb_guidance call, surface HOW this kb_id was selected by reading
    the persistent selection index that get_kb_candidates writes. This makes a
    guidance trace self-explanatory even when the candidates call happened in an
    earlier turn / another process — directly answering 'how was this KB picked'."""
    info = tk.lookup_selection(kb_id)
    t.step("HOW THIS KB WAS SELECTED (from get_kb_candidates)")
    if not info:
        t.log(f"No get_kb_candidates call returning {kb_id} has reached this trace "
              f"service, so the selection reasoning isn't recorded here.")
        t.note("Likely cause: the agent chose this kb_id from earlier conversation context, "
               "OR get_kb_candidates is routed to a different endpoint than get_kb_guidance. "
               "To capture the 'why', make sure BOTH tool operations point at teva-kb-trace.")
        t.data(kb_selection=None)
        return
    t.kv("kb_id", info.get("kb_id"))
    t.kv("selected because",
         f"get_kb_candidates returned it with score {info.get('score')} "
         f"(rank #{info.get('rank')} of {len(info.get('all_candidates') or [])}), "
         f"spread={info.get('spread')}, guidance_troubleshoot={info.get('guidance_troubleshoot')}")
    t.kv("original user query", repr(tk.short(info.get("query"), 200)))
    comp = info.get("all_candidates") or []
    t.kv("competing candidates (kb_id:score)",
         ", ".join(f"{c.get('kb_id')}:{c.get('score')}" for c in comp) or "n/a")
    t.kv("recorded at", info.get("time"))
    t.kv("trace id of that candidates call", info.get("trace_id"))
    if info.get("turn_file"):
        t.kv("full candidates trace in turn file", info.get("turn_file"))
    t.note("This is the most recent get_kb_candidates that returned this kb_id. If its "
           "timestamp predates the current conversation, treat it as historical context "
           "rather than this turn's retrieval.")
    t.data(kb_selection=info)


def _explain_next(t, path: str, resp: dict):
    """Append a 'WHAT HAPPENS NEXT' step interpreting the response against the
    candidates-only contract (followup_required / kb_id / top_score /
    discriminating_symptoms / message), so the log reads end-to-end."""
    t.step("WHAT HAPPENS NEXT (per classification-agent protocol)")

    if path == "/debug_search":
        t.note("debug_search is diagnostic only — the agent never calls it.")
        return

    if path != "/get_kb_candidates":
        return

    followup = resp.get("followup_required")
    top = resp.get("top_score")
    kb_id = resp.get("kb_id")
    symptoms = resp.get("discriminating_symptoms") or []
    msg = resp.get("message")

    # RESOLVE / present-at-cap: followup_required is False.
    if followup is False:
        if kb_id:
            t.log(f"followup_required=FALSE with kb_id={kb_id} (top_score={top}).")
            t.log("→ Confident, unambiguous match (or best-available at the round cap). The "
                  "agent presents THIS article to the user — no further follow-up.")
        else:
            t.log(f"followup_required=FALSE with kb_id=None (top_score={top}).")
            t.log("→ Concluded with no sufficiently confident match. The agent stops asking "
                  "and closes / hands off per policy.")
        if msg:
            t.log(f"   tool message: {msg}", indent=2)
        return

    # FOLLOW-UP needed: followup_required is True.
    if symptoms:
        t.log(f"followup_required=TRUE (top_score={top}) — ASK SYMPTOMS.")
        t.log("→ Agent asks 1–3 follow-up questions grounded ONLY in these discriminating "
              "symptoms, then calls get_kb_candidates again (same session_id):")
        for s in symptoms:
            t.log(f"   · {s}", indent=2)
    else:
        t.log(f"followup_required=TRUE (top_score={top}) — ASK FREEFORM (no usable symptoms).")
        t.log("→ Agent asks its own focused, Outlook-scoped follow-up question (the tool "
              "supplied no discriminating symptoms) and calls get_kb_candidates again.")
        if msg:
            t.log(f"   tool message: {msg}", indent=2)
    t.note("Round cap: after MAX_ROUNDS follow-up calls (counted per session_id) the tool "
           "force-concludes — presenting the best candidate if top_score ≥ MIN_DISPLAY_SCORE, "
           "else returning kb_id=None.")


# ════════════════════════════════════════════════════════════════════
# 7. Browser log viewer — read traces from anywhere, no Kudu needed
#    GET /trace?key=...               index of TURNS (one per conversation),
#                                     newest first, named by user description
#    GET /trace?key=...&turn=<file>   the full end-to-end trace of one turn
#    GET /trace?key=...&day=YYYY-MM-DD  flat day view (all calls, newest first)
#    GET /trace/raw?key=...&turn=<file> | &day=YYYY-MM-DD  plain-text download
#    Auth: TRACE_VIEW_KEY app setting if set, else the tool API key.
# ════════════════════════════════════════════════════════════════════
_BLOCK_SPLIT = re.compile(r"(?=^═{20,}\nTRACE )", re.M)
_CSS = """
  body  { background:#0d1117; color:#c9d1d9; font-family:Consolas,monospace; margin:16px; }
  a     { color:#58a6ff; text-decoration:none; }
  a:hover { text-decoration:underline; }
  .top  { position:sticky; top:0; background:#0d1117; padding:8px 0;
          border-bottom:1px solid #30363d; margin-bottom:8px; }
  .blk  { background:#161b22; border:1px solid #30363d; border-radius:6px;
          padding:12px; margin:14px 0; white-space:pre-wrap; font-size:12.5px; line-height:1.45; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  td,th { border-bottom:1px solid #21262d; padding:7px 10px; text-align:left; vertical-align:top; }
  th    { color:#8b949e; }
  tr:hover td { background:#161b22; }
  .desc { color:#e6edf3; }
  .meta { color:#8b949e; }
"""


def _check_view_key(key: str):
    expected = os.environ.get("TRACE_VIEW_KEY", "").strip() or main.TOOL_API_KEY
    if not key or key != expected:
        raise HTTPException(status_code=401, detail="missing or invalid ?key=")


def _trace_days() -> list[str]:
    try:
        return sorted(
            f[len("trace_"):-len(".log")]
            for f in os.listdir(tk.LOG_DIR)
            if f.startswith("trace_") and f.endswith(".log")
        )
    except FileNotFoundError:
        return []


def _read_day(day: str) -> str:
    path = os.path.join(tk.LOG_DIR, f"trace_{day}.log")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _turn_files() -> list[dict]:
    """All per-turn files, newest first, with parsed header metadata."""
    out = []
    try:
        names = os.listdir(tk.TURN_DIR)
    except FileNotFoundError:
        return out
    for name in names:
        if not (name.startswith("turn_") and name.endswith(".log")):
            continue
        path = os.path.join(tk.TURN_DIR, name)
        try:
            st = os.stat(path)
            desc, opened, ncalls = "", "", 0
            with open(path, encoding="utf-8") as f:
                head = [next(f, "") for _ in range(4)]
                body = f.read()
            for ln in head:
                if ln.startswith("initial user description:"):
                    desc = ln.split(":", 1)[1].strip()
                elif ln.startswith("TURN "):
                    opened = ln.strip()
            ncalls = body.count("\nTRACE ") + (1 if body.startswith("TRACE ") else 0)
        except Exception:
            continue
        out.append({"name": name, "desc": desc, "opened": opened,
                    "calls": ncalls, "mtime": st.st_mtime, "size": st.st_size})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _read_turn(name: str) -> str:
    # guard against path traversal
    if "/" in name or "\\" in name or not name.endswith(".log"):
        raise HTTPException(status_code=400, detail="bad turn name")
    path = os.path.join(tk.TURN_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="turn not found")
    with open(path, encoding="utf-8") as f:
        return f.read()


@app.get("/trace/raw", response_class=PlainTextResponse)
def trace_raw(key: str = "", day: str = "", turn: str = ""):
    _check_view_key(key)
    if turn:
        return _read_turn(turn)
    days = _trace_days()
    if not days:
        return "No traces recorded yet."
    sel = day if day in days else days[-1]
    return _read_day(sel)


def _page(title: str, top_html: str, body_html: str, refresh: int = 0) -> HTMLResponse:
    meta = f"<meta http-equiv='refresh' content='{int(refresh)}'>" if refresh > 0 else ""
    return HTMLResponse(
        f"<!doctype html><html><head><title>{title}</title>{meta}"
        f"<style>{_CSS}</style></head><body>"
        f"<div class='top'>{top_html}</div>{body_html}</body></html>"
    )


@app.get("/trace", response_class=HTMLResponse)
def trace_view(key: str = "", turn: str = "", day: str = "", refresh: int = 0, limit: int = 80):
    _check_view_key(key)
    k = html.escape(key)

    # ── single turn: full end-to-end story, chronological ──────────
    if turn:
        text = _read_turn(turn)
        blocks = [b for b in _BLOCK_SPLIT.split(text) if b.strip()]
        head = blocks[0] if blocks and blocks[0].lstrip().startswith("═") and "TURN " in blocks[0] else ""
        # the header chunk (before the first TRACE) rides with blocks[0]; split it out
        call_blocks = blocks
        top = (f"<a href='/trace?key={k}'>&larr; all turns</a> &nbsp;│&nbsp; "
               f"<b>{html.escape(turn)}</b> &nbsp;│&nbsp; "
               f"<a href='/trace?key={k}&turn={html.escape(turn)}&refresh=10'>auto-refresh 10s</a> "
               f"&nbsp;│&nbsp; <a href='/trace/raw?key={k}&turn={html.escape(turn)}'>raw</a>")
        body = "\n".join(f"<pre class='blk'>{html.escape(b.rstrip())}</pre>" for b in call_blocks)
        return _page(f"turn {turn}", top, body, refresh)

    # ── flat day view (every call, newest first) ───────────────────
    if day:
        days = _trace_days()
        sel = day if day in days else (days[-1] if days else "")
        blocks = [b for b in _BLOCK_SPLIT.split(_read_day(sel)) if b.strip()] if sel else []
        total = len(blocks)
        blocks = list(reversed(blocks))[:max(1, limit)]
        nav = " · ".join(f"<a href='/trace?key={k}&day={d}'>{d}</a>" if d != sel else f"<b>{d}</b>"
                         for d in days[-14:])
        top = (f"<a href='/trace?key={k}'>&larr; turns</a> &nbsp;│&nbsp; "
               f"<b>day {sel}</b> — {len(blocks)} of {total} calls (newest first) "
               f"&nbsp;│&nbsp; days: {nav} &nbsp;│&nbsp; "
               f"<a href='/trace/raw?key={k}&day={sel}'>raw file</a>")
        body = "\n".join(f"<pre class='blk'>{html.escape(b.rstrip())}</pre>" for b in blocks)
        return _page(f"day {sel}", top, body, refresh)

    # ── default: index of turns ────────────────────────────────────
    turns = _turn_files()
    days = _trace_days()
    daylinks = " · ".join(f"<a href='/trace?key={k}&day={d}'>{d}</a>" for d in days[-14:]) or "—"
    top = (f"<b>KB trace — turns</b> ({len(turns)}) &nbsp;│&nbsp; "
           f"each row = one user question and its tool call(s) &nbsp;│&nbsp; "
           f"<a href='/trace?key={k}&refresh=10'>auto-refresh 10s</a> &nbsp;│&nbsp; "
           f"flat day view: {daylinks}")
    if not turns:
        return _page("turns", top, "<p style='margin-top:20px'>No turns recorded yet — "
                                   "run the agent once and reload.</p>", refresh)
    rows = ["<table><tr><th>when</th><th>initial user description</th>"
            "<th>calls</th><th></th></tr>"]
    for t in turns:
        rows.append(
            f"<tr><td class='meta'>{html.escape(t['opened'].replace('TURN ', '').split('│')[-1].strip() or '')}</td>"
            f"<td class='desc'><a href='/trace?key={k}&turn={html.escape(t['name'])}'>"
            f"{html.escape(t['desc'] or t['name'])}</a></td>"
            f"<td>{t['calls']}</td>"
            f"<td class='meta'><a href='/trace/raw?key={k}&turn={html.escape(t['name'])}'>raw</a></td></tr>"
        )
    rows.append("</table>")
    return _page("turns", top, "\n".join(rows), refresh)
