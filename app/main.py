import os
import logging
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


# ── Helper ────────────────────────────────────────────────────────
def compute_spread(scores):
    if len(scores) < 2:
        return "low"
    gap = scores[0] - (scores[2] if len(scores) >= 3 else scores[1])
    return "low" if gap > SPREAD_THRESHOLD else "high"


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
                k_nearest_neighbors=20,
                fields="content_embedding",
            )],
            select=[
                "kb_id",
                "content_text",
                "symptoms",
                "guidance_troubleshoot",
            ],
            top=TOP_K,
        )

        candidates = []
        for r in results:
            score = float(
                r.get("@search.reranker_score") or r.get("@search.score") or 0.0
            )
            candidates.append({
                "kb_id":                 r.get("kb_id", ""),
                "summary":               (r.get("content_text") or "")[:300],
                "key_symptoms":          r.get("symptoms") or [],
                "guidance_troubleshoot": r.get("guidance_troubleshoot"),
                "score":                 round(score, 3),
            })

        if not candidates:
            logger.info("No results returned from search")
            return {
                "candidates": [],
                "spread":     "low",
                "top_score":  0.0,
                "message":    "No candidate exceeded minimum confidence.",
            }

        candidates.sort(key=lambda c: c["score"], reverse=True)
        scores = [c["score"] for c in candidates]

        logger.info("Search returned %d candidates", len(candidates))
        for c in candidates:
            logger.info("  candidate: %s | score=%.3f", c["kb_id"], c["score"])

        if scores[0] < MIN_SCORE:
            logger.info("No candidate above MIN_SCORE=%.2f", MIN_SCORE)
            return {
                "candidates": [],
                "spread":     "low",
                "top_score":  round(scores[0], 3),
                "message":    "No candidate exceeded minimum confidence.",
            }

        return {
            "candidates": candidates[:RETURN_K],
            "spread":     compute_spread(scores),
            "top_score":  round(scores[0], 3),
        }

    except Exception as e:
        logger.exception("Search failed")
        raise


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

        return {
            "query":         description,
            "total_results": len(raw_results),
            "results":       raw_results,
        }

    except Exception as e:
        logger.exception("Debug search failed")
        raise
