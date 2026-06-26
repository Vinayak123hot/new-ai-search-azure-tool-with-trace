# Application Insights / OpenTelemetry edition (parallel, isolated)

This is a **completely separate** observability path that explores Azure-native
tracing with **Application Insights + OpenTelemetry**. It does not change a single
character of the existing readable-log system:

- Untouched: `app/main.py`, `app/main_traced.py`, `app/trace_kit.py`,
  `requirements.txt`, `openapi/*`, and the **teva-kb-trace** App Service.
- New files only: `app/otel_instrumentation.py`, `app/main_otel.py`,
  `requirements-otel.txt`, `tools/otel_appsettings.json`, this README.
- New, separate App Service: **teva-kb-otel** (same plan, no extra compute cost).

You can keep using the kb_trace readable logs exactly as before; this runs alongside.

## What it does

`main_otel.py` imports the unchanged `main.py` and wraps the same pipeline points
as the readable tracer, but emits **OpenTelemetry spans** instead of log lines:

| Span | Key attributes |
|---|---|
| FastAPI request (auto) | http route, status, duration |
| `kb.azure_ai_search` | `kb.search.text`, `kb.search.top`, `kb.search.raw_chunk_count` |
| `kb.dedup` | `kb.dedup.chunks_in/articles_out/kb_ids/top_score` |
| `kb.spread` | `kb.spread.verdict/scores/top/gap/threshold` |
| `kb.discriminating_symptoms` | `kb.symptoms.candidate_count/selected_count/selected` |
| `kb.parse_docx` | `kb.guidance.doc_bytes/title/sections` |

Azure SDK calls (Azure AI Search, Blob, Key Vault) also appear automatically as
**dependency** spans, so one request renders as a full span waterfall.

Telemetry only activates when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set; with
it unset the app behaves identically to the plain service (safe no-op).

## Azure resources created

- Log Analytics workspace: **teva-kb-logs** (RG TevaAgenticAi)
- Application Insights: **teva-kb-insights** (workspace-based)
- App Service: **teva-kb-otel** → `https://teva-kb-otel.azurewebsites.net`
  - startup: `python -m uvicorn app.main_otel:app --host 0.0.0.0 --port 8000`
  - managed identity granted Key Vault Secrets User + Storage Blob Data Reader/Delegator

## How to view traces

Azure Portal → **Application Insights: teva-kb-insights**:

- **Investigate → Transaction search** — click any request to see the span
  waterfall (request → search → dedup → spread → symptoms → blob/docx).
- **Investigate → Application map** — service-to-dependency topology.
- **Monitoring → Logs (KQL)** — query, e.g.:

  ```kusto
  // requests with their pipeline custom dimensions
  dependencies
  | where name startswith "kb."
  | project timestamp, name, duration, customDimensions
  | order by timestamp desc

  // spread verdicts over time
  dependencies
  | where name == "kb.spread"
  | extend verdict = tostring(customDimensions["kb.spread.verdict"]),
           top = todouble(customDimensions["kb.spread.top"])
  | project timestamp, verdict, top
  ```

Note: App Insights ingestion has ~1–3 minutes latency, so traces appear shortly
after traffic, not instantly.

## Redeploy

```bash
# from repo root, build with the OTel requirements as requirements.txt
python - <<'PY'
import os, zipfile
dest = os.path.join(os.environ["TEMP"], "kb-otel-deploy.zip")
if os.path.exists(dest): os.remove(dest)
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.write("requirements-otel.txt", "requirements.txt")
    for root,_,files in os.walk("app"):
        for f in files:
            if f.endswith(".pyc") or "__pycache__" in root: continue
            p=os.path.join(root,f); zf.write(p, p.replace(os.sep,"/"))
PY
az webapp deploy -g TevaAgenticAi -n teva-kb-otel --src-path "$TEMP/kb-otel-deploy.zip" --type zip
```

## Unifying with the Foundry agent trace (optional next step)

Azure AI Foundry tracing is also OpenTelemetry. If Test-agent's tool is pointed at
`teva-kb-otel` and Foundry is connected to the **same** Application Insights
resource, and trace context (`traceparent`) propagates over the tool HTTP call,
the agent run and the tool/search spans collapse into a single end-to-end trace —
no timestamp matching needed. (Not enabled by default; the kb_trace readable-log
path remains the recommended day-to-day debugging view.)

## Teardown (if you stop exploring)

```bash
az webapp delete -g TevaAgenticAi -n teva-kb-otel
az monitor app-insights component delete -g TevaAgenticAi -a teva-kb-insights
az monitor log-analytics workspace delete -g TevaAgenticAi -n teva-kb-logs --yes
```
