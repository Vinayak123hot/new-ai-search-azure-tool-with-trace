import os
import logging
from collections import Counter, OrderedDict
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
SPREAD_THRESHOLD      = float(os.environ.get("SPREAD_THRESHOLD", "0.6"))
MIN_SCORE             = float(os.environ.get("MIN_SCORE", "0.1"))
TOP_K                 = int(os.environ.get("TOP_K", "15"))
RETURN_K              = int(os.environ.get("RETURN_K", "3"))

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


# ── Helper functions ──────────────────────────────────────────────
def compute_spread(scores):
    if len(scores) < 2:
        return "low"
    gap = scores[0] - (scores[2] if len(scores) >= 3 else scores[1])
    return "low" if gap > SPREAD_THRESHOLD else "high"


def extract_key_symptoms(content_text: str) -> list[str]:
    """
    Extracts up to 5 short symptom sentences from content_text.
    This is a fallback until a proper key_symptoms field is added
    to the index with curated values.
    """
    if not content_text:
        return []
    sentences = [s.strip() for s in content_text.replace("\n", ". ").split(".") if len(s.strip()) > 20]
    return sentences[:5]


def guidance_flag(value) -> bool | None:
    """
    Returns True if troubleshooting guidance exists in the KB article,
    else None (renders as null in JSON).
    Treats empty strings, whitespace-only strings, and missing values as null.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return True


def merge_symptoms(symptom_lists: list[list[str]], max_total: int = 5) -> list[str]:
    """
    Merges multiple symptom lists (from different chunks of the same KB),
    deduplicating while preserving order, capped at max_total.
    """
    seen = set()
    merged = []
    for sym_list in symptom_lists:
        for s in sym_list:
            key = s.lower().strip()
            if key and key not in seen:
                seen.add(key)
                merged.append(s)
                if len(merged) >= max_total:
                    return merged
    return merged


def deduplicate_chunks_by_kb(raw_results: list[dict]) -> list[dict]:
    """
    Groups chunks by document_title (KB article name).
    For each KB, keeps the highest-scoring chunk's metadata,
    and merges key_symptoms across ALL chunks of that KB.
    Returns deduplicated list, ordered by highest score per KB.
    """
    # Group chunks by document_title, preserving first-seen order (= reranker order)
    grouped: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in raw_results:
        title = r.get("document_title", "") or "(untitled)"
        grouped.setdefault(title, []).append(r)

    deduped = []
    for title, chunks in grouped.items():
        # Sort chunks for this KB by score (descending) — best chunk wins
        chunks_sorted = sorted(
            chunks,
            key=lambda c: float(
                c.get("@search.reranker_score") or c.get("@search.score") or 0.0
            ),
            reverse=True,
        )
        best = chunks_sorted[0]
        best_score = float(
            best.get("@search.reranker_score")
            or best.get("@search.score")
            or 0.0
        )

        # Merge key_symptoms across all chunks of this KB
        all_symptoms = [extract_key_symptoms(c.get("content_text") or "") for c in chunks_sorted]
        merged_symptoms = merge_symptoms(all_symptoms, max_total=5)

        # Use the best chunk's content_text for the summary
        best_content_text = best.get("content_text") or ""

        # Check across ALL chunks of this KB — if ANY chunk has guidance, flag = True
        any_guidance = any(
            guidance_flag(c.get("guidance_troubleshoot")) for c in chunks_sorted
        )
        guidance_value = True if any_guidance else None

        deduped.append({
            "kb_id":                 best.get("content_id", ""),
            "kb_article_name":       title,
            "summary":               best_content_text[:300],
            "key_symptoms":          merged_symptoms,
            "guidance_troubleshoot": guidance_value,  # true or null (checked across all chunks)
            "score":                 round(best_score, 3),
            "_chunk_count":          len(chunks),   # for debugging/visibility
        })

    # Sort final deduplicated list by score (descending)
    deduped.sort(key=lambda d: d["score"], reverse=True)
    return deduped


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
    return {"status": "ok", "tool": "get_kb_candidates"}


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

    logger.info("get_kb_candidates | description=%r", description[:200])

    try:
        results = search.search(
            search_text=description,
            query_type=QueryType.SEMANTIC,
            semantic_configuration_name=SEMANTIC_CONFIG_NAME,
            vector_queries=[VectorizableTextQuery(
                text=description,
                k_nearest_neighbors=20,   # ⬆️ raised to pull more chunks for dedup
                fields="content_embedding",
            )],
            select=[
                "content_id",
                "document_title",
                "content_text",
                "guidance_troubleshoot",
            ],
            top=TOP_K,
        )

        # Collect all raw chunks first (need them for dedup grouping)
        raw_chunks = list(results)

        if not raw_chunks:
            logger.info("No chunks returned from search")
            return {
                "candidates": [],
                "spread":     "low",
                "top_score":  0.0,
                "message":    "No candidate exceeded minimum confidence.",
            }

        # Log raw chunks before dedup
        logger.info("Raw chunks returned: %d", len(raw_chunks))
        for r in raw_chunks:
            score = float(
                r.get("@search.reranker_score")
                or r.get("@search.score")
                or 0.0
            )
            logger.info(
                "  raw chunk: %s | score=%.3f",
                r.get("document_title"), score
            )

        # Deduplicate by document_title
        candidates = deduplicate_chunks_by_kb(raw_chunks)
        scores = [c["score"] for c in candidates]

        logger.info("After dedup: %d unique KB articles", len(candidates))
        for c in candidates:
            logger.info(
                "  candidate: %s | score=%.3f | chunks=%d",
                c["kb_article_name"], c["score"], c["_chunk_count"]
            )

        if not candidates or scores[0] < MIN_SCORE:
            logger.info("No candidate above MIN_SCORE=%.2f", MIN_SCORE)
            return {
                "candidates": [],
                "spread":     "low",
                "top_score":  round(scores[0], 3) if scores else 0.0,
                "message":    "No candidate exceeded minimum confidence.",
            }

        # Drop internal _chunk_count before returning to agent
        final_candidates = []
        for c in candidates[:RETURN_K]:
            c_clean = {k: v for k, v in c.items() if not k.startswith("_")}
            final_candidates.append(c_clean)

        return {
            "candidates": final_candidates,
            "spread":     compute_spread(scores),
            "top_score":  round(scores[0], 3),
        }

    except Exception as e:
        logger.exception("Search failed")
        raise


# ── 🔧 DIAGNOSTIC ENDPOINT — checks if results are duplicate chunks ──
@app.post("/debug_search")
def debug_search(
    body: CandidateRequest,
    x_api_key: str = Header(default=""),
):
    """
    Diagnostic endpoint to inspect raw chunks returned by Azure AI Search.
    Helps determine if multiple chunks come from the same KB article.
    Remove or protect this endpoint in production.
    """
    if x_api_key != TOOL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    description = (body.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="'description' is required")

    logger.info("debug_search | description=%r", description[:200])

    try:
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
                "content_id",
                "document_title",
                "content_text",
            ],
            top=TOP_K,
        )

        raw_chunks = []
        titles = []
        for idx, r in enumerate(results, start=1):
            score = float(
                r.get("@search.reranker_score")
                or r.get("@search.score")
                or 0.0
            )
            title = r.get("document_title", "")
            titles.append(title)
            content_text = r.get("content_text") or ""
            raw_chunks.append({
                "rank":            idx,
                "score":           round(score, 3),
                "content_id":      r.get("content_id", ""),
                "document_title":  title,
                "content_preview": content_text[:200],
                "content_length":  len(content_text),
            })

        title_counts = Counter(titles)
        duplicates = {t: c for t, c in title_counts.items() if c > 1}

        return {
            "query":              description,
            "total_chunks":       len(raw_chunks),
            "unique_kb_articles": len(set(titles)),
            "duplicate_titles":   duplicates,
            "has_duplicates":     bool(duplicates),
            "chunks":             raw_chunks,
        }

    except Exception as e:
        logger.exception("Debug search failed")
        raise
