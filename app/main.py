import os
import re
import logging
from datetime import datetime, timezone, timedelta
from io import BytesIO
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizableTextQuery, QueryType
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
import docx as python_docx

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

    BLOB_ACCOUNT_URL    = os.environ["BLOB_ACCOUNT_URL"]
    BLOB_CONTAINER_NAME = os.environ["BLOB_CONTAINER_NAME"]
    blob_service        = BlobServiceClient(account_url=BLOB_ACCOUNT_URL, credential=credential)

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


# ── Request models ────────────────────────────────────────────────
class CandidateRequest(BaseModel):
    description: str

class GuidanceRequest(BaseModel):
    kb_id: str


# ── KB docx parser ────────────────────────────────────────────────
_SKIP_PREFIXES  = ("kb id:", "guidance troubleshoot:")
_KNOWN_SECTIONS = ("user experience", "symptoms:", "cause:", "resolution:")
_OPTION_RE      = re.compile(r"^option\s*\d+", re.IGNORECASE)

def _parse_kb_docx(doc_bytes: bytes) -> dict:
    doc = python_docx.Document(BytesIO(doc_bytes))

    title, environment, note = "", "", ""
    sections: list = []
    current_section = None
    first_normal = True

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        tl      = text.lower()
        is_list = para.style.name == "List Paragraph"

        if any(tl.startswith(p) for p in _SKIP_PREFIXES):
            continue

        if is_list:
            if current_section is None:
                current_section = {"heading": "Resolution Steps", "steps": []}
                sections.append(current_section)
            current_section["steps"].append(text)
            continue

        if first_normal:
            title = text
            first_normal = False
            continue

        if tl.startswith("environment:"):
            environment = text.split(":", 1)[1].strip()
            continue

        if tl.startswith("note:"):
            note = text.split(":", 1)[1].strip()
            continue

        # Structural dividers (symptoms, cause, resolution headers) — skip them
        # and the Normal paragraph that immediately follows each one is their body text,
        # which is also skipped by falling through to the end of this loop.
        if any(tl.startswith(h) for h in _KNOWN_SECTIONS):
            current_section = None
            continue

        if _OPTION_RE.match(tl):
            current_section = {"heading": text.rstrip(":"), "steps": []}
            sections.append(current_section)

    return {"title": title, "environment": environment, "note": note, "sections": sections}


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
    return {"status": "ok", "tools": ["get_kb_candidates", "get_kb_guidance"]}


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


# ── KB Guidance endpoint ──────────────────────────────────────────
@app.post("/get_kb_guidance")
def get_kb_guidance(
    body: GuidanceRequest,
    x_api_key: str = Header(default=""),
):
    if x_api_key != TOOL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    kb_id = (body.kb_id or "").strip()
    if not kb_id:
        raise HTTPException(status_code=400, detail="'kb_id' is required")

    logger.info("get_kb_guidance | kb_id=%s", kb_id)
    blob_name = f"{kb_id}.docx"

    try:
        doc_bytes = (
            blob_service
            .get_blob_client(container=BLOB_CONTAINER_NAME, blob=blob_name)
            .download_blob()
            .readall()
        )
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail=f"KB article '{kb_id}' not found in storage.")
    except Exception:
        logger.exception("Blob download failed: %s", blob_name)
        raise

    parsed = _parse_kb_docx(doc_bytes)

    # Generate a 2-hour read-only SAS URL using Managed Identity (user delegation key).
    # Requires Storage Blob Delegator role on the App Service identity.
    document_url = None
    try:
        now    = datetime.now(timezone.utc)
        expiry = now + timedelta(hours=2)
        udk    = blob_service.get_user_delegation_key(key_start_time=now, key_expiry_time=expiry)
        sas    = generate_blob_sas(
            account_name=blob_service.account_name,
            container_name=BLOB_CONTAINER_NAME,
            blob_name=blob_name,
            user_delegation_key=udk,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )
        document_url = f"{BLOB_ACCOUNT_URL.rstrip('/')}/{BLOB_CONTAINER_NAME}/{blob_name}?{sas}"
    except Exception:
        logger.warning("Could not generate SAS URL for %s — returning without link", blob_name)

    return {
        "kb_id":         kb_id,
        "title":         parsed["title"],
        "environment":   parsed["environment"],
        "note":          parsed["note"],
        "sections":      parsed["sections"],
        "document_url":  document_url,
        "document_name": blob_name,
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
