# KB Candidates Tool — Azure App Service

A lightweight FastAPI service for **Outlook support triage**. Given a free-text issue description, it queries an Azure AI Search knowledge base and returns the most relevant KB articles with a confidence signal (`spread`) that tells an AI agent whether to ask a follow-up question or resolve immediately.

Designed to be called as a **tool by an AI agent** (Azure AI Foundry, Copilot Studio, etc.).

---

## How It Works

```
   User issue
   ──────────►  AI Agent  ──► POST /get_kb_candidates  ──► Azure AI Search
                                      │                    (semantic + vector)
                                      ▼
                             Returns candidates + spread
                             spread=high → agent asks follow-up
                             spread=low  → agent resolves issue
```

---

## Project Structure

```
├── app/
│   ├── main.py                      # All application code (FastAPI)
│   └── requirements.txt             # Python dependencies (mirror of root)
│
├── openapi/
│   ├── kb_candidates.json           # OpenAPI 3.0 spec — agent tool definition
│   └── test.sh                      # End-to-end smoke tests
│
├── requirements.txt                 # Python dependencies — Oryx reads this at build
├── startup.sh                       # App Service startup command
├── runtime.txt                      # Python version hint for Oryx
├── azure-app-settings.env.example   # Reference for App Settings to configure in portal
└── .gitignore
```

---

## File Reference

### `app/main.py`
The entire application. Responsibilities:
- Loads config from environment variables at startup
- Fetches `search-api-key` and `tool-api-key` from Azure Key Vault via Managed Identity
- `GET /healthz` — liveness probe, no auth required
- `POST /get_kb_candidates` — authenticates caller via `x-api-key` header, runs hybrid semantic + vector search, deduplicates chunks by KB article, computes `spread` signal, returns top candidates
- `POST /debug_search` — returns raw search chunks for tuning `TOP_K` / `MIN_SCORE`
- Global exception handler returning clean JSON 500 errors

**This is the only file you need to edit to change application behaviour.**

### `requirements.txt` (root)
Pinned Python dependencies read by the Azure Oryx build system during deployment. Must stay at the repo root. Contains all packages including `azure-identity` and `azure-keyvault-secrets` required for Key Vault access.

### `startup.sh`
The command Azure App Service runs to start the application:
```bash
/home/site/wwwroot/pythonenv3.12/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --log-level info
```
Points to the virtual environment created by Oryx during build. If you change the Python runtime version, update the `pythonenv3.XX` path to match.

### `runtime.txt`
Tells Oryx which Python version to use when building the virtual environment. Current value: `python-3.11`. Azure App Service container may use a different patch version — the startup command path must match the actual venv created.

### `azure-app-settings.env.example`
Documents all environment variables that must be configured as Application Settings in Azure Portal. Not loaded by the app directly — it is a reference only. Copy values from here into the portal.

### `app/requirements.txt`
Mirror of the root `requirements.txt`. Kept for local development reference.

### `openapi/kb_candidates.json`
OpenAPI 3.0 specification used by AI agents to understand and call the tool. Contains the server URL, request/response schema, `x-api-key` security scheme, and behavioral instructions for the agent embedded in endpoint descriptions. **Update the `servers[].url` field when deploying to a new App Service.**

### `openapi/test.sh`
Smoke test script that validates the live endpoint:
1. Health check (`/healthz`)
2. Valid request — expects candidates + spread
3. Wrong API key — expects `401`
4. Empty description — expects `400`

**Update `BASE` and `KEY` variables before running.**

---

## API Reference

### GET /healthz
No auth. Returns:
```json
{ "status": "ok", "tool": "get_kb_candidates" }
```

### POST /get_kb_candidates
```
Header: x-api-key: <tool-api-key>
Header: Content-Type: application/json
```
```json
{ "description": "Outlook crashes when I click send with a PDF attached" }
```
Response:
```json
{
  "candidates": [
    {
      "kb_id": "...",
      "kb_article_name": "...",
      "summary": "...",
      "key_symptoms": ["symptom 1", "symptom 2"],
      "guidance_troubleshoot": true,
      "score": 2.134
    }
  ],
  "spread": "high",
  "top_score": 2.134
}
```

| `spread` | Meaning | Agent action |
|----------|---------|-------------|
| `high` | Candidates are clustered — ambiguous | Ask one follow-up using `key_symptoms` |
| `low` | One candidate clearly leads | Resolve the issue |

| Status | Meaning |
|--------|---------|
| `200` | Success (candidates may be empty if below `MIN_SCORE`) |
| `400` | Missing or empty `description` |
| `401` | Invalid or missing `x-api-key` |
| `500` | Search or server error |

### POST /debug_search
Same auth and body as above. Returns raw chunk-level results from Azure AI Search — use this to tune `TOP_K`, `MIN_SCORE`, and `SPREAD_THRESHOLD` without changing code.

---

## Tuning Parameters (App Settings)

| Setting | Purpose | Default |
|---------|---------|---------|
| `TOP_K` | Raw chunks fetched from search | `5` |
| `RETURN_K` | Final candidates returned to agent | `3` |
| `MIN_SCORE` | Minimum reranker score to include a candidate | `1.0` |
| `SPREAD_THRESHOLD` | Score gap above which spread is "low" (confident) | `0.6` |

---

## Deploying to a New Azure App Service

Follow these steps in order. Replace every value in `< >` with your own.

