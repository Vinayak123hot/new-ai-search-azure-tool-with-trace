# Classification Agent — 4-Layer Evaluation

Generated 2026-06-22T10:44:29 · endpoint `https://teva-kb-trace.azurewebsites.net` · model `gpt-5.4-mini` · 83 cases · agent layers averaged over **3 runs** (mean ± std)

## Gate

| Check | Target | Actual | Status |
|---|---|---|---|
| layer1_recall_at_k | ≥0.98 | 98.6 | PASS |
| layer1_top1 | ≥0.85 | 84.9 | FAIL |
| layer2_out_of_kb_rejection | ≥0.9 | 100.0 | PASS |
| layer2_display_precision | ≥0.85 | 100.0 | PASS |
| layer3_clarification_trigger | ≥0.75 | 75.0 | PASS |
| layer4_end_to_end_top1 | ≥0.9 | 88.6 | FAIL |

## Layer 1 — Retrieval
- recall@3: **98.6%** · top-1: **84.9%** · MRR: 0.918
- per-doc top-1: {'KB0010265': '10/12', 'KB0010863': '6/8', 'KB0010865': '8/9', 'KB0013608': '7/9', 'KB0015622': '6/9', 'KB0015711': '8/8', 'KB0016162': '9/9', 'KB0016493': '8/9'}

## Layer 2 — Threshold calibration
- display precision: **100.0%** · recall: **75.8%**
- out-of-KB rejection: **100.0%** · weak correctly held: 66.7%
- DISPLAY_MIN_SCORE in use: 2.8

  Threshold sweep (display recall vs false-display):

  | DISPLAY_MIN_SCORE | display recall % | false display % |
  |---|---|---|
  | 2.0 | 100.0 | 0.0 |
  | 2.2 | 98.5 | 0.0 |
  | 2.4 | 92.4 | 0.0 |
  | 2.6 | 81.8 | 0.0 |
  | 2.8 | 75.8 | 0.0 |
  | 3.0 | 57.6 | 0.0 |
  | 3.2 | 40.9 | 0.0 |
  | 3.4 | 21.2 | 0.0 |

## Layer 3 — Clarification
- spread=high on first call: 100.0 ± 0.0% · clarification trigger: **75.0 ± 0.0%**
- questions grounded in symptoms: 77.8 ± 15.7% · resolved after follow-up: **66.7 ± 11.8%** · avg rounds: 2.1 ± 0.4

## Layer 4 — End-to-end
- end-to-end top-1: **88.6 ± 0.6%** · out-of-KB correct: **93.3 ± 9.4%**
- guidance routing: 57.2 ± 3.0% · avg rounds: 1.5 ± 0.1
