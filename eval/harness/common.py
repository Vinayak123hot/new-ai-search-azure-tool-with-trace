"""
common.py — shared infrastructure for the 4-layer evaluation harness.

Everything that is FIXED (does not change when you scale from 8 to 200 docs)
lives here: config loading, the KB tool client, the faithful agent runner
(same model + exact production prompt + clarification simulator), and small
metric helpers. The per-layer scripts only orchestrate and score.

Secrets come from the environment, never from config.json:
  TOOL_API_KEY       x-api-key for get_kb_candidates / get_kb_guidance
  AZURE_OPENAI_KEY   key for the agent layers (3 and 4)

Paths are resolved relative to eval/ so the harness is location-independent.
"""
import json
import os
import random
import re
import sys
import time
import urllib.request

EVAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../eval


def _log(msg: str) -> None:
    """Diagnostics go to stderr so they never pollute the JSON/metrics on stdout."""
    print(msg, file=sys.stderr, flush=True)


# ── config + dataset loading ───────────────────────────────────────
def load_config() -> dict:
    with open(os.path.join(EVAL_DIR, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def load_gold() -> list[dict]:
    with open(os.path.join(EVAL_DIR, "gold_set.json"), encoding="utf-8") as f:
        return json.load(f)["cases"]


def load_kb_meta() -> dict:
    with open(os.path.join(EVAL_DIR, "kb_meta.json"), encoding="utf-8") as f:
        return {m["kb_id"]: m for m in json.load(f)}


# ── KB tool client (layers 1, 2, 4) ────────────────────────────────
class KBTool:
    """Thin client for the deployed tool endpoints."""

    def __init__(self, cfg: dict):
        self.endpoint = cfg["endpoint"].rstrip("/")
        self.key = os.environ.get("TOOL_API_KEY", "")

    MAX_ATTEMPTS = 4

    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        last_err = None
        for attempt in range(self.MAX_ATTEMPTS):
            # build a fresh Request per attempt (urllib mutates state on send)
            req = urllib.request.Request(f"{self.endpoint}{path}", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            if self.key:
                req.add_header("x-api-key", self.key)
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                last_err = e
                if attempt < self.MAX_ATTEMPTS - 1:
                    # exponential backoff with jitter: ~1s, 2s, 4s (+/- jitter)
                    delay = (2 ** attempt) + random.uniform(0, 0.5)
                    _log(f"  (!) {path} attempt {attempt + 1}/{self.MAX_ATTEMPTS} failed: {e} "
                         f"- retrying in {delay:.1f}s")
                    time.sleep(delay)
        _log(f"  (!) {path} FAILED after {self.MAX_ATTEMPTS} attempts: {last_err}")
        return {"_error": str(last_err)}

    def candidates(self, description: str) -> dict:
        return self._post("/get_kb_candidates", {"description": description})

    def guidance(self, kb_id: str) -> dict:
        return self._post("/get_kb_guidance", {"kb_id": kb_id})


# ── Agent runner (layers 3, 4): faithful reconstruction ─────────────
# Same model, the EXACT production prompt, real tools, and a data-driven
# user simulator. This is the one place that talks to Azure OpenAI.
KB_RE = re.compile(r"KB_Article_name\s*:\s*(KB\d+)", re.IGNORECASE)


def _tools_spec():
    return [
        {"type": "function", "function": {
            "name": "get_kb_candidates",
            "description": "Retrieve candidate KB articles for an Outlook issue/task description.",
            "parameters": {"type": "object", "properties": {
                "description": {"type": "string"}}, "required": ["description"]}}},
        {"type": "function", "function": {
            "name": "get_kb_guidance",
            "description": "Fetch resolution steps for a KB article by kb_id.",
            "parameters": {"type": "object", "properties": {
                "kb_id": {"type": "string"}}, "required": ["kb_id"]}}},
    ]


def simulated_answer(case: dict, question: str, kb_meta: dict) -> str:
    """Data-driven user simulator. Answers the agent's follow-up using the
    case's dialog.answers keyword map first, then generic baseline questions,
    then the expected KB's symptoms. Adding cases needs NO change here."""
    q = (question or "").lower()
    answers = (case.get("dialog") or {}).get("answers") or {}
    for keyword, reply in answers.items():
        if keyword.lower() in q:
            return reply
    if any(w in q for w in ("laptop", "mobile", "desktop", "device", "machine")):
        return "On a laptop/desktop."
    if any(w in q for w in ("from when", "since when", "when did", "started", "how long")):
        return "It started recently."
    kb = case.get("expected_kb")
    if kb and kb in kb_meta and kb_meta[kb].get("symptoms"):
        return "Yes. " + " ".join(kb_meta[kb]["symptoms"][:2])
    return "It's not related to those; nothing more to add."


class AgentRunner:
    """Drives the reconstructed classification agent end to end and records a
    structured transcript so any layer can inspect tool calls, spread, the
    follow-up questions asked, and the final classification."""

    def __init__(self, cfg: dict, kb_meta: dict, tool: "KBTool"):
        from openai import AzureOpenAI                      # lazy: only layers 3/4 need it
        a = cfg["agent"]
        self.deploy = os.environ.get("AOAI_DEPLOYMENT", a["deployment"])
        self.max_rounds = a["max_rounds"]
        self.max_steps = a["max_steps"]
        self.kb_meta = kb_meta
        self.tool = tool
        instr_path = os.path.join(EVAL_DIR, a["instructions_file"])
        with open(instr_path, encoding="utf-8") as f:
            self.instructions = f.read()
        self.client = AzureOpenAI(
            azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT", a["azure_openai_endpoint"]),
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version=a["api_version"],
        )

    def run(self, case: dict) -> dict:
        messages = [{"role": "system", "content": self.instructions},
                    {"role": "user", "content": case["query"]}]
        tools = _tools_spec()
        rounds = 0
        predicted = None
        final_text = ""
        tool_events = []          # [{name, request, response}]
        followups = []            # clarification questions the agent asked the user

        for _ in range(self.max_steps):
            kwargs = dict(model=self.deploy, messages=messages, tools=tools)
            if "gpt-5" not in self.deploy:                 # gpt-5.x rejects temperature != 1
                kwargs["temperature"] = 0
            msg = self.client.chat.completions.create(**kwargs).choices[0].message

            # capture a classification emitted in ANY assistant turn
            if msg.content:
                m = KB_RE.search(msg.content)
                if m:
                    predicted = m.group(1).upper()
                    final_text = msg.content
                    break

            if msg.tool_calls:
                messages.append({"role": "assistant", "content": msg.content or None,
                                 "tool_calls": [{"id": tc.id, "type": "function",
                                                 "function": {"name": tc.function.name,
                                                              "arguments": tc.function.arguments}}
                                                for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        args = {}
                    if tc.function.name == "get_kb_candidates":
                        resp = self.tool.candidates(args.get("description", ""))
                    else:
                        resp = self.tool.guidance(args.get("kb_id", ""))
                    tool_events.append({"name": tc.function.name, "request": args, "response": resp})
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps(resp)})
                continue

            # assistant text with no classification -> it's a clarification question
            final_text = msg.content or ""
            messages.append({"role": "assistant", "content": final_text})
            m = KB_RE.search(final_text)
            if m:
                predicted = m.group(1).upper()
                break
            if rounds >= self.max_rounds:
                break
            followups.append(final_text)
            rounds += 1
            messages.append({"role": "user",
                             "content": simulated_answer(case, final_text, self.kb_meta)})

        return {
            "predicted": predicted,
            "rounds": rounds,
            "followups": followups,
            "tool_events": tool_events,
            "called_guidance": any(e["name"] == "get_kb_guidance" for e in tool_events),
            "final_excerpt": (final_text or "")[:240].replace("\n", " "),
        }


# ── small shared helpers ────────────────────────────────────────────
def pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))
