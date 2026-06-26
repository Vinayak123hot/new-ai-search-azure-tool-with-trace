"""
Layer 4 — Full end-to-end agent outcome.

Runs every case through the real agent (with the simulator answering any
follow-ups) and scores the FINAL behavior the user would actually get.

Metrics:
  - end_to_end_top1: in-KB cases where final KB_Article_name == expected
  - out_of_kb_correct: out-of-KB cases where the agent did NOT assert a KB
  - guidance_routing: of resolved in-KB picks whose KB has
    guidance_troubleshoot=true, did the agent call get_kb_guidance
    (and NOT call it when false)
  - avg_rounds

Needs AZURE_OPENAI_KEY (+ TOOL_API_KEY). The deterministic display-decision
check lives in Layer 2; here we score the conversation's final answer.
"""
import json
import os

from common import AgentRunner, KBTool, load_config, load_gold, load_kb_meta, pct


def run(cfg=None, gold=None, kb_meta=None, tool=None, runner=None, verbose=True) -> dict:
    cfg = cfg or load_config()
    gold = gold or load_gold()
    kb_meta = kb_meta or load_kb_meta()
    tool = tool or KBTool(cfg)
    runner = runner or AgentRunner(cfg, kb_meta, tool)

    # in-KB = any case naming a real doc (strong/weak/ambiguous); out-of-KB = expected null
    in_kb = [c for c in gold if c.get("expected_kb")]
    oob = [c for c in gold if not c.get("expected_kb")]

    rows, top1, oob_ok, rounds_total = [], 0, 0, 0
    gd_total = gd_ok = 0
    misses, false_pos = [], []

    for c in in_kb + oob:
        res = runner.run(c)
        exp = c.get("expected_kb")
        pred = res["predicted"]
        rounds_total += res["rounds"]
        if exp:
            # ambiguous cases may resolve to dialog.expected_after_followup (== expected_kb here)
            ok = pred == exp
            if ok:
                top1 += 1
                meta = kb_meta.get(exp, {})
                want_guidance = bool(meta.get("guidance_troubleshoot"))
                gd_total += 1
                if res["called_guidance"] == want_guidance:
                    gd_ok += 1
            else:
                misses.append({"id": c["id"], "expected": exp, "predicted": pred,
                               "rounds": res["rounds"]})
        else:
            ok = pred is None
            if ok:
                oob_ok += 1
            else:
                false_pos.append({"id": c["id"], "predicted": pred})
        rows.append({"id": c["id"], "tier": c["tier"], "expected": exp, "predicted": pred,
                     "rounds": res["rounds"], "called_guidance": res["called_guidance"],
                     "correct": ok})
        if verbose:
            tag = "OK " if ok else ("FP " if not exp else "MISS")
            print(f"  [L4] {tag} {c['id']:<14} exp={str(exp):<11} got={str(pred):<11} r={res['rounds']}")

    metrics = {
        "in_kb_cases": len(in_kb), "out_of_kb_cases": len(oob),
        "end_to_end_top1": pct(top1, len(in_kb)),
        "out_of_kb_correct": pct(oob_ok, len(oob)),
        "guidance_routing": pct(gd_ok, gd_total),
        "avg_rounds": round(rounds_total / len(rows), 2) if rows else None,
    }
    return {"layer": 4, "name": "end_to_end", "metrics": metrics,
            "misses": misses, "false_positives": false_pos}


if __name__ == "__main__":
    out = run()
    print(json.dumps(out["metrics"], indent=2))
    with open(os.path.join(os.path.dirname(__file__), "..", "results_layer4.json"),
              "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
