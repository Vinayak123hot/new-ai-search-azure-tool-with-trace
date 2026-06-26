"""
foundry_eval.py — publish the gold-set evaluation to Azure AI Foundry so it
appears in the portal's Evaluation tab.

Consumes results_agent_local.json (produced by eval_agent_local.py: the faithful
reconstruction of Test-agent over the gold set) and uploads a dataset evaluation
with custom evaluators (top-1 KB match, correctness incl. out-of-KB).

Run after the end-to-end eval completes:
  python eval/foundry_eval.py
Prints the portal studio_url.
"""
import json
import os

from azure.ai.evaluation import evaluate

HERE = os.path.dirname(os.path.abspath(__file__))
ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "https://teva.services.ai.azure.com/api/projects/Teva")
RESULTS = os.path.join(HERE, "results_agent_local.json")
DATA = os.path.join(HERE, "foundry_eval_data.jsonl")
NAME = os.environ.get("EVAL_NAME", "testagent-kb-classification")


class KBTopMatch:
    """1.0 when the predicted KB equals the expected KB (in-KB cases)."""
    def __call__(self, *, ground_truth=None, predicted=None):
        return {"score": 1.0 if (predicted or "NONE") == (ground_truth or "NONE") else 0.0}


class Correctness:
    """1.0 when the agent did the right thing, including correctly returning
    NONE for out-of-KB queries."""
    def __call__(self, *, ground_truth=None, predicted=None):
        return {"score": 1.0 if (predicted or "NONE") == (ground_truth or "NONE") else 0.0}


def build_dataset():
    data = json.load(open(RESULTS, encoding="utf-8"))
    rows = data["results"] if isinstance(data, dict) else data
    with open(DATA, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({
                "query": r.get("query", ""),
                "response": r.get("final_excerpt", "") or "(no final text)",
                "ground_truth": r.get("expected") or "NONE",
                "predicted": r.get("predicted") or "NONE",
                "intent": r.get("intent", ""),
                "rounds": r.get("rounds", 0),
            }) + "\n")
    return sum(1 for _ in open(DATA, encoding="utf-8"))


def main():
    n = build_dataset()
    print(f"dataset rows: {n} -> {DATA}")
    result = evaluate(
        data=DATA,
        evaluators={"kb_top1_match": KBTopMatch(), "correctness": Correctness()},
        evaluation_name=NAME,
        azure_ai_project=ENDPOINT,
    )
    print("\nmetrics:", json.dumps(result.get("metrics"), indent=2))
    print("\nstudio_url:", result.get("studio_url"))


if __name__ == "__main__":
    main()
