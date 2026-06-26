"""
Layer 2 — Threshold calibration (the two-threshold model).

Treats the score thresholds as a classifier and answers: are we showing the
right docs and rejecting the rest?

  - strong cases    -> expect top candidate meets_display_threshold == true   (display TP)
  - out_of_kb cases -> expect NO confident display (any_meets_display_threshold == false / empty) (display FP if it does)
  - weak cases      -> expect RETURNED (non-empty) but top meets_display_threshold == false (informational)

Metrics: display precision/recall, out-of-KB rejection, weak-held rate, and a
DISPLAY_MIN_SCORE sweep (TP/FP display rate at each candidate threshold) so you
can pick the knee without changing code. Tool only (TOOL_API_KEY).
"""
import json
import os

from common import KBTool, load_config, load_gold, pct


def run(cfg=None, gold=None, tool=None, verbose=True) -> dict:
    cfg = cfg or load_config()
    gold = gold or load_gold()
    tool = tool or KBTool(cfg)
    disp_thr = cfg["thresholds"]["DISPLAY_MIN_SCORE"]

    strong = [c for c in gold if c["tier"] == "strong"]
    weak = [c for c in gold if c["tier"] == "weak"]
    oob = [c for c in gold if c["tier"] == "out_of_kb"]

    # collect the live signal for each group
    def probe(cases):
        out = []
        for c in cases:
            r = tool.candidates(c["query"])
            cands = r.get("candidates") or []
            top = cands[0] if cands else None
            out.append({
                "id": c["id"], "expected": c.get("expected_kb"),
                "returned_top": top.get("kb_id") if top else None,
                "top_score": r.get("top_score") if cands else None,
                "top_meets_display": bool(top.get("meets_display_threshold")) if top else False,
                "any_meets_display": bool(r.get("any_meets_display_threshold")),
                "n_candidates": len(cands),
            })
            if verbose:
                print(f"  [L2] {c['id']:<14} score={out[-1]['top_score']} "
                      f"display={out[-1]['top_meets_display']}")
        return out

    s_rows, w_rows, o_rows = probe(strong), probe(weak), probe(oob)

    display_tp = sum(1 for r in s_rows if r["top_meets_display"])          # should & did display
    display_fp = sum(1 for r in o_rows if r["any_meets_display"])          # shouldn't but displayed
    oob_rejected = sum(1 for r in o_rows if not r["any_meets_display"])
    weak_held = sum(1 for r in w_rows if r["n_candidates"] > 0 and not r["top_meets_display"])

    precision = pct(display_tp, display_tp + display_fp)
    recall = pct(display_tp, len(s_rows))

    # threshold sweep: at each candidate DISPLAY_MIN_SCORE, what would TP/FP display look like?
    sweep = []
    for thr in cfg["threshold_sweep"]:
        tp = sum(1 for r in s_rows if (r["top_score"] or 0) >= thr)
        fp = sum(1 for r in o_rows if (r["top_score"] or 0) >= thr)
        sweep.append({"display_min_score": thr,
                      "display_recall_pct": pct(tp, len(s_rows)),
                      "false_display_pct": pct(fp, len(o_rows))})

    metrics = {
        "display_min_score_in_use": disp_thr,
        "strong_cases": len(s_rows), "weak_cases": len(w_rows), "out_of_kb_cases": len(o_rows),
        "display_precision": precision,
        "display_recall": recall,
        "out_of_kb_rejection": pct(oob_rejected, len(o_rows)),
        "weak_correctly_held": pct(weak_held, len(w_rows)),
        "threshold_sweep": sweep,
    }
    false_displays = [r for r in o_rows if r["any_meets_display"]]
    return {"layer": 2, "name": "threshold", "metrics": metrics,
            "false_displays": false_displays}


if __name__ == "__main__":
    out = run()
    print(json.dumps(out["metrics"], indent=2))
    with open(os.path.join(os.path.dirname(__file__), "..", "results_layer2.json"),
              "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
