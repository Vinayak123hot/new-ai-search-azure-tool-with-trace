"""
run_all.py — orchestrate the 4-layer evaluation and apply the CI gate.

  python eval/harness/run_all.py            # run everything, write report, gate
  python eval/harness/run_all.py --layers 1,2   # subset
  python eval/harness/run_all.py --no-gate       # report only, exit 0

Layers 1-2 need TOOL_API_KEY. Layers 3-4 additionally need AZURE_OPENAI_KEY;
if it is missing they are skipped (not failed) so the tool-only layers can
still run in environments without model access.

Writes eval/eval_results.json (machine-readable, all layers) and
eval/EVAL_REPORT_4LAYER.md (human-readable). Exit code is non-zero if any
gate target is missed — wire this straight into CI.

SCALING: adding docs/cases never touches this file. The same metrics and the
same gates are recomputed over the larger gold set.
"""
import argparse
import json
import os
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # make 'common' + layers importable

from common import AgentRunner, KBTool, load_config, load_gold, load_kb_meta   # noqa: E402
import layer1_retrieval, layer2_threshold, layer3_clarification, layer4_end_to_end  # noqa: E402

EVAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# which gate keys read from which layer's metrics
GATE_SOURCE = {
    "layer1_recall_at_k": (1, "recall_at_k"),
    "layer1_top1": (1, "top1"),
    "layer2_out_of_kb_rejection": (2, "out_of_kb_rejection"),
    "layer2_display_precision": (2, "display_precision"),
    "layer3_clarification_trigger": (3, "clarification_trigger"),
    "layer4_end_to_end_top1": (4, "end_to_end_top1"),
}

# layers 1-2 are deterministic (tool only); 3-4 depend on a nondeterministic model
# (gpt-5.4-mini ignores temperature=0), so they are the ones worth running N times.
NONDETERMINISTIC_LAYERS = {3, 4}


