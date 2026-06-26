"""
eval_agent_local.py — faithful end-to-end eval of the classification agent,
reconstructed locally: same model (gpt-4.1-mini), the EXACT instructions pulled
from the Foundry agent (agent_instructions.txt), and function-calling wired to
the live KB endpoint. Avoids the broken Foundry tool-connection and the
inaccessible nextgen agent, while reproducing prompt + model + clarification loop.

Env:
  AZURE_OPENAI_ENDPOINT  (default https://teva.openai.azure.com)
  AZURE_OPENAI_KEY       (required)
  AOAI_DEPLOYMENT        (default gpt-4.1-mini)
  TOOL_API_KEY           (KB tool key)
  EVAL_ENDPOINT          (default https://teva-kb-trace.azurewebsites.net)
  EVAL_LIMIT             (optional: only run first N cases, for a smoke test)
"""
import json
import os
import re
import time
import urllib.request

from openai import AzureOpenAI

HERE = os.path.dirname(os.path.abspath(__file__))
AOAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "https://teva.openai.azure.com")
AOAI_KEY = os.environ["AZURE_OPENAI_KEY"]
DEPLOY = os.environ.get("AOAI_DEPLOYMENT", "gpt-4.1-mini")
KB_ENDPOINT = os.environ.get("EVAL_ENDPOINT", "https://teva-kb-trace.azurewebsites.net").rstrip("/")
TOOL_KEY = os.environ.get("TOOL_API_KEY", "")
LIMIT = int(os.environ.get("EVAL_LIMIT", "0"))
MAX_ROUNDS = 5          # max simulated user clarification answers (baseline + follow-ups)
MAX_STEPS = 16          # safety cap on model<->tool turns per case

client = AzureOpenAI(azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_KEY, api_version="2024-12-01-preview")
INSTR_FILE = os.environ.get("INSTR_FILE", "agent_instructions.txt")
INSTRUCTIONS = open(os.path.join(HERE, INSTR_FILE), encoding="utf-8").read()
META = {m["kb_id"]: m for m in json.load(open(os.path.join(HERE, "kb_meta.json"), encoding="utf-8"))}
KB_RE = re.compile(r"KB_Article_name\s*:\s*(KB\d+)", re.IGNORECASE)

TOOLS = [
    {"type": "function", "function": {
        "name": "get_kb_candidates",
        "description": "Retrieve candidate KB articles for an Outlook issue/task description.",
        "parameters": {"type": "object", "properties": {
            "description": {"type": "string", "description": "Enhanced issue/task description"}},
            "required": ["description"]}}},
    {"type": "function", "function": {
        "name": "get_kb_guidance",
        "description": "Fetch resolution steps for a KB article by kb_id (only when guidance_troubleshoot=true).",
        "parameters": {"type": "object", "properties": {
            "kb_id": {"type": "string"}}, "required": ["kb_id"]}}},
]


