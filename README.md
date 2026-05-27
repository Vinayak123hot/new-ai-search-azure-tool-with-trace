# KB Candidates Tool

A lightweight HTTP service that powers **Outlook support triage**. Given a free-text
description of an issue, it searches a curated Azure AI Search knowledge base and returns
the most relevant KB articles, along with metadata that helps an AI agent decide whether
to **ask a clarifying question** or **resolve the issue immediately**.

The service is designed to be called as a **tool by an AI agent** (e.g. an Azure AI
Foundry / Copilot-style agent). It exposes a single, well-documented API endpoint and is
deployed as a hardened systemd service behind nginx with TLS.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Folder Structure](#folder-structure)
3. [File-by-File Reference](#file-by-file-reference)
4. [The API](#the-api)
5. [Configuration](#configuration)
6. [Deployment](#deployment)
7. [Testing](#testing)

---

## How It Works

```
                 ┌──────────────────────────────────────────────┐
   User issue    │                  AI Agent                     │
  ────────────►  │  (sends cumulative description on each turn)  │
                 └───────────────────┬──────────────────────────┘
                                     │  POST /get_kb_candidates
                                     │  (x-api-key header)
                                     ▼
   ┌───────────────┐        ┌──────────────────┐        ┌─────────────────────┐
   │     nginx     │  ───►  │  FastAPI service  │  ───►  │   Azure AI Search   │
   │ (TLS + proxy) │        │   (uvicorn:8000)  │        │ (semantic + vector) │
   └───────────────┘        └──────────────────┘        └─────────────────────┘
```

1. The agent sends the user's **full cumulative description** to `/get_kb_candidates`.
2. The service runs a **hybrid query** against Azure AI Search — combining semantic
   ranking with vector (embedding) similarity.
3. It returns the top candidate articles plus a **`spread`** signal:
   - **`high`** → candidates are tightly clustered (ambiguous). The agent should ask
     **one** follow-up question grounded only in the returned `key_symptoms`.
   - **`low`** → one candidate clearly leads. The agent is ready to resolve.
4. The agent calls the tool again after each follow-up answer until the spread resolves.

This "spread" mechanism is the core idea: it lets the agent intelligently decide when it
has enough confidence to act, instead of guessing.

---

## Folder Structure

```
clasisi_agent/
├── app/                     # The FastAPI application
│   ├── main.py              # Service entrypoint and all business logic
│   └── requirements.txt     # Python dependencies (pinned versions)
│
├── config/                  # Deployment configuration
│   ├── kbtool.env           # Environment variables & secrets (NOT for source control)
│   ├── kbtool.service       # systemd unit definition
│   └── nginx.conf           # nginx reverse-proxy site definition
│
├── openapi/                 # Tool contract & integration tests
│   ├── kb_candidates.json   # OpenAPI 3.0 spec (the agent's tool definition)
│   └── test.sh              # End-to-end smoke tests against the live endpoint
│
├── scripts/                 # Operations automation
│   ├── setup.sh             # One-time VM provisioning (deps, service, nginx, TLS)
│   └── deploy.sh            # Redeploy: update deps + restart service
│
└── venv/                    # Python virtual environment (generated, not in repo)
```

---

## File-by-File Reference

### `app/main.py` — Application core
The heart of the project. A FastAPI application that:

- **Loads configuration** from environment variables (search credentials, API key, and
  tuning knobs like `TOP_K`, `MIN_SCORE`, `SPREAD_THRESHOLD`).
- **`GET /healthz`** — a simple liveness probe used by deploy scripts and nginx.
- **`POST /get_kb_candidates`** — the main endpoint. It:
  - Authenticates the caller via the `x-api-key` header.
  - Validates that a `description` was provided.
  - Runs a **semantic + vector** query against Azure AI Search.
  - Computes a confidence **`spread`** (`compute_spread`) from the result scores.
  - Extracts short symptom snippets (`extract_key_symptoms`) as a stand-in until a
    curated `key_symptoms` field is added to the index.
  - Filters out low-confidence results below `MIN_SCORE`.
- **Global exception handler** — returns clean JSON `500` errors and logs the full trace.

**Importance:** This is the only application code in the project; everything else exists
to configure, deploy, document, or test it.

### `app/requirements.txt` — Dependencies
Pins exact versions of the runtime libraries:
- `fastapi` / `uvicorn` — the web framework and ASGI server.
- `pydantic` — request validation (`CandidateRequest`).
- `azure-search-documents` / `azure-core` — the Azure AI Search client.

**Importance:** Guarantees reproducible installs across the dev machine and the VM.

### `config/kbtool.env` — Secrets & tuning
Environment file consumed by the systemd service. Holds:
- **Azure AI Search** endpoint, index name, and admin key.
- **`TOOL_API_KEY`** — the shared secret the agent must send in `x-api-key`.
- **Tuning parameters** — `SPREAD_THRESHOLD`, `MIN_SCORE`, `TOP_K`, `RETURN_K`.

> ⚠️ **Security note:** This file contains live credentials. It should be kept off
> source control, restricted with tight file permissions, and rotated regularly.
> Generate a fresh `TOOL_API_KEY` with `openssl rand -hex 32`.

**Importance:** Central place to configure the service without touching code.

### `config/kbtool.service` — systemd unit
Defines how the service runs as a managed background process:
- Runs `uvicorn` with 2 workers, bound to **localhost only** (nginx handles the public side).
- Loads secrets from `kbtool.env`.
- **Auto-restarts on crash** with rate limiting.
- Applies **basic hardening** (`PrivateTmp`, `NoNewPrivileges`, `ProtectSystem=strict`).

**Importance:** Makes the service resilient, auto-starting on boot, and operationally
manageable via `systemctl`.

### `config/nginx.conf` — Reverse proxy
A script that writes the nginx site configuration. It:
- Listens on port 80 and proxies the two public routes (`/healthz`, `/get_kb_candidates`)
  to the local uvicorn process.
- Reserves the ACME challenge path for **Let's Encrypt** certificate issuance.
- Returns `404` for all other paths to minimize the attack surface.

**Importance:** Provides the public entry point, TLS termination (via certbot), and a
clean separation between the internet and the app process.

### `openapi/kb_candidates.json` — Tool contract
The **OpenAPI 3.0 specification** the AI agent uses to understand and call the tool. It
documents the request/response schema, the `x-api-key` security scheme, and — crucially —
embeds **behavioral instructions** in the endpoint description telling the agent how to
interpret `spread` and when to ask follow-up questions.

**Importance:** This is the integration contract between the agent platform and the
service. The agent's behavior is driven directly by the descriptions in this file.

### `openapi/test.sh` — Smoke tests
A bash script that exercises the live endpoint end-to-end:
1. Health check.
2. A valid request (expects candidates + spread).
3. A wrong API key (expects `401`).
4. An empty description (expects `400`).

**Importance:** Fast confidence check that a deployment is healthy and auth/validation
behave correctly.

### `scripts/setup.sh` — One-time provisioning
Bootstraps a fresh VM from scratch:
- Installs system packages (Python, nginx, snapd, certbot).
- Creates the Python virtualenv and installs dependencies.
- Installs and enables the systemd service.
- Configures and reloads nginx.
- Obtains a **Let's Encrypt TLS certificate** and enables HTTPS redirect.

**Importance:** Turns a bare VM into a fully running, TLS-secured service in one command.

### `scripts/deploy.sh` — Redeploy
For routine updates: refreshes dependencies, restarts the service, and runs a health
check. Use this after pulling new code.

**Importance:** Safe, repeatable redeploys without re-running full provisioning.

---

## The API

### `GET /healthz`
Liveness probe. Returns:
```json
{ "status": "ok", "tool": "get_kb_candidates" }
```

### `POST /get_kb_candidates`
**Headers:** `x-api-key: <TOOL_API_KEY>`, `Content-Type: application/json`

**Request:**
```json
{ "description": "Outlook crashes when I click send with a PDF attached" }
```

**Response:**
```json
{
  "candidates": [
    {
      "kb_id": "...",
      "kb_article_name": "...",
      "summary": "...",
      "key_symptoms": ["...", "..."],
      "guidance_troubleshoot": "...",
      "score": 2.134
    }
  ],
  "spread": "high",
  "top_score": 2.134
}
```

| Status | Meaning |
|--------|---------|
| `200`  | Candidates returned (may be empty if below confidence threshold) |
| `400`  | Missing `description` field |
| `401`  | Invalid or missing API key |
| `500`  | Search or server error |

---

## Configuration

All settings live in `config/kbtool.env`:

| Variable | Purpose | Default |
|----------|---------|---------|
| `SEARCH_ENDPOINT`  | Azure AI Search service URL | — (required) |
| `SEARCH_INDEX`     | Index name to query | — (required) |
| `SEARCH_API_KEY`   | Azure AI Search admin key | — (required) |
| `TOOL_API_KEY`     | Shared secret for `x-api-key` auth | — (required) |
| `SPREAD_THRESHOLD` | Score gap above which spread is "low" (confident) | `0.6` |
| `MIN_SCORE`        | Minimum top score to return any candidate | `0.1` |
| `TOP_K`            | Results fetched from search | `5` |
| `RETURN_K`         | Candidates returned to the agent | `3` |

---

## Deployment

**First-time setup** (on the target VM — set `DOMAIN` in the script first):
```bash
bash scripts/setup.sh
```

**Subsequent deploys:**
```bash
bash scripts/deploy.sh
```

**Service management:**
```bash
sudo systemctl status kbtool      # check status
sudo systemctl restart kbtool     # restart
journalctl -u kbtool -f           # tail logs
```

---

## Testing

Run the end-to-end smoke tests against the live endpoint:
```bash
bash openapi/test.sh
```
This verifies the health check, a successful query, and the `401`/`400` error paths.
