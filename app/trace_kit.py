"""
trace_kit.py — human-readable, end-to-end tracing core for the KB tool service.

Every HTTP request handled by the service becomes ONE ordered trace block
(all its pipeline steps grouped together, never interleaved with other
requests) and is written to two files inside TRACE_LOG_DIR
(default: <repo>/logs):

    trace_YYYY-MM-DD.log     human-readable, step-by-step execution story
    trace_YYYY-MM-DD.jsonl   the same events as one JSON object per request
                             (for grep / pandas / later analysis)

This module is only consumed by main_traced.py — main.py is never modified.
"""

import json
import os
import re
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime

# ── Output location ────────────────────────────────────────────────
_DEFAULT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
)
LOG_DIR = os.environ.get("TRACE_LOG_DIR", _DEFAULT_DIR)
os.makedirs(LOG_DIR, exist_ok=True)

# Per-turn files live here. One turn = one user question and the one or two
# tool calls (candidates [+ follow-up rounds] and/or guidance) that resolve it.
TURN_DIR = os.path.join(LOG_DIR, "turns")
os.makedirs(TURN_DIR, exist_ok=True)

# A new tool call is treated as a continuation of an open turn when it arrives
# within this many seconds of that turn's last activity (and links to it by
# description overlap or kb_id). Older turns are considered closed.
TURN_IDLE_SECONDS = int(os.environ.get("TRACE_TURN_IDLE_SECONDS", "900"))

_write_lock = threading.Lock()
_current: ContextVar = ContextVar("kb_trace_current", default=None)
_sessions: list = []   # open turns, guarded by _write_lock

RULE_HEAVY = "═" * 100
RULE_LIGHT = "─" * 100


class Trace:
    """Collects all steps of a single request, flushed as one block at the end."""

    def __init__(self, endpoint: str):
        self.id = uuid.uuid4().hex[:8]
        self.endpoint = endpoint
        self.started = datetime.now()
        self.step_no = 0
        self.lines: list[str] = []          # human-readable body
        self.events: list[dict] = []        # structured mirror for the .jsonl
        self.meta: dict = {}                # scratchpad shared between steps

    # ── building the block ────────────────────────────────────────
    def step(self, title: str, **data):
        """Open a new numbered step, e.g. 'AZURE AI SEARCH'."""
        self.step_no += 1
        self.lines.append("")
        self.lines.append(f"STEP {self.step_no} ── {title}")
        self.lines.append(RULE_LIGHT)
        self.events.append({"step": self.step_no, "title": title, **data})

    def log(self, text: str = "", indent: int = 1):
        """Add free-form line(s) under the current step."""
        pad = "    " * indent
        for ln in str(text).splitlines() or [""]:
            self.lines.append(pad + ln)

    def kv(self, key: str, value, indent: int = 1):
        """Add an aligned 'key : value' line."""
        self.log(f"{key:<38}: {value}", indent=indent)

    def note(self, text: str, indent: int = 1):
        """Add an explanatory note (prefixed so it reads as commentary)."""
        self.log(f"ℹ {text}", indent=indent)

    def data(self, **kwargs):
        """Attach structured data to the most recent step (jsonl only)."""
        if self.events:
            self.events[-1].update(kwargs)
        else:
            self.events.append(kwargs)


# ── current-trace handling (contextvar survives the threadpool hop) ─
def start(endpoint: str) -> Trace:
    t = Trace(endpoint)
    t._token = _current.set(t)
    return t


def current() -> Trace | None:
    return _current.get()


