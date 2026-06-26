"""
otel_instrumentation.py — OpenTelemetry / Azure Application Insights setup.

This file is COMPLETELY SEPARATE from the readable-log tracer
(trace_kit.py / main_traced.py). It is imported only by main_otel.py and
changes nothing about the existing kb_trace app.

If APPLICATIONINSIGHTS_CONNECTION_STRING is not set, every function here is a
safe no-op, so the app still starts and behaves normally without telemetry.
"""

import os
from contextlib import contextmanager

_ENABLED = False
_tracer = None


def init(service_name: str = "teva-kb-otel") -> bool:
    """Configure Azure Monitor (App Insights) export via OpenTelemetry.
    Returns True when telemetry is active, False when disabled/unavailable."""
    global _ENABLED, _tracer
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not conn:
        print("OTEL: APPLICATIONINSIGHTS_CONNECTION_STRING not set — telemetry disabled")
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry import trace
        os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
        # azure SDK calls (Search/Blob/Key Vault) become dependency spans too.
        configure_azure_monitor(connection_string=conn)
        _tracer = trace.get_tracer("kb.pipeline")
        _ENABLED = True
        print(f"OTEL: Azure Monitor configured for service '{service_name}'")
    except Exception as e:           # never let telemetry break the app
        print(f"OTEL: init failed ({type(e).__name__}: {e}) — telemetry disabled")
        return False
    return True


def instrument_fastapi(app) -> None:
    """Explicitly instrument the already-created FastAPI app so each HTTP request
    becomes a server span (the app is created before init(), so the distro's
    auto-instrumentation would otherwise miss it)."""
    if not _ENABLED:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        print("OTEL: FastAPI request instrumentation enabled")
    except Exception as e:
        print(f"OTEL: FastAPI instrumentation skipped ({type(e).__name__}: {e})")


def enabled() -> bool:
    return _ENABLED


def _coerce(v):
    """OTel attribute values must be str/bool/int/float (or sequences of those)."""
    if isinstance(v, bool) or isinstance(v, (int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [x if isinstance(x, (str, bool, int, float)) else str(x) for x in v]
    return str(v)


@contextmanager
def span(name: str, **attrs):
    """Open a child span (nested under the current request span). No-op when disabled."""
    if not _ENABLED or _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            try:
                sp.set_attribute(k, _coerce(v))
            except Exception:
                pass
        yield sp


def set_attrs(sp, **attrs) -> None:
    """Add attributes to an open span. Safe when sp is None (telemetry disabled)."""
    if sp is None:
        return
    for k, v in attrs.items():
        if v is None:
            continue
        try:
            sp.set_attribute(k, _coerce(v))
        except Exception:
            pass
