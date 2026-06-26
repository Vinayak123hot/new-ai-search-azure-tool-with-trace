"""
Layer 3 — Clarification / follow-up quality (the conversational core).

Runs the ambiguous, multi-turn cases through the real agent + user simulator.
Measures whether the agent asks a grounded follow-up when retrieval is
uncertain, and whether asking actually helps.

Metrics:
  - clarification_trigger: of ambiguous cases (spread=high on first call),
    how many actually asked a follow-up instead of guessing
  - groundedness: are follow-up questions built from the returned
    discriminating_symptoms (token overlap), not invented
  - resolved_after_followup: landed on expected_after_followup
  - avg_rounds: follow-up turns used

Needs AZURE_OPENAI_KEY (+ TOOL_API_KEY). Returns metrics dict.
"""
import json
import os

from common import AgentRunner, KBTool, load_config, load_gold, load_kb_meta, pct, tokens


def _first_candidates_event(ev):
    for e in ev:
        if e["name"] == "get_kb_candidates":
            return e
    return None


def run(cfg=None, gold=None, tool=None, kb_meta=None, runner=None, verbose=True) -> dict:
    cfg = cfg or load_config()
    gold = gold or load_gold()
    kb_meta = kb_meta or load_kb_meta()
    tool = tool or KBTool(cfg)
    runner = runner or AgentRunner(cfg, kb_meta, tool)

    cases = [c for c in gold if c["tier"] == "ambiguous"]
    rows = []
    triggered = grounded = resolved = high_spread = 0
    rounds_total = 0

    for c in cases:
        res = runner.run(c)
        ev = res["tool_events"]
        first = _first_candidates_event(ev)
        spread = (first or {}).get("response", {}).get("spread")
        disc = (first or {}).get("response", {}).get("discriminating_symptoms", []) or []
        asked = len(res["followups"]) > 0
        is_high = spread == "high"
        if is_high:
            high_spread += 1
        if is_high and asked:
            triggered += 1
        # groundedness: do the asked questions draw on the discriminating symptoms?
        g = None
        if asked and disc:
            disc_tok = set().union(*(tokens(s) for s in disc)) if disc else set()
            q_tok = set().union(*(tokens(q) for q in res["followups"]))
            overlap = len(disc_tok & q_tok) / len(q_tok) if q_tok else 0.0
            g = round(overlap, 2)
            if overlap >= 0.15:           # question meaningfully references symptom vocabulary
                grounded += 1
        exp_after = (c.get("dialog") or {}).get("expected_after_followup") or c.get("expected_kb")
        ok = res["predicted"] == exp_after
        if ok:
            resolved += 1
        rounds_total += res["rounds"]
        rows.append({"id": c["id"], "spread_first": spread, "asked_followup": asked,
                     "rounds": res["rounds"], "groundedness": g,
                     "expected_after": exp_after, "predicted": res["predicted"], "resolved": ok})
        if verbose:
            print(f"  [L3] {c['id']:<10} spread={spread} asked={asked} rounds={res['rounds']} "
                  f"-> {res['predicted']} (exp {exp_after}) {'OK' if ok else 'MISS'}")

    n = len(cases)
    metrics = {
        "ambiguous_cases": n,
        "spread_high_on_first": pct(high_spread, n),
        "clarification_trigger": pct(triggered, high_spread) if high_spread else None,
        "questions_grounded": pct(grounded, sum(1 for r in rows if r["asked_followup"])),
        "resolved_after_followup": pct(resolved, n),
        "avg_rounds": round(rounds_total / n, 2) if n else None,
    }
    return {"layer": 3, "name": "clarification", "metrics": metrics, "rows": rows}


if __name__ == "__main__":
    out = run()
    print(json.dumps(out["metrics"], indent=2))
    with open(os.path.join(os.path.dirname(__file__), "..", "results_layer3.json"),
              "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