def finish(t: Trace, status: int = 200, error: str | None = None):
    """Render the whole block and append it to today's log files."""
    duration_ms = (datetime.now() - t.started).total_seconds() * 1000.0
    ts = t.started.strftime("%Y-%m-%d %H:%M:%S")
    status_txt = f"{status}" + ("  ✗ ERROR" if (error or status >= 400) else "  ✓ OK")

    header = [
        RULE_HEAVY,
        f"TRACE {t.id} │ {ts} │ {t.endpoint} │ status={status_txt} │ {duration_ms:.0f} ms",
        RULE_HEAVY,
    ]
    footer = [""]
    if error:
        footer = ["", f"    ✗ UNHANDLED ERROR: {error}", ""]
    block = "\n".join(header + t.lines + footer) + "\n"

    day = t.started.strftime("%Y-%m-%d")
    log_path = os.path.join(LOG_DIR, f"trace_{day}.log")
    jsonl_path = os.path.join(LOG_DIR, f"trace_{day}.jsonl")

    record = {
        "trace_id": t.id,
        "time": t.started.isoformat(),
        "endpoint": t.endpoint,
        "status": status,
        "duration_ms": round(duration_ms, 1),
        "error": error,
        "events": t.events,
    }

    with _write_lock:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(block)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        # Also append this block to its per-turn file (grouped by conversation).
        try:
            _route_to_turn(t, block, record)
        except Exception as exc:        # tracing must never break the request
            try:
                with open(os.path.join(LOG_DIR, "turn_routing_errors.log"), "a",
                          encoding="utf-8") as f:
                    f.write(f"{ts} {type(exc).__name__}: {exc}\n")
            except Exception:
                pass

    # Persist the KB-selection index (own lock — must run outside the block above).
    if t.meta.get("endpoint_kind") == "candidates" and t.meta.get("candidates_detail"):
        try:
            record_selection(
                t.meta.get("query", ""),
                t.meta.get("spread"),
                t.meta["candidates_detail"],
                t.id,
                t.meta.get("turn_file"),
                t.started,
            )
        except Exception:
            pass

    try:
        _current.reset(t._token)
    except Exception:
        _current.set(None)


# ── Per-turn grouping ──────────────────────────────────────────────
#
# The tool service sees individual HTTP calls, not conversations, and Foundry
# OpenAPI tools don't pass a thread id. So we reconstruct a "turn" with a
# best-effort heuristic that works well for sequential agent runs:
#
#   • get_kb_candidates  → continues the most recent open turn whose description
#     strongly overlaps this one (a follow-up round re-sends the cumulative
#     description); otherwise starts a NEW turn whose file is named after this
#     (initial) description.
#   • get_kb_guidance    → attaches to the most recent open turn whose returned
#     candidates included the requested kb_id; otherwise opens its own turn.
#
# An explicit correlation header wins over the heuristic when present — set
# TRACE_TURN_HEADER (e.g. "x-conversation-id") and configure the agent tool to
# send it, and turns become exact. (meta["turn_key"] carries that value.)

