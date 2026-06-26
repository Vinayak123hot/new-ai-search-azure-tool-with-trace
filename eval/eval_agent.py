"""
eval_agent.py — end-to-end accuracy of the Foundry classification agent.

For each gold-set utterance: open a thread, send it, run the agent. If the agent
asks a clarification question (no 'KB_Article_name:' in its reply), simulate a
user answer from the EXPECTED KB's symptoms and run again (up to MAX_ROUNDS).
Parse the final 'KB_Article_name: KBxxxx' and compare to expected.

This measures the REAL pipeline including the clarification loop — the number
that backs a production accuracy claim.

Env:
  FOUNDRY_PROJECT_ENDPOINT (default the Teva project)
  AGENT_ID                 (default Classification Agent)
Writes eval/results_agent.json and prints a summary.
"""
import json
import os
import re
import time

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.agents.models import ListSortOrder

HERE = os.path.dirname(os.path.abspath(__file__))
ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "https://teva.services.ai.azure.com/api/projects/Teva")
AGENT_ID = os.environ.get("AGENT_ID", "asst_q9ACY0rZWMl5FIzCtw0solGS")  # Classification Agent
MAX_ROUNDS = 3

KB_RE = re.compile(r"KB_Article_name\s*:\s*(KB\d+)", re.IGNORECASE)

client = AIProjectClient(endpoint=ENDPOINT, credential=DefaultAzureCredential())
agents = client.agents
meta = {m["kb_id"]: m for m in json.load(open(os.path.join(HERE, "kb_meta.json"), encoding="utf-8"))}


def last_assistant(thread_id):
    msgs = list(agents.messages.list(thread_id=thread_id, order=ListSortOrder.ASCENDING))
    text = ""
    for m in msgs:
        if m.role == "assistant":
            for c in m.content:
                v = getattr(getattr(c, "text", None), "value", None)
                if v:
                    text = v
    return text


def simulated_answer(expected_kb):
    """A plausible user reply to a clarification question, drawn from the
    expected KB's symptoms (or a neutral elaboration for out-of-KB cases)."""
    if expected_kb and expected_kb in meta and meta[expected_kb]["symptoms"]:
        return "Yes — " + " ".join(meta[expected_kb]["symptoms"][:2])
    return "It is not related to those options; nothing else to add."


def run_case(case):
    expected = case.get("expected_kb")
    thread = agents.threads.create()
    agents.messages.create(thread_id=thread.id, role="user", content=case["query"])
    rounds = 0
    predicted = None
    final_text = ""
    for rounds in range(1, MAX_ROUNDS + 1):
        run = agents.runs.create_and_process(thread_id=thread.id, agent_id=AGENT_ID)
        final_text = last_assistant(thread.id)
        m = KB_RE.search(final_text or "")
        if m:
            predicted = m.group(1).upper()
            break
        # no classification yet -> agent likely asked a follow-up; answer it
        if rounds < MAX_ROUNDS:
            agents.messages.create(thread_id=thread.id, role="user",
                                   content=simulated_answer(expected))
    return {
        "query": case["query"],
        "expected": expected,
        "intent": case.get("intent"),
        "predicted": predicted,
        "rounds": rounds,
        "asked_clarification": rounds > 1 or (predicted is None),
        "final_excerpt": (final_text or "")[:300],
        "thread_id": thread.id,
    }


def main():
    gold = json.load(open(os.path.join(HERE, "gold_set.json"), encoding="utf-8"))
    cases = gold["cases"]
    results = []
    for i, c in enumerate(cases, 1):
        for attempt in range(2):
            try:
                r = run_case(c)
                break
            except Exception as e:
                if attempt == 1:
                    r = {"query": c["query"], "expected": c.get("expected_kb"),
                         "predicted": None, "rounds": 0, "error": str(e),
                         "asked_clarification": None, "final_excerpt": "", "thread_id": None}
                time.sleep(3)
        results.append(r)
        ok = ("OK " if r["predicted"] == r["expected"] else "MISS") if r["expected"] else \
             ("OK " if r["predicted"] is None else "FP ")
        print(f"  [{i:>3}/{len(cases)}] {ok} {c['query'][:44]:<44} exp={str(r['expected']):<10} "
              f"got={str(r['predicted']):<10} rounds={r['rounds']}")
        # checkpoint
        json.dump(results, open(os.path.join(HERE, "results_agent.json"), "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)

    in_kb = [r for r in results if r["expected"]]
    out_kb = [r for r in results if not r["expected"]]
    top1 = sum(1 for r in in_kb if r["predicted"] == r["expected"])
    oob_ok = sum(1 for r in out_kb if r["predicted"] is None)
    cl, total_rounds = 0, 0
    for r in in_kb:
        if r.get("rounds", 0) > 1:
            cl += 1
        total_rounds += r.get("rounds", 0)

    def pct(n, d):
        return f"{(100.0*n/d):.1f}%" if d else "n/a"

    summary = {
        "agent_id": AGENT_ID,
        "total": len(results),
        "in_kb": len(in_kb),
        "out_kb": len(out_kb),
        "end_to_end_top1_accuracy": pct(top1, len(in_kb)),
        "out_of_kb_correct": pct(oob_ok, len(out_kb)),
        "needed_clarification": f"{cl}/{len(in_kb)}",
        "avg_rounds_in_kb": f"{(total_rounds/len(in_kb)):.2f}" if in_kb else "n/a",
    }
    misses = [r for r in in_kb if r["predicted"] != r["expected"]]
    fps = [r for r in out_kb if r["predicted"] is not None]
    out = {"summary": summary, "misses": misses, "false_positives": fps, "results": results}
    json.dump(out, open(os.path.join(HERE, "results_agent.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)

    print("\n" + "=" * 70)
    print("END-TO-END AGENT EVALUATION")
    print("=" * 70)
    for k, v in summary.items():
        print(f"  {k:<28}: {v}")
    print("\n  MISSES:")
    for r in misses:
        print(f"    \"{r['query'][:48]}\"  {r['expected']} -> {r['predicted']} (rounds={r['rounds']})")
    print("\n  FALSE POSITIVES (out-of-KB classified as a KB):")
    for r in fps:
        print(f"    \"{r['query'][:48]}\" -> {r['predicted']}")


if __name__ == "__main__":
    main()