### Prerequisites
Before starting, have these ready:
- An Azure subscription
- An **Azure AI Search** service with a populated index
- An **Azure Key Vault** containing two secrets:
  - `search-api-key` — Azure AI Search admin key
  - `tool-api-key` — any strong random string (the API password callers must send)

Generate a strong `tool-api-key`:
```bash
openssl rand -hex 32
```

---

### Step 1 — Clone the repo
```bash
git clone https://github.com/Vinayak123hot/ai-search-azure-web-app.git
cd ai-search-azure-web-app
```

---

### Step 2 — Create Azure App Service

In Azure Portal:
1. Create a resource → **Web App**
2. Configure:
   - **Runtime stack:** Python 3.12
   - **Operating System:** Linux
   - **Region:** your preferred region
3. Create and wait for deployment to finish

---

### Step 3 — Enable Managed Identity

`App Service → Identity → System assigned → Status: On → Save`

Copy the **Object (principal) ID** shown — needed in Step 4.

---

### Step 4 — Grant Key Vault access

`Key Vault → Access control (IAM) → Add role assignment`
- Role: **Key Vault Secrets User**
- Assign access to: **Managed identity**
- Select: your App Service

---

### Step 5 — Configure Application Settings

`App Service → Environment variables → App settings → + Add`

Add all of these — **change the values marked with ← CHANGE**:

| Name | Value |
|------|-------|
| `SEARCH_ENDPOINT` | `https://<your-search-service>.search.windows.net`  ← CHANGE |
| `SEARCH_INDEX` | `<your-index-name>`  ← CHANGE |
| `AZURE_KEY_VAULT_URL` | `https://<your-keyvault>.vault.azure.net`  ← CHANGE |
| `SPREAD_THRESHOLD` | `0.6` |
| `MIN_SCORE` | `1.0` |
| `TOP_K` | `5` |
| `RETURN_K` | `3` |
| `WEBSITES_PORT` | `8000` |

Click **Save**.

---

### Step 6 — Deploy the code

#### Option A — Kudu Zip Deploy (no CLI needed)

Get your deployment credentials:
`App Service → Deployment Center → FTPS credentials`

Note the username (starts with `$`) and password. Then run:

```bash
# Create zip (exclude git and cache)
zip -r deploy.zip . --exclude "*.git*" --exclude "*__pycache__*" --exclude "*.pyc"

# Upload to App Service (replace USERNAME and PASSWORD)
curl -X PUT \
  -u '$<USERNAME>:<PASSWORD>' \
  -H "Content-Type: application/zip" \
  --data-binary @deploy.zip \
  https://<app-name>.scm.<region>.azurewebsites.net/api/zip/site/wwwroot/
```

Then run the Oryx build to install packages:
```bash
curl -s -u '$<USERNAME>:<PASSWORD>' \
  -X POST \
  "https://<app-name>.scm.<region>.azurewebsites.net/api/command" \
  -H "Content-Type: application/json" \
  -d '{"command":"/opt/oryx/oryx build /home/site/wwwroot --output /home/site/wwwroot --platform python --platform-version 3.12","dir":"/home/site/wwwroot"}'
```

#### Option B — GitHub Actions (recommended for ongoing deploys)

`App Service → Deployment Center → Source: GitHub`

Connect your repo and branch. Azure auto-deploys on every push to `main`.

---

### Step 7 — Set startup command

`App Service → Configuration → General settings → Startup Command:`
```
bash /home/site/wwwroot/startup.sh
```
Click **Save** — App Service restarts automatically.

---

### Step 8 — Update OpenAPI spec

Edit `openapi/kb_candidates.json` — update the server URL:
```json
"servers": [
  { "url": "https://<your-app-name>.<region>.azurewebsites.net" }
]
```

---

### Step 9 — Verify

```bash
# Health check — should return {"status":"ok","tool":"get_kb_candidates"}
curl https://<your-app-name>.<region>.azurewebsites.net/healthz

# Full smoke test (edit BASE and KEY in the script first)
bash openapi/test.sh
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `KeyError: 'SEARCH_ENDPOINT'` in logs | App Settings not saved | Re-add all settings in portal and Save |
| Exit code 127 on startup | `startup.sh` points to wrong venv path | Check which `pythonenvX.XX` folder Oryx created, update `startup.sh` |
| `401` on all requests | Wrong `tool-api-key` | Check the secret value in Key Vault matches what callers send |
| `RuntimeError: Failed to load secrets from Key Vault` | Managed Identity not granted access | Redo Step 4 |
| App serves default placeholder page | Startup command not set | Redo Step 7 |

### View logs
`App Service → Advanced Tools → Kudu → Logs → View Current Container Log`

Or via Kudu URL:
```
https://<app-name>.scm.<region>.azurewebsites.net/api/logs/docker
```

---

## Local Development

```bash
# Clone
git clone https://github.com/Vinayak123hot/ai-search-azure-web-app.git
cd ai-search-azure-web-app

# Create venv and install deps
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Set env vars (copy from azure-app-settings.env.example and fill in values)
export SEARCH_ENDPOINT=https://<your-search>.search.windows.net
export SEARCH_INDEX=<your-index>
export AZURE_KEY_VAULT_URL=https://<your-vault>.vault.azure.net

# Auth for local Key Vault access
az login

# Run
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

App available at `http://localhost:8000`
