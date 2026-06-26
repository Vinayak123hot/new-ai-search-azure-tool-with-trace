"""
Layer 1 — Retrieval quality (get_kb_candidates in isolation).

Does the search surface the right document at all, and rank it first?
Independent of the LLM. Metrics: recall@RETURN_K, top-1, MRR, plus a
per-doc breakdown and the confusion list (every miss).

Runs on tool only (TOOL_API_KEY). Returns a metrics dict; can also run
standalone:  python eval/harness/layer1_retrieval.py
"""
import json
import os

from common import KBTool, load_config, load_gold, pct


def run(cfg=None, gold=None, tool=None, verbose=True) -> dict:
    cfg = cfg or load_config()
    gold = gold or load_gold()
    tool = tool or KBTool(cfg)
    k = cfg["thresholds"]["RETURN_K"]

    # Layer 1 scores every case that names a real doc (any tier).
    cases = [c for c in gold if c.get("expected_kb")]
    rows, recall_hits, top1_hits, rr_sum, misses = [], 0, 0, 0.0, []

    for c in cases:
        resp = tool.candidates(c["query"])
        returned = [x.get("kb_id") for x in (resp.get("candidates") or [])]
        exp = c["expected_kb"]
        rank = returned.index(exp) + 1 if exp in returned else 0
        if exp in returned:
            recall_hits += 1
            rr_sum += 1.0 / rank
        if returned[:1] == [exp]:
            top1_hits += 1
        else:
            misses.append({"id": c["id"], "query": c["query"], "expected": exp,
                           "predicted_top": returned[0] if returned else None,
                           "returned": returned, "top_score": resp.get("top_score"),
                           "spread": resp.get("spread")})
        rows.append({"id": c["id"], "expected": exp, "returned": returned,
                     "rank": rank, "top_score": resp.get("top_score")})
        if verbose:
            print(f"  [L1] {c['id']:<14} exp={exp:<11} got={returned[:1]} rank={rank}")

    n = len(cases)
    per_doc = {}
    for c, r in zip(cases, rows):
        d = per_doc.setdefault(c["expected_kb"], [0, 0])
        d[1] += 1
        if r["returned"][:1] == [c["expected_kb"]]:
            d[0] += 1

    metrics = {
        "cases": n,
        "recall_at_k": pct(recall_hits, n),
        "top1": pct(top1_hits, n),
        "mrr": round(rr_sum / n, 3) if n else None,
        "k": k,
        "per_doc_top1": {kb: f"{v[0]}/{v[1]}" for kb, v in sorted(per_doc.items())},
    }
    return {"layer": 1, "name": "retrieval", "metrics": metrics, "misses": misses}


if __name__ == "__main__":
    out = run()
    print(json.dumps(out["metrics"], indent=2))
    json.dump(out, open(os.path.join(os.path.dirname(__file__), "..", "results_layer1.json"),
                        "w", encoding="utf-8"), indent=2, ensure_ascii=False)
