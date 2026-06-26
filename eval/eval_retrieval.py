"""
eval_retrieval.py — tool-level accuracy of get_kb_candidates over the gold set.

Standalone (stdlib only). Reads eval/gold_set.json, calls the candidates endpoint
for each utterance, and reports:
  - top-1 accuracy (in-KB cases)
  - recall@RETURN_K (is the right doc returned at all)
  - out-of-KB rejection rate (null-expected cases that correctly return no match)
  - guidance_troubleshoot correctness on the top-1
  - spread distribution
  - confusion list (every miss)

Usage:
  set TOOL_API_KEY=...           (or it runs unauth if the service allows)
  set EVAL_ENDPOINT=https://teva-kb-trace.azurewebsites.net   (default)
  python eval/eval_retrieval.py
"""
import json
import os
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENDPOINT = os.environ.get("EVAL_ENDPOINT", "https://teva-kb-trace.azurewebsites.net").rstrip("/")
API_KEY = os.environ.get("TOOL_API_KEY", "")


def call_candidates(description):
    body = json.dumps({"description": description}).encode("utf-8")
    req = urllib.request.Request(f"{ENDPOINT}/get_kb_candidates", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if API_KEY:
        req.add_header("x-api-key", API_KEY)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    gold = json.load(open(os.path.join(HERE, "gold_set.json"), encoding="utf-8"))
    cases = gold["cases"]

    results = []
    for i, c in enumerate(cases, 1):
        for attempt in range(3):
            try:
                resp = call_candidates(c["query"])
                break
            except Exception as e:
                if attempt == 2:
                    resp = {"_error": str(e), "candidates": []}
                time.sleep(2)
        cands = resp.get("candidates", []) or []
        pred_ids = [x.get("kb_id") for x in cands]
        top = cands[0] if cands else None
        results.append({
            "query": c["query"],
            "expected": c.get("expected_kb"),
            "intent": c.get("intent"),
            "predicted_top": top.get("kb_id") if top else None,
            "top_score": resp.get("top_score"),
            "spread": resp.get("spread"),
            "returned": pred_ids,
            "top_gt": top.get("guidance_troubleshoot") if top else None,
        })
        print(f"  [{i:>3}/{len(cases)}] {c['query'][:48]:<48} exp={str(c.get('expected_kb')):<10} "
              f"got={str(pred_ids[0] if pred_ids else None):<10} spread={resp.get('spread')}")

    # ── metrics ────────────────────────────────────────────────
    in_kb = [r for r in results if r["expected"]]
    out_kb = [r for r in results if not r["expected"]]

    top1 = sum(1 for r in in_kb if r["predicted_top"] == r["expected"])
    recall = sum(1 for r in in_kb if r["expected"] in r["returned"])
    # out-of-KB handled correctly = no confident match returned (empty candidates)
    oob_ok = sum(1 for r in out_kb if not r["returned"])

    def pct(n, d):
        return f"{(100.0*n/d):.1f}%" if d else "n/a"

    spread_counts = {}
    for r in results:
        spread_counts[r["spread"]] = spread_counts.get(r["spread"], 0) + 1

    # spread among correct vs wrong top-1 (in-KB)
    sp_correct = {"high": 0, "low": 0}
    sp_wrong = {"high": 0, "low": 0}
    for r in in_kb:
        bucket = sp_correct if r["predicted_top"] == r["expected"] else sp_wrong
        if r["spread"] in bucket:
            bucket[r["spread"]] += 1

    # guidance_troubleshoot correctness on correct top-1 picks
    meta = {m["kb_id"]: m for m in json.load(open(os.path.join(HERE, "kb_meta.json"), encoding="utf-8"))}
    gt_correct = gt_total = 0
    for r in in_kb:
        if r["predicted_top"] == r["expected"] and r["expected"] in meta:
            gt_total += 1
            if r["top_gt"] == meta[r["expected"]]["guidance_troubleshoot"]:
                gt_correct += 1

    # per-KB top-1
    per_kb = {}
    for r in in_kb:
        k = r["expected"]
        per_kb.setdefault(k, [0, 0])
        per_kb[k][1] += 1
        if r["predicted_top"] == r["expected"]:
            per_kb[k][0] += 1

    misses = [r for r in in_kb if r["predicted_top"] != r["expected"]]
    false_pos = [r for r in out_kb if r["returned"]]

    summary = {
        "endpoint": ENDPOINT,
        "total_cases": len(results),
        "in_kb_cases": len(in_kb),
        "out_kb_cases": len(out_kb),
        "top1_accuracy": pct(top1, len(in_kb)),
        "recall_at_returnk": pct(recall, len(in_kb)),
        "out_of_kb_rejection": pct(oob_ok, len(out_kb)),
        "guidance_flag_accuracy_on_hits": pct(gt_correct, gt_total),
        "spread_distribution": spread_counts,
        "spread_when_correct": sp_correct,
        "spread_when_wrong": sp_wrong,
        "per_kb_top1": {k: f"{v[0]}/{v[1]}" for k, v in sorted(per_kb.items())},
    }

    out = {"summary": summary, "misses": misses, "false_positives": false_pos, "results": results}
    json.dump(out, open(os.path.join(HERE, "results_retrieval.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)

    print("\n" + "=" * 70)
    print("TOOL-LEVEL RETRIEVAL EVALUATION  (get_kb_candidates)")
    print("=" * 70)
    for k, v in summary.items():
        print(f"  {k:<32}: {v}")
    print("\n  MISSES (expected -> predicted):")
    for r in misses:
        print(f"    \"{r['query'][:50]}\"  {r['expected']} -> {r['predicted_top']} "
              f"(score={r['top_score']}, spread={r['spread']})")
    print("\n  FALSE POSITIVES (out-of-KB that returned a match):")
    for r in false_pos:
        print(f"    \"{r['query'][:50]}\"  -> {r['predicted_top']} (score={r['top_score']})")
    print(f"\n  full results -> eval/results_retrieval.json")


if __name__ == "__main__":
    main()
