# Outlook Classification Agent — Codebase Guide

A short, file-by-file reference for the **Outlook KB Classification Agent** system, intended
to explain the code and JSON structure to stakeholders before the code is moved to the
client environment.

---

## What the system does (1-minute overview)

A support user describes an Outlook problem or how-to. A **classification agent** (hosted in
Azure AI Foundry, driven by the system prompt in `eval/agent_instructions.txt`) gathers just
enough detail and calls one tool — **`get_kb_candidates`** — to find the single best-matching
KB article, then hands the result off. It does **not** troubleshoot; it only classifies and
routes.

```
User ─▶ Classification Agent (Foundry)
              │  calls the tool (OpenAPI, /openapi)
              ▼
        get_kb_candidates  (FastAPI service, /app)
              │  Azure AI Search: keyword + vector + semantic re-rank
              ▼
        returns: followup_required / kb_id / top_score / discriminating_symptoms / message
              │
   ┌──────────┴───────────┐
   │ confident → resolve   │  ambiguous → ask one follow-up, call again
   └──────────────────────┘
```

Supporting pieces: **`/eval`** measures accuracy against a labelled gold set; **`/tools`** and
**`/openapi`** handle deployment, observability, and the agent↔tool contract.

The four folders, at a glance:

| Folder | Role | Deployed? |
|---|---|---|
| `app/` | The runtime tool service (FastAPI) + optional tracing variants | **Yes** |
| `openapi/` | The OpenAPI tool contract the Foundry agent registers | config (used by Foundry) |
| `tools/` | Deployment scripts, App Service settings, agent-side trace viewer | No (operational helpers) |
| `eval/` | Offline accuracy evaluation (gold set, harness, reports) | No (test/QA only) |

---

## `/app` — the runtime tool service