def call_tool(name, args):
    path = "/get_kb_candidates" if name == "get_kb_candidates" else "/get_kb_guidance"
    body = json.dumps(args).encode("utf-8")
    req = urllib.request.Request(f"{KB_ENDPOINT}{path}", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if TOOL_KEY:
        req.add_header("x-api-key", TOOL_KEY)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        return json.dumps({"error": str(e)})


def simulated_answer(expected_kb, question=""):
    q = (question or "").lower()
    if any(w in q for w in ("laptop", "mobile", "desktop", "device")):
        return "On a laptop/desktop."
    if any(w in q for w in ("from when", "since when", "when did", "started", "how long")):
        return "It started yesterday."
    if expected_kb and expected_kb in META and META[expected_kb]["symptoms"]:
        return "Yes. " + " ".join(META[expected_kb]["symptoms"][:2])
    return "It's not related to those; nothing more to add."


def run_case(case):
    expected = case.get("expected_kb")
    messages = [{"role": "system", "content": INSTRUCTIONS},
                {"role": "user", "content": case["query"]}]
    answers = 0
    predicted = None
    final_text = ""
    for _ in range(MAX_STEPS):
        kwargs = dict(model=DEPLOY, messages=messages, tools=TOOLS)
        if "gpt-5" not in DEPLOY:        # gpt-5.x reasoning models reject temperature!=1
            kwargs["temperature"] = 0
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        # Capture the Phase 3 classification from ANY assistant turn — even one
        # that ALSO calls get_kb_guidance in the same message (content + tool_calls).
        if msg.content:
            mm = KB_RE.search(msg.content)
            if mm:
                predicted = mm.group(1).upper()
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
                result = call_tool(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            continue

        # assistant produced text
        final_text = msg.content or ""
        messages.append({"role": "assistant", "content": final_text})
        m = KB_RE.search(final_text)
        if m:
            predicted = m.group(1).upper()
            break
        # it's a clarification question -> answer it, then keep looping so the
        # model can emit the Phase 3 block after the last answer
        if answers >= MAX_ROUNDS:
            break
        answers += 1
        messages.append({"role": "user", "content": simulated_answer(expected, final_text)})

    return {"query": case["query"], "expected": expected, "intent": case.get("intent"),
            "predicted": predicted, "rounds": answers,
            "final_excerpt": final_text[:200].replace("\n", " ")}


def main():
    gold = json.load(open(os.path.join(HERE, "gold_set.json"), encoding="utf-8"))
    cases = gold["cases"]
    if LIMIT:
        cases = cases[:LIMIT]
    results = []
    for i, c in enumerate(cases, 1):
        for attempt in range(2):
            try:
                r = run_case(c)
                break
            except Exception as e:
                if attempt == 1:
                    r = {"query": c["query"], "expected": c.get("expected_kb"),
                         "predicted": None, "rounds": 0, "error": str(e), "final_excerpt": ""}
                time.sleep(3)
        results.append(r)
        exp = r["expected"]
        ok = ("OK " if r["predicted"] == exp else "MISS") if exp else \
             ("OK " if r["predicted"] is None else "FP ")
        print(f"  [{i:>3}/{len(cases)}] {ok} {c['query'][:42]:<42} exp={str(exp):<10} "
              f"got={str(r['predicted']):<10} rounds={r['rounds']}")
        json.dump(results, open(os.path.join(HERE, "results_agent_local.json"), "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)

    in_kb = [r for r in results if r["expected"]]
    out_kb = [r for r in results if not r["expected"]]
    top1 = sum(1 for r in in_kb if r["predicted"] == r["expected"])
    oob_ok = sum(1 for r in out_kb if r["predicted"] is None)
    multi = sum(1 for r in in_kb if r.get("rounds", 1) > 1)

    def pct(n, d):
        return f"{(100.0*n/d):.1f}%" if d else "n/a"

    summary = {
        "model": DEPLOY, "total": len(results), "in_kb": len(in_kb), "out_kb": len(out_kb),
        "end_to_end_top1": pct(top1, len(in_kb)),
        "out_of_kb_correct": pct(oob_ok, len(out_kb)),
        "needed_clarification": f"{multi}/{len(in_kb)}",
    }
    misses = [r for r in in_kb if r["predicted"] != r["expected"]]
    fps = [r for r in out_kb if r["predicted"] is not None]
    json.dump({"summary": summary, "misses": misses, "false_positives": fps, "results": results},
              open(os.path.join(HERE, "results_agent_local.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)

    print("\n" + "=" * 68)
    print("END-TO-END AGENT EVAL (local reconstruction: gpt-4.1-mini + v6 prompt)")
    print("=" * 68)
    for k, v in summary.items():
        print(f"  {k:<24}: {v}")
    print("\n  MISSES:")
    for r in misses:
        print(f"    \"{r['query'][:46]}\"  {r['expected']} -> {r['predicted']} (rounds={r['rounds']})")
    if fps:
        print("\n  FALSE POSITIVES:")
        for r in fps:
            print(f"    \"{r['query'][:46]}\" -> {r['predicted']}")


if __name__ == "__main__":
    main()