def aggregate_runs(per_run: list[dict]) -> dict:
    """Average a layer's metrics across N runs. Returns a result dict whose
    ['metrics'] holds the MEAN of each numeric metric (so the gate and report
    read the expected value, not one noisy draw), plus per-metric std, the raw
    per-run values, and the last run's full detail payload."""
    metric_dicts = [r["metrics"] for r in per_run]
    keys = metric_dicts[0].keys()
    mean, std, raw = {}, {}, {}
    for k in keys:
        vals = [m.get(k) for m in metric_dicts]
        nums = [v for v in vals if isinstance(v, (int, float))]
        if nums and len(nums) == len(vals):          # purely numeric metric
            mean[k] = round(statistics.fmean(nums), 1)
            std[k] = round(statistics.pstdev(nums), 1) if len(nums) > 1 else 0.0
            raw[k] = nums
        else:                                         # non-numeric (e.g. None / structures)
            mean[k] = metric_dicts[-1].get(k)
    last = dict(per_run[-1])
    last["metrics"] = mean
    last["metrics_std"] = std
    last["per_run_metrics"] = raw
    last["n_runs"] = len(per_run)
    return last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", default="1,2,3,4")
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--runs", type=int, default=1,
                    help="repeat the nondeterministic agent layers (3,4) N times and gate on "
                         "the mean +/- std (the model ignores temperature=0, so one run is noisy)")
    ap.add_argument("--smoke", type=int, default=0,
                    help="sample up to N cases PER TIER (cheap subset run; skips the gate)")
    args = ap.parse_args()
    runs = max(1, args.runs)
    want = {int(x) for x in args.layers.split(",") if x.strip()}

    cfg, gold, kb_meta = load_config(), load_gold(), load_kb_meta()
    if args.smoke:
        seen, sample = {}, []
        for c in gold:
            t = c["tier"]
            if seen.get(t, 0) < args.smoke:
                sample.append(c); seen[t] = seen.get(t, 0) + 1
        gold = sample
        args.no_gate = True   # a subset isn't a valid gate
        print(f"SMOKE MODE: {len(gold)} cases (<={args.smoke}/tier) - gate disabled")
    tool = KBTool(cfg)
    has_aoai = bool(os.environ.get("AZURE_OPENAI_KEY"))

    results, skipped = {}, []

    if 1 in want:
        print("\n=== LAYER 1: retrieval ===")
        results[1] = layer1_retrieval.run(cfg, gold, tool)
    if 2 in want:
        print("\n=== LAYER 2: threshold ===")
        results[2] = layer2_threshold.run(cfg, gold, tool)

    runner = None
    if (3 in want or 4 in want):
        if has_aoai:
            runner = AgentRunner(cfg, kb_meta, tool)
        else:
            for L in (3, 4):
                if L in want:
                    skipped.append(L)
            print("\n(!) AZURE_OPENAI_KEY not set -> skipping agent layers 3 and 4.")

    if 3 in want and runner:
        print(f"\n=== LAYER 3: clarification ({runs} run(s)) ===")
        per_run = [layer3_clarification.run(cfg, gold, tool, kb_meta, runner)
                   for _ in range(runs)]
        results[3] = aggregate_runs(per_run) if runs > 1 else per_run[0]
    if 4 in want and runner:
        print(f"\n=== LAYER 4: end-to-end ({runs} run(s)) ===")
        per_run = [layer4_end_to_end.run(cfg, gold, kb_meta, tool, runner)
                   for _ in range(runs)]
        results[4] = aggregate_runs(per_run) if runs > 1 else per_run[0]

    # ── gate ────────────────────────────────────────────────────────
    gate_rows, failed = [], 0
    for key, target in cfg["gates"].items():
        if not isinstance(target, (int, float)):
            continue
        layer, metric = GATE_SOURCE[key]
        if layer not in results:
            gate_rows.append((key, target, None, "SKIP"))
            continue
        actual = results[layer]["metrics"].get(metric)
        actual_frac = (actual / 100.0) if isinstance(actual, (int, float)) else None
        if actual_frac is None:
            gate_rows.append((key, target, actual, "SKIP"))
        elif actual_frac >= target:
            gate_rows.append((key, target, actual, "PASS"))
        else:
            gate_rows.append((key, target, actual, "FAIL"))
            failed += 1

    # ── write machine-readable + report ────────────────────────────
    bundle = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "endpoint": cfg["endpoint"], "deployment": cfg["agent"]["deployment"],
        "total_cases": len(gold), "skipped_layers": skipped,
        "agent_runs": runs,
        "metrics": {L: results[L]["metrics"] for L in results},
        "metrics_std": {L: results[L].get("metrics_std") for L in results if results[L].get("metrics_std")},
        "gate": [{"check": k, "target": t, "actual": a, "status": s} for k, t, a, s in gate_rows],
        "details": results,
    }
    with open(os.path.join(EVAL_DIR, "eval_results.json"), "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, ensure_ascii=False)
    write_report(bundle, cfg)

    # ── console summary + exit code ─────────────────────────────────
    print("\n" + "=" * 60 + "\nGATE\n" + "=" * 60)
    for k, t, a, s in gate_rows:
        print(f"  [{s:<4}] {k:<30} target>={t:<5} actual={a}")
    print(f"\nResults: eval/eval_results.json | Report: eval/EVAL_REPORT_4LAYER.md")
    if args.no_gate:
        sys.exit(0)
    sys.exit(1 if failed else 0)


def write_report(b, cfg):
    L = b["metrics"]
    std = b.get("metrics_std", {})
    runs = b.get("agent_runs", 1)

    def pm(layer, key):
        """Format 'mean ± std' when the layer was run more than once, else the value."""
        val = L.get(layer, {}).get(key)
        s = std.get(layer, {}).get(key) if isinstance(std.get(layer), dict) else None
        return f"{val} ± {s}" if s is not None else f"{val}"

    runs_note = (f" · agent layers averaged over **{runs} runs** (mean ± std)" if runs > 1
                 else "")
    lines = [f"# Classification Agent — 4-Layer Evaluation",
             f"\nGenerated {b['generated']} · endpoint `{b['endpoint']}` · model `{b['deployment']}` "
             f"· {b['total_cases']} cases{runs_note}",
             "\n## Gate", "\n| Check | Target | Actual | Status |", "|---|---|---|---|"]
    for g in b["gate"]:
        lines.append(f"| {g['check']} | ≥{g['target']} | {g['actual']} | {g['status']} |")

    if 1 in L:
        m = L[1]; lines += ["\n## Layer 1 — Retrieval",
            f"- recall@{m['k']}: **{m['recall_at_k']}%** · top-1: **{m['top1']}%** · MRR: {m['mrr']}",
            f"- per-doc top-1: {m['per_doc_top1']}"]
    if 2 in L:
        m = L[2]; lines += ["\n## Layer 2 — Threshold calibration",
            f"- display precision: **{m['display_precision']}%** · recall: **{m['display_recall']}%**",
            f"- out-of-KB rejection: **{m['out_of_kb_rejection']}%** · weak correctly held: {m['weak_correctly_held']}%",
            f"- DISPLAY_MIN_SCORE in use: {m['display_min_score_in_use']}",
            "\n  Threshold sweep (display recall vs false-display):",
            "\n  | DISPLAY_MIN_SCORE | display recall % | false display % |", "  |---|---|---|"]
        for s in m["threshold_sweep"]:
            lines.append(f"  | {s['display_min_score']} | {s['display_recall_pct']} | {s['false_display_pct']} |")
    if 3 in L:
        m = L[3]; lines += ["\n## Layer 3 — Clarification",
            f"- spread=high on first call: {pm(3, 'spread_high_on_first')}% · clarification trigger: **{pm(3, 'clarification_trigger')}%**",
            f"- questions grounded in symptoms: {pm(3, 'questions_grounded')}% · resolved after follow-up: **{pm(3, 'resolved_after_followup')}%** · avg rounds: {pm(3, 'avg_rounds')}"]
    if 4 in L:
        m = L[4]; lines += ["\n## Layer 4 — End-to-end",
            f"- end-to-end top-1: **{pm(4, 'end_to_end_top1')}%** · out-of-KB correct: **{pm(4, 'out_of_kb_correct')}%**",
            f"- guidance routing: {pm(4, 'guidance_routing')}% · avg rounds: {pm(4, 'avg_rounds')}"]
    if b["skipped_layers"]:
        lines.append(f"\n_Skipped layers {b['skipped_layers']} (AZURE_OPENAI_KEY not set)._")
    with open(os.path.join(EVAL_DIR, "EVAL_REPORT_4LAYER.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
