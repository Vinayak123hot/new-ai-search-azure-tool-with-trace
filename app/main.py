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
SEARCH_ENDPOINT  = os.environ["SEARCH_ENDPOINT"]
SEARCH_INDEX     = os.environ["SEARCH_INDEX"]
SPREAD_THRESHOLD = float(os.environ.get("SPREAD_THRESHOLD", "0.6"))
MIN_SCORE        = float(os.environ.get("MIN_SCORE", "0.1"))
TOP_K            = int(os.environ.get("TOP_K", "5"))
RETURN_K         = int(os.environ.get("RETURN_K", "3"))

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
            semantic_configuration_name="multimodal-rag-1778857242111-semantic-configuration",
            vector_queries=[VectorizableTextQuery(
                text=description,
                k_nearest_neighbors=10,
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

        candidates, scores = [], []
        for r in results:
            score = float(
                r.get("@search.reranker_score")
                or r.get("@search.score")
                or 0.0
            )
            scores.append(score)
            content_text = r.get("content_text") or ""
            candidates.append({
                "kb_id":                 r.get("content_id", ""),
                "kb_article_name":       r.get("document_title", ""),
                "summary":               content_text[:300],
                "key_symptoms":          extract_key_symptoms(content_text),
                "guidance_troubleshoot": r.get("guidance_troubleshoot"),
                "score":                 round(score, 3),
            })
            logger.info(
                "candidate: %s | score=%.3f",
                r.get("document_title"), score
            )

        if not candidates or scores[0] < MIN_SCORE:
            logger.info("No candidate above MIN_SCORE=%.2f", MIN_SCORE)
            return {
                "candidates": [],
                "spread":     "low",
                "top_score":  round(scores[0], 3) if scores else 0.0,
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