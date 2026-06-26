# 4-Layer Evaluation Harness — Classification Agent

Production-grade, **data-driven** evaluation for the Outlook KB classification agent.
The design goal: **going from 8 docs to 200 changes only data, never code, metrics, or gates.**

```
eval/
├── config.json              # thresholds (mirror app) + CI gate targets   ← tune rarely
├── kb_meta.json             # doc registry (kb_id, title, guidance_troubleshoot, symptoms)  ← GROWS
├── gold_set.json            # test cases with tiers + multi-turn dialogs              ← GROWS
├── agent_instructions_testagent.txt   # exact production prompt (layers 3-4)
└── harness/
    ├── common.py            # config/data loaders, KB tool client, agent runner, simulator
    ├── layer1_retrieval.py  # recall@K, top-1, MRR  (tool only)
    ├── layer2_threshold.py  # display precision/recall, OOK rejection, threshold sweep (tool only)
    ├── layer3_clarification.py  # follow-up trigger, groundedness, lift  (agent)
    ├── layer4_end_to_end.py     # final top-1, OOK handling, guidance routing  (agent)
    └── run_all.py           # orchestrates all layers, writes report, applies CI gate
```

## The four layers

| Layer | Question | Needs | Key metrics |
|---|---|---|---|
| **1 Retrieval** | Does search find & rank the right doc? | tool | recall@K, top-1, MRR |
| **2 Threshold** | Do we show right docs / reject the rest? | tool | display precision/recall, OOK rejection, **DISPLAY_MIN_SCORE sweep** |
| **3 Clarification** | Does it ask a grounded follow-up when unsure, and does it help? | agent | clarification trigger, groundedness, resolved-after-followup |
| **4 End-to-end** | Final answer correct + routed right? | agent | end-to-end top-1, OOK correct, guidance routing |

Layers 1–2 are deterministic and cheap (run on every commit). Layers 3–4 call the
LLM (run nightly / pre-release).

## Run it

```bash
# secrets via env (never in config.json)
export TOOL_API_KEY=...                  # x-api-key for the tool endpoints
export AZURE_OPENAI_KEY=...              # only needed for layers 3-4

python eval/harness/run_all.py                 # all layers + gate (exit!=0 on regression)
python eval/harness/run_all.py --layers 1,2    # tool-only, fast
python eval/harness/run_all.py --smoke 2       # ≤2 cases/tier, cheap LLM check, gate off
python eval/harness/run_all.py --no-gate       # report without failing the build

# individual layer (writes results_layerN.json)
python eval/harness/layer1_retrieval.py
```

### Before the first layer-3/4 run (no quota spent)
`preflight_layers34.py` validates the whole agent path — prompt, dataset, SDK,
endpoint, the agent loop, the clarification simulator, and the layer-3/4 scoring —
using a **mocked LLM + mocked tool**. Run it any time you change the harness:

```bash
python eval/harness/preflight_layers34.py      # exit 0 = ready
```

When the LLM is ready, the real run is just:
```bash
export TOOL_API_KEY=... AZURE_OPENAI_KEY=...
python eval/harness/run_all.py --smoke 2        # sanity (a few LLM calls)
python eval/harness/run_all.py                  # full run (~80+ LLM calls) + gate
```

> The harness replays the **exact current production prompt**
> (`agent_instructions_testagent.txt`). If you edit the live Test-agent, re-sync
> that file from the latest version before running so the reconstruction stays faithful.

Outputs: `eval/eval_results.json` (machine-readable) and `eval/EVAL_REPORT_4LAYER.md`
(human-readable). `run_all.py` exits non-zero if any gate target is missed → drop it
straight into CI as a regression gate.

## Scaling from 8 → 200 docs (the whole point)

You only touch **two data files**. No code, metric, or gate edits.

1. **Add the doc to `kb_meta.json`:**
   ```json
   { "kb_id": "KB00xxxxx", "title": "...", "guidance_troubleshoot": true,
     "environment": "...", "symptoms": ["...", "..."], "source_file": "KB00xxxxx.docx" }
   ```
   (`parse_kb_docs.py` can regenerate this from the .docx files automatically.)

2. **Add ~10–20 cases to `gold_set.json`** for that doc, covering the tiers:
   ```json
   {"id":"KB00xxxxx-01","query":"<realistic user utterance>","expected_kb":"KB00xxxxx","intent":"ISSUE","tier":"strong"}
   ```

   | tier | meaning | which layers use it |
   |---|---|---|
   | `strong` | clear match — should be top-1 **and** displayed | 1, 2, 4 |
   | `weak` | on-topic but vague — returned (powers grounding) but **not** displayed | 2 |
   | `ambiguous` | under-specified — should trigger spread=high + follow-up (needs `dialog`) | 3, 4 |
   | `out_of_kb` | no valid doc (`expected_kb: null`) — should be rejected | 2, 4 |

   Ambiguous cases carry a multi-turn `dialog`:
   ```json
   {"id":"CLAR-xx","query":"<vague opener>","expected_kb":"KB00xxxxx","intent":"ISSUE","tier":"ambiguous",
    "dialog":{"answers":{"<keyword in agent's follow-up>":"<simulated user reply>"},
              "expected_after_followup":"KB00xxxxx"}}
   ```
   The simulator matches the agent's question against the `answers` keywords — so a new
   case just needs the right keyword→reply pairs, no code change.

3. **Re-run** `run_all.py`. Same metrics, same gates, recomputed over the bigger set.
   Target ~2,000–4,000 cases at 200 docs.

## When you *would* edit code/config (rare, deliberate)

- **Retune thresholds** (`config.json → thresholds`) — only if you change the app's
  `MIN_SCORE`/`DISPLAY_MIN_SCORE`. Use the Layer-2 sweep to pick values, then mirror
  them here so eval and prod agree.
- **Tighten gates** (`config.json → gates`) — raise targets as accuracy improves to
  prevent backsliding.
- New metric or new layer — that's a genuine code change; everything else is data.