def _slug(text: str, max_words: int = 9, max_len: int = 64) -> str:
    words = re.findall(r"[A-Za-z0-9]+", (text or "").lower())
    s = "-".join(words[:max_words]) or "no-description"
    return s[:max_len].rstrip("-")


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _new_turn(desc: str, now: datetime, turn_key=None) -> dict:
    sid = uuid.uuid4().hex[:6]
    fname = f"turn_{now.strftime('%Y-%m-%d_%H%M%S')}_{sid}_{_slug(desc)}.log"
    s = {
        "id": sid,
        "turn_key": turn_key,
        "tokens": _tokens(desc),
        "initial_description": desc,
        "candidate_kb_ids": set(),
        "file": os.path.join(TURN_DIR, fname),
        "name": fname,
        "created": now,
        "last_activity": now,
        "calls": 0,
    }
    with open(s["file"], "a", encoding="utf-8") as f:
        f.write(RULE_HEAVY + "\n")
        f.write(f"TURN {sid} │ opened {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"initial user description: {short(desc, 240)}\n")
        f.write(RULE_HEAVY + "\n")
    _sessions.append(s)
    return s


def _route_to_turn(t: "Trace", block: str, record: dict):
    """Append the rendered block to the right per-turn file. Caller holds the lock."""
    now = t.started
    kind = t.meta.get("endpoint_kind")          # 'candidates' | 'guidance' | None
    turn_key = t.meta.get("turn_key")            # explicit correlation id, if any

    # drop turns that have gone idle
    global _sessions
    _sessions = [s for s in _sessions
                 if (now - s["last_activity"]).total_seconds() <= TURN_IDLE_SECONDS]

    session = None
    if turn_key:
        session = next((s for s in _sessions if s["turn_key"] == turn_key), None)

    if session is None and kind == "candidates":
        desc = t.meta.get("description", "")
        toks = _tokens(desc)
        best, best_score = None, 0.0
        for s in _sessions:
            sc = _jaccard(toks, s["tokens"])
            if sc > best_score:
                best, best_score = s, sc
        if best is not None and best_score >= 0.5:
            session = best
            session["tokens"] |= toks
        else:
            session = _new_turn(desc, now, turn_key)
        session["candidate_kb_ids"].update(t.meta.get("candidate_kb_ids") or [])

    elif session is None and kind == "guidance":
        kb_id = t.meta.get("kb_id", "")
        for s in reversed(_sessions):
            if kb_id in s["candidate_kb_ids"]:
                session = s
                break
        if session is None:
            # No open candidates turn for this kb_id — name the guidance-only turn
            # after the original query (from the selection index) so it's findable,
            # falling back to the kb_id.
            info = lookup_selection(kb_id)
            label = (info or {}).get("query") or f"guidance {kb_id}"
            session = _new_turn(label, now, turn_key)

    if session is None:
        session = _new_turn(t.endpoint, now, turn_key)

    session["last_activity"] = now
    session["calls"] += 1
    t.meta["turn_file"] = session["name"]
    with open(session["file"], "a", encoding="utf-8") as f:
        f.write(block)


# ── Persistent KB-selection index ──────────────────────────────────
# Records, per kb_id, how get_kb_candidates last surfaced it (score, rank,
# spread, the user query, trace id, turn file). get_kb_guidance reads this so
# its trace can show HOW the KB was selected — even when the candidates call
# happened in an earlier turn, a different worker process, or before a restart.
SELECTION_INDEX = os.path.join(LOG_DIR, "kb_selection_index.json")
_MAX_INDEX = 500


def record_selection(query, spread, candidates, trace_id, turn_file, when):
    """candidates: list of dicts with kb_id/score/rank/guidance_troubleshoot.
    Manages its own lock — call it OUTSIDE _route_to_turn (which holds the lock)."""
    if not candidates:
        return
    with _write_lock:
        try:
            with open(SELECTION_INDEX, encoding="utf-8") as f:
                idx = json.load(f)
        except Exception:
            idx = {}
        all_c = [{"kb_id": x.get("kb_id"), "score": x.get("score")} for x in candidates]
        for c in candidates:
            kb = c.get("kb_id")
            if not kb:
                continue
            idx[kb] = {
                "kb_id": kb,
                "score": c.get("score"),
                "rank": c.get("rank"),
                "guidance_troubleshoot": c.get("guidance_troubleshoot"),
                "spread": spread,
                "query": query,
                "all_candidates": all_c,
                "trace_id": trace_id,
                "turn_file": turn_file,
                "time": when.strftime("%Y-%m-%d %H:%M:%S"),
            }
        if len(idx) > _MAX_INDEX:
            items = sorted(idx.items(), key=lambda kv: kv[1].get("time", ""), reverse=True)[:_MAX_INDEX]
            idx = dict(items)
        tmp = SELECTION_INDEX + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False)
        os.replace(tmp, SELECTION_INDEX)


def lookup_selection(kb_id):
    """Read-only (no lock) so it is safe to call from inside _route_to_turn."""
    try:
        with open(SELECTION_INDEX, encoding="utf-8") as f:
            return json.load(f).get(kb_id)
    except Exception:
        return None


# ── small helpers used by main_traced.py ───────────────────────────
def short(text, limit: int = 160) -> str:
    """One-line preview of arbitrary text."""
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def pretty_json(obj, limit: int = 6000) -> str:
    try:
        s = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    if len(s) > limit:
        s = s[:limit] + f"\n… (truncated, {len(s)} chars total)"
    return s
