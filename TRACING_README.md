# End-to-End Trace Tooling

Human-readable, step-by-step execution traces for the KB classification pipeline —
**without any change to `app/main.py`**.

## Deployment model: separate trace App Service (per-agent tracing)

The traced app runs on its **own always-on App Service** (`teva-kb-trace`), created
next to the production `teva-kb-candidate` on the same plan by
`tools/deploy_trace_service.ps1`. Only **Test-agent**'s OpenAPI tool points at the
trace service (`openapi/kb_candidates_trace.json` — identical spec, different server
URL, same `x-api-key`); every other agent keeps using the untouched production
service. Result: every run of Test-agent, by anyone, is traced — nothing else is.

```
Test-agent (Foundry) ──▶ https://teva-kb-trace…azurewebsites.net   (traced, logs kept)
all other agents     ──▶ https://teva-kb-candidate…azurewebsites.net (untouched)
```

**Browser log viewer** — no Kudu needed:

- `https://<trace-host>/trace?key=<tool-api-key>` — newest-first, one card per
  request, day navigation, optional `&refresh=10` auto-reload while testing
- `https://<trace-host>/trace/raw?key=<tool-api-key>&day=YYYY-MM-DD` — plain-text file

The key defaults to the tool API key from Key Vault; set a `TRACE_VIEW_KEY` app
setting to use a separate, shareable viewer key instead. Trace files persist under
`/home/LogFiles/kbtrace`.

The system has two halves, and there is one tool for each:

| Half | What happens there | Tool | Output |
|---|---|---|---|
| **Tool service** (this repo, App Service) | search → dedup → confidence gate → spread → symptom selection → guidance/blob/docx | `app/main_traced.py` | `logs/trace_YYYY-MM-DD.log` (+ `.jsonl`) |
| **Foundry agent** | which tool it calls, with what arguments, what follow-up questions it asks the user | `tools/foundry_trace.py` | console or a text file |

---

## 1. Local pipeline trace (`main_traced.py`)

### How to run

`main_traced.py` imports the unchanged `main.py` app and transparently wraps its
functions. Behavior, endpoints, auth — everything stays identical. Just point
uvicorn at it instead of `main:app`:

```bash
# local dev
uvicorn app.main_traced:app --host 0.0.0.0 --port 8000

# Azure App Service: change startup.sh to
/home/site/wwwroot/pythonenv3.11/bin/uvicorn app.main_traced:app --host 0.0.0.0 --port 8000 --workers 2 --log-level info
```

Trace files are written to `logs/` next to `app/` (override with the
`TRACE_LOG_DIR` environment variable — on App Service set it to
`/home/site/wwwroot/logs` or `/home/LogFiles/kbtrace` so the files persist and
are downloadable via Kudu).

Two files per day:

- `trace_YYYY-MM-DD.log` — the human-readable story (open in any editor)
- `trace_YYYY-MM-DD.jsonl` — one JSON object per request with the same data,
  for grep/pandas/Excel analysis

To revert to non-traced operation, point uvicorn back at `app.main:app`. Nothing else to undo.

### What one request looks like in the log

Every tool call from the agent becomes **one block** — steps never interleave
between concurrent requests:

```
════════════════════════════════════════════════════════════════════════════════
TRACE 8ef3b4b0 │ 2026-06-12 10:31:02 │ POST /get_kb_candidates │ status=200 ✓ OK │ 412 ms
════════════════════════════════════════════════════════════════════════════════

STEP 1 ── AGENT → TOOL CALL RECEIVED: get_kb_candidates
    x-api-key                             : present (a1b2…)
    request body:
        { "description": "Outlook is not working" }

STEP 2 ── AZURE AI SEARCH — HYBRID RETRIEVAL + SEMANTIC RE-RANK
    index                                 : kb-index
    search_text (keyword/BM25 part)       : 'Outlook is not working'
    query_type                            : QueryType.SEMANTIC
    vector_query[1]                       : fields=content_embedding, k_nearest_neighbors=20
    top (max chunks requested)            : 10
    ℹ How ranking works: keyword (BM25) + vector results are fused by RRF into
      '@search.score'; the semantic re-ranker then re-scores ... into '@search.reranker_score'.
    RAW CHUNKS RETURNED: 10
    # 1  kb_id=KB0010265     reranker=2.84 base=0.031  → score_used=2.84
          symptoms: ['Outlook stuck on loading screen', ...]
          text: "When Outlook fails to launch ..."
    # 2  kb_id=KB0010265     reranker=2.41 ...
    ...

STEP 3 ── DEDUP — COLLAPSE CHUNKS INTO DISTINCT KB ARTICLES
    KB0010265      3 chunk(s) → kept best score 2.84, dropped 2 chunk(s) with scores [2.41, 1.97]
    KB0010312      1 chunk(s) → kept best score 2.50
    RESULT: 10 chunks → 4 distinct KB articles (order: ['KB0010265', 'KB0010312', ...])

STEP 4 ── CONFIDENCE GATE (MIN_SCORE) + TRUNCATION (RETURN_K)
    top score after dedupe                : 2.84
    MIN_SCORE threshold                   : 1.9
    gate verdict                          : PASS → continue
    RETURN_K (max candidates to agent)    : 3 → agent will see ['KB0010265', 'KB0010312', 'KB0010401']

STEP 5 ── SPREAD — RETRIEVAL-CONFIDENCE DECISION
    scores of returned candidates         : [2.84, 2.5, 2.1]
    check 1 · weak absolute match         : top 2.84 < HIGH_CONFIDENCE 3.0 ? → True
    check 2 · weak dominance              : gap #1−#2 = 0.34 < SPREAD_THRESHOLD 0.4 ? → True
    spread verdict                        : HIGH
    ℹ HIGH = retrieval is NOT confident. Per agent protocol (Phase 2c) the agent
      MUST ask 1–3 follow-up questions before classifying ...

STEP 6 ── DISCRIMINATING-SYMPTOM SELECTION (spread was HIGH)
    description tokens (after stopword filter): ['outlook', 'working']
    #1  "Outlook stuck on loading screen at startup"
         carried by: KB0010265 (2.84)
         rel=0.21  mass=0.39  dist=0.77  →  final=0.49
    #2  "Emails stuck in outbox" ...
    NEAR-DUPLICATE FILTER (greedy, cosine > 0.85 vs already-selected):
    KEEP  "Outlook stuck on loading screen at startup"
    SKIP  "Outlook hangs on loading screen"  (cosine 0.91 with "Outlook stuck on loading...")
    SELECTED (3): [...]

STEP 7 ── TOOL → AGENT RESPONSE (HTTP 200)
    { "candidates": [...], "spread": "high", "top_score": 2.84, "discriminating_symptoms": [...] }

STEP 8 ── WHAT HAPPENS NEXT (per classification-agent protocol)
    spread=HIGH with candidates ['KB0010265', 'KB0010312', 'KB0010401'].
    → Agent MUST run a clarification round (Phase 2c): it will ask 1–3 follow-up
      questions grounded ONLY in the returned discriminating_symptoms: ...
```

A `get_kb_guidance` call gets the same treatment: request → blob download size →
parsed docx structure (title / note / every section with every step) → SAS link
status → response → "agent will render N sections / M steps, then
`Call Orchestrator: True`".

### What each step explains (mapped to your questions)

- **which tool the agent calls** → block header + STEP 1 (endpoint, body, API-key presence)
- **how many chunks retrieved** → STEP 2 (`RAW CHUNKS RETURNED`, per-chunk rank/kb_id/text)
- **how re-ranking works** → STEP 2 shows both `@search.score` (BM25+vector RRF fusion)
  and `@search.reranker_score` (semantic re-ranker, 0–4) per chunk, and which one was used
- **how dedup is done** → STEP 3 (chunks grouped per kb_id, kept vs dropped scores)
- **confidence gating** → STEP 4 (top score vs MIN_SCORE, RETURN_K truncation)
- **how spread (follow-up trigger) is decided** → STEP 5 (both checks with numbers)
- **how symptoms-vs-description similarity is calculated** → STEP 6
  (description tokens, per-symptom `rel`/`mass`/`dist`/`final`, near-duplicate filter decisions)
- **how the follow-up question comes about** → STEP 8 interprets the response
  against the agent protocol (HIGH → mandatory clarification round grounded in the
  selected symptoms; LOW → Phase 3 hand-off, and whether a `get_kb_guidance` call
  is expected next)

---

## 2. Foundry agent trace (`tools/foundry_trace.py`)

Shows the agent half: the actual conversation (including the exact follow-up
questions the agent asked) and every tool call with arguments and raw output.

```bash
pip install azure-ai-projects azure-identity
az login
set FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>

python tools/foundry_trace.py                 # list 10 newest threads
python tools/foundry_trace.py thread_abc123   # full readable trace of one conversation
python tools/foundry_trace.py thread_abc123 -o conversation.txt
```

The endpoint is on the Foundry portal → your project → **Overview → Azure AI
Foundry project endpoint**. Auth uses `DefaultAzureCredential` (your `az login`),
so no keys need to be stored.

**Correlating the two halves:** each Foundry tool call's `description` argument
appears verbatim as STEP 1 of a block in `logs/trace_*.log` — match by that
string and the timestamp.