| File | Purpose |
|---|---|
| **`main.py`** | The production tool service (FastAPI). Exposes `POST /get_kb_candidates` (Azure AI Search hybrid keyword+vector+semantic retrieval → score-band routing: confident **resolve**, mid-band **ask follow-up**, low **no-match**, with a per-session round cap), plus `POST /debug_search` and `GET /healthz`. Returns `followup_required / kb_id / top_score / discriminating_symptoms / message`. **This is the core service.** |
| **`main_traced.py`** | A drop-in wrapper that imports `main.py` unchanged and adds per-request **human-readable tracing** plus a browser viewer at `/trace`. Run this instead of `main:app` to get end-to-end traces; the API is identical. |
| **`trace_kit.py`** | The tracing engine used by `main_traced.py`. Groups every request into one ordered trace block and writes `trace_YYYY-MM-DD.log` (readable) + `.jsonl` (machine) + per-conversation "turn" files; powers the `/trace` viewer. |
| **`main_otel.py`** | Alternative wrapper that adds **OpenTelemetry → Azure Application Insights** distributed tracing (independent of the readable-log tracer). Activates only when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set. |
| **`otel_instrumentation.py`** | OpenTelemetry / Azure Monitor setup helper imported by `main_otel.py` (safe no-op when telemetry isn't configured). |
| **`requirements.txt`** | Python dependencies for the service (FastAPI, uvicorn, azure-search-documents, azure-identity, azure-keyvault-secrets, …). |
| `main.py.bak-*` | Local timestamped backup of `main.py`. **Not part of the app — can be excluded when moving.** |

---

## `/openapi` — the agent↔tool contract

| File | Purpose |
|---|---|
| **`kb_candidates.json`** | OpenAPI 3.0 spec that registers `get_kb_candidates` as the agent's tool (request/response schema, `x-api-key` auth). This is what Foundry imports to let the agent call the service. |
| **`kb_candidates_trace.json`** | Same contract, but the `servers` URL points at the **tracing** deployment (`teva-kb-trace`) so the agent's calls are captured by the trace viewer. |
| **`test.sh`** | `curl` smoke tests for the endpoints (health check, a valid call, wrong-key → 401, empty body → 400). |

---

## `/tools` — deployment & observability helpers (not runtime)

| File | Purpose |
|---|---|
| **`deploy_trace_service.ps1`** | Provisions a separate always-on App Service running the **traced** app (`main_traced:app`) alongside the main service — clones the plan, app settings, and managed-identity permissions — and writes `openapi/kb_candidates_trace.json`. |
| **`foundry_trace.py`** | Prints a readable transcript of a Foundry **agent** conversation (the agent-side half: user messages, follow-up questions, and each tool call with arguments + output) for debugging. |
| **`trace_appsettings.json`** | App Service environment settings for the **trace** service (search/vault/threshold config + `TRACE_LOG_DIR`). |
| **`otel_appsettings.json`** | App Service environment settings for the **OpenTelemetry / App Insights** variant. |

---

## `/eval` — offline accuracy evaluation (test/QA, not deployed)

**Knowledge base & dataset**

| File | Purpose |
|---|---|
| **`kb_docs/*.docx`** (8) | The source KB articles — the actual knowledge-base content the agent classifies against. |
| **`parse_kb_docs.py`** | Parses the `.docx` files into `kb_meta.json` (kb_id, title, symptoms, guidance flag, environment). |
| **`kb_meta.json`** | The parsed KB catalogue — ground-truth metadata for the 8 articles. |
| **`gold_set.json`** | The labelled test set: ~83 user utterances tagged `strong / weak / ambiguous / out_of_kb` with the expected KB. **The evaluation dataset.** |
| `gold_set_v1_backup.json` | Earlier version of the gold set (backup). |
| `_testagent_v41_backup.json` | Backup snapshot of the agent definition/config (reference). |
| **`config.json`** | Eval configuration: score thresholds (mirrors the service), the threshold sweep, CI **gate** targets, and model/endpoint settings. |

**Evaluators**

| File | Purpose |
|---|---|
| **`eval_retrieval.py`** | Tool-level eval — calls `get_kb_candidates` over the gold set and reports top-1, recall@K, out-of-KB rejection, guidance correctness, and a confusion list. Deterministic (no LLM). → `results_retrieval.json` |
| **`eval_agent.py`** | End-to-end eval against the **live Foundry agent** (opens threads, runs the clarification loop, parses the final KB). → `results_agent.json` |
| **`eval_agent_local.py`** | Faithful **local** reconstruction of the agent (same model + exact prompt + live tool + clarification simulator) — used when the Foundry tool-connection isn't available. → `results_agent_local.json` |
| **`foundry_eval.py`** | Publishes the eval results to the **Azure AI Foundry portal** Evaluation tab with custom evaluators. |
| `foundry_eval_data.jsonl`, `foundry_portal_dataset.jsonl` | Datasets formatted for the Foundry portal evaluation. |
| `results_retrieval.json`, `results_agent.json`, `results_agent_local.json` | Saved outputs of the three evaluators above. |

**4-layer harness (`eval/harness/`)**

| File | Purpose |
|---|---|
| **`run_all.py`** | Orchestrates the 4-layer evaluation, applies the CI **gate**, and writes `eval_results.json` + `EVAL_REPORT_4LAYER.md` (supports multi-run averaging for the model-dependent layers). |
| **`common.py`** | Shared infrastructure: config/dataset loading, the KB tool client (with retry/backoff), the faithful `AgentRunner` (model + prompt + tools + user simulator), and metric helpers. |
| `layer1_retrieval.py` | **Layer 1** — retrieval quality (recall@K, top-1, MRR). Tool-only, deterministic. |
| `layer2_threshold.py` | **Layer 2** — threshold calibration (display precision/recall, out-of-KB rejection, threshold sweep). |
| `layer3_clarification.py` | **Layer 3** — clarification quality on ambiguous cases (does it ask grounded follow-ups, and does asking help). |
| `layer4_end_to_end.py` | **Layer 4** — full-conversation outcome (end-to-end top-1, out-of-KB correct, guidance routing). |
| `preflight_layers34.py` | Offline dry-run that verifies the Layer 3 & 4 plumbing with a **mocked** LLM/tool — proves the harness works without spending model quota. |
| `README.md` | How to run the harness. |

**Reports, prompt & logs**

| File | Purpose |
|---|---|
| **`agent_instructions.txt`** | The **production classification-agent system prompt** — the full behaviour spec (intake-only role, phases, follow-up rules, output blocks). The single most important behavioural document. |
| `EVAL_REPORT.md` | Baseline retrieval + agent accuracy report (8-doc set). |
| `EVAL_REPORT_4LAYER.md` | Generated 4-layer gate/metrics report. |
| `eval_results.json` | Machine-readable 4-layer results (all layers + gate). |
| `explain_eval.md` | Plain-language guide to the 4-layer evaluation strategy (for explaining methodology to stakeholders). |
| `run_testagent.log`, `full_run.log` | Run logs (artifacts; **not required** to move). |

---

## Housekeeping notes (before client handoff)

- **Safe to exclude** when moving (backups / logs, not part of the system): `app/main.py.bak-*`,
  `eval/*.log`, `eval/*_backup.json`.
- **Version drift to reconcile:** `openapi/*.json` and `tools/*_appsettings.json` were authored
  for the **earlier** service contract (response fields `spread` / `candidates[]`, and env vars
  `MIN_SCORE` / `HIGH_CONFIDENCE` / `BLOB_*`). The current `app/main.py` uses the **score-band**
  contract (`followup_required` + `CONFIDENT_SCORE` / `FOLLOWUP_FLOOR` / `MIN_DISPLAY_SCORE`).
  Regenerate these to match `main.py` before the client registers the tool.
- **Secrets:** none are stored in the repo. The service reads `search-api-key` / `tool-api-key`
  from Azure Key Vault at runtime; `*_appsettings.json` contain only non-secret configuration.
