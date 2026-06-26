"""
main_otel.py — Application Insights / OpenTelemetry edition of the KB tool service.

PARALLEL and ISOLATED. It imports the unchanged app/main.py and adds OpenTelemetry
spans so the full path (HTTP request → Azure AI Search → dedup → spread → symptom
selection → guidance/docx) shows up as ONE distributed trace in Azure Application
Insights. It does NOT import or touch trace_kit.py / main_traced.py and writes no
local trace files — that system keeps running independently on teva-kb-trace.

Run it on its own App Service (teva-kb-otel):
    uvicorn app.main_otel:app --host 0.0.0.0 --port 8000

Telemetry only activates when APPLICATIONINSIGHTS_CONNECTION_STRING is set;
otherwise this behaves exactly like the plain service.
"""

import os

try:                       # repo root:  uvicorn app.main_otel:app
    from app import main
    from app import otel_instrumentation as otel
except ImportError:        # app dir:    uvicorn main_otel:app
    import main
    import otel_instrumentation as otel

# The FastAPI app already exists (created during `import main`), so configure
# telemetry now and then instrument that specific app instance.
otel.init(service_name=os.environ.get("OTEL_SERVICE_NAME", "teva-kb-otel"))

app = main.app
otel.instrument_fastapi(app)


# ── Custom pipeline spans (only emit when telemetry is enabled) ─────
# Same monkeypatch points as the readable tracer, but here they attach
# OpenTelemetry attributes instead of writing log lines. Runs in this
# process only (teva-kb-otel); teva-kb-trace is unaffected.

_orig_search = main.search.search
def _search(*args, **kwargs):
    with otel.span("kb.azure_ai_search") as sp:
        results = list(_orig_search(*args, **kwargs))
        otel.set_attrs(
            sp,
            **{
                "kb.search.text": str(kwargs.get("search_text", ""))[:200],
                "kb.search.top": kwargs.get("top"),
                "kb.search.raw_chunk_count": len(results),
            },
        )
        return results
main.search.search = _search


_orig_dedupe = main.dedupe_by_kb
def _dedupe(rows):
    with otel.span("kb.dedup") as sp:
        out = _orig_dedupe(rows)
        otel.set_attrs(
            sp,
            **{
                "kb.dedup.chunks_in": len(rows),
                "kb.dedup.articles_out": len(out),
                "kb.dedup.kb_ids": [c.get("kb_id", "") for c in out],
                "kb.dedup.top_score": out[0]["score"] if out else 0.0,
            },
        )
        return out
main.dedupe_by_kb = _dedupe


_orig_spread = main.compute_spread
def _spread(scores):
    with otel.span("kb.spread") as sp:
        res = _orig_spread(scores)
        attrs = {
            "kb.spread.verdict": res,
            "kb.spread.scores": [float(s) for s in scores],
            "kb.spread.high_confidence": main.HIGH_CONFIDENCE,
            "kb.spread.threshold": main.SPREAD_THRESHOLD,
        }
        if scores:
            attrs["kb.spread.top"] = float(scores[0])
            if len(scores) > 1:
                attrs["kb.spread.gap"] = round(float(scores[0]) - float(scores[1]), 3)
        otel.set_attrs(sp, **attrs)
        return res
main.compute_spread = _spread


_orig_select = main.select_discriminating_symptoms
def _select(description, candidates, top_k=3):
    with otel.span("kb.discriminating_symptoms") as sp:
        res = _orig_select(description, candidates, top_k=top_k)
        otel.set_attrs(
            sp,
            **{
                "kb.symptoms.candidate_count": len(candidates),
                "kb.symptoms.selected_count": len(res),
                "kb.symptoms.selected": res,
            },
        )
        return res
main.select_discriminating_symptoms = _select


_orig_parse = main._parse_kb_docx
def _parse(doc_bytes):
    with otel.span("kb.parse_docx") as sp:
        parsed = _orig_parse(doc_bytes)
        otel.set_attrs(
            sp,
            **{
                "kb.guidance.doc_bytes": len(doc_bytes),
                "kb.guidance.title": parsed.get("title"),
                "kb.guidance.sections": len(parsed.get("sections") or []),
            },
        )
        return parsed
main._parse_kb_docx = _parse
