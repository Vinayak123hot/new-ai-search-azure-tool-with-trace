# Outlook KB Classifier — Evaluation Report (8-document baseline)

Generated 2026-06-15. Gold set: 76 cases (66 in-KB across 8 articles, 10 out-of-KB).
Two layers measured: (1) retrieval tool `get_kb_candidates`, (2) end-to-end agent
(faithful reconstruction of **Test-agent**: `gpt-5.4-mini` + its exact 34k prompt +
live KB tool + clarification simulator).

Foundry portal evaluation: https://ai.azure.com/resource/build/evaluation/efd468c3-60ae-4527-88fa-9bc966c3670f

## Headline numbers

| Metric | Result |
|---|---|
| Retrieval recall@RETURN_K (3) | **100%** (66/66) — correct doc always retrieved |
| Retrieval top-1 | 86.4% (57/66) |
| **End-to-end agent top-1 (in-KB)** | **93.9%** (62/66) |
| End-to-end overall (incl. out-of-KB) | 92.1% (70/76) |
| Out-of-KB correctly rejected | 80% (8/10) — 2 false positives |
| guidance_troubleshoot flag accuracy | 100% |

## Failure analysis (4 end-to-end misses — all confusable clusters)

All four misses collapse into **KB0010265 (Outlook Performance / stuck loading)**:

| Query | Expected | Predicted | Score |
|---|---|---|---|
| "my outlook profile is broken how do I rebuild it" | KB0010863 (rebuild profile) | KB0010265 | 2.97 |
| "nothing happens when I click the outlook icon" | KB0013608 (can't open) | KB0010265 | 2.75 |
| "outlook does not launch when I double click it" | KB0013608 | KB0010265 | 2.92 |
| "my outlook app refuses to open" | KB0013608 | KB0010265 | 3.32 |

Two overlapping clusters: **"can't open" (KB0013608) vs "stuck/slow" (KB0010265)**, and
**"rebuild profile" (KB0010863) vs "performance" (KB0010265)**. Retrieval *found* the
right doc every time (recall@3 = 100%); these are **ranking/tie-break** errors.

## Two false positives (out-of-KB matched a KB)

| Query | Predicted | Score |
|---|---|---|
| "set up out of office auto reply" | KB0010863 | 2.059 |
| "how do I create a rule to move emails to a folder" | KB0015711 | 1.905 |

Both score **just above MIN_SCORE (1.9)** — raising MIN_SCORE to ~2.2 likely removes both.

## Key behavioural finding

The agent classified **every in-KB case in one shot (0 clarification rounds)** — even
when retrieval was ambiguous (`spread=high`). It is **not engaging the mandatory
clarification loop**, so the spread="high → ask a follow-up" safety net isn't lifting
accuracy. At the tool level, **every** wrong top-1 had `spread=high`, i.e. the system
*knew* it was unsure. Forcing the follow-up on `spread=high` is the single biggest lever
to push past 95%.

## Recommendations to reach 95% and scale to 200+ docs

1. **Enforce the clarification loop on `spread=high`.** The model currently commits
   directly; making the follow-up mandatory would catch the confusable misses
   (all 4 were ambiguous retrievals). Highest-impact, prompt-level fix.
2. **Raise MIN_SCORE** (≈1.9 → ~2.2). Removes the 2 out-of-KB false positives with
   little risk (true matches scored ≥2.7).
3. **Differentiate the confusable articles.** KB0013608 (can't open), KB0010265
   (performance/stuck), KB0015622 (calendar crash) overlap in symptoms. Sharpen each
   article's `symptoms` (distinct verbs/states) or add discriminating metadata.
4. **Increase retrieval depth as the corpus grows.** Recall@3 is 100% at 8 docs; with
   200 docs, raise `TOP_K`/`k_nearest_neighbors` and re-verify recall@RETURN_K stays
   high (the right doc must survive dedup + truncation).
5. **Scale the gold set** to ~10–20 utterances per document (~2,000–4,000 cases for 200
   docs) and run this harness as a **CI regression gate** before any prompt/threshold/
   doc change ships.
6. **Add content validation** (`parse_kb_docs.py`-style) so all 200 docs parse and carry
   required fields before indexing.

## How to reproduce

```
# 1. pull docs (needs Storage Blob Data Reader on tevablob)
az storage blob download-batch --account-name tevablob --source rag-kb-articles --destination eval/kb_docs --auth-mode login --pattern "*.docx"
python eval/parse_kb_docs.py                      # -> kb_meta.json

# 2. retrieval eval
set TOOL_API_KEY=...&  python eval/eval_retrieval.py   # -> results_retrieval.json

# 3. end-to-end agent eval (faithful reconstruction of Test-agent)
set AZURE_OPENAI_KEY=...; set AOAI_DEPLOYMENT=gpt-5.4-mini; set INSTR_FILE=agent_instructions_testagent.txt
python eval/eval_agent_local.py                   # -> results_agent_local.json

# 4. publish to Foundry portal Evaluation tab
python eval/foundry_eval.py                        # -> studio_url
```

Files: `gold_set.json` (dataset), `eval_retrieval.py`, `eval_agent_local.py`,
`foundry_eval.py`, `kb_meta.json`, `agent_instructions_testagent.txt`,
`results_retrieval.json`, `results_agent_local.json`.
