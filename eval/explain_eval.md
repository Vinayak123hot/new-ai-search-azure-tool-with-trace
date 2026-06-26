# How the Classification-Agent Evaluation Works

A plain-language guide to the 4-layer evaluation: what each layer measures, the
exact formula it uses, and a worked example with real numbers from the latest
(v54) run. Use this to explain the eval strategy end to end.

- **What is evaluated:** the Outlook KB classification agent
  (user message → `get_kb_candidates` retrieval → spread-driven follow-up →
  display-threshold decision → optional `get_kb_guidance`).
- **Where the numbers come from:** `eval/gold_set.json` (the labelled test cases)
  scored by `eval/harness/*` against the live tool + the live agent prompt (v54).
- **Latest headline (v54):** retrieval recall@3 98.6%, end-to-end top-1 91.8%,
  out-of-KB rejection 100%, display precision 100%.

---

## The dataset (one file drives all 4 layers)

Every case in `gold_set.json` has a **tier** that says what *should* happen. The
tier is what lets one dataset score four different things.

| tier | meaning | example query | expected |
|---|---|---|---|
| `strong` | clear match — should be found, ranked #1, and displayed | "outlook won't open at all" | KB0013608 |
| `weak` | on-topic but vague — should be *retrieved* but **not** displayed as a fix | "something seems off with my outlook calendar" | KB0015622 (held) |
| `ambiguous` | under-specified — should trigger a follow-up question | "my email isn't working in outlook" | KB0016493 (after Q) |
| `out_of_kb` | no valid doc — should be rejected | "what is the weather today" | (none) |

Current set: 83 cases (66 strong, 3 weak, 4 ambiguous, 10 out-of-KB) across 8 docs.

> **Scaling rule:** adding docs/cases never changes code or metrics — you only add
> rows here (and KB entries to `kb_meta.json`). The same four formulas recompute.

---

## Layer 1 — Retrieval quality

**Question it answers:** *Does the search find the right document, and rank it first?*
This tests the `get_kb_candidates` tool **alone** — no LLM involved, so it is
deterministic (same input → same score every time).

**How it's scored** (over every case that names a real doc):

```
recall@K  = (# cases where the expected KB appears anywhere in the returned K) / N
top-1     = (# cases where the expected KB is the FIRST returned)             / N
MRR       = average of (1 / rank-of-expected)        # rank 1→1.0, rank 2→0.5, absent→0
```
(`K` = RETURN_K = 3.)

**Worked example — a hit:**
Query `"how to turn off work offline in outlook"` (expected **KB0016493**).
The tool returns:
```
1. KB0016493  score 3.703   ← expected, and it's #1
2. KB0015711  score 2.80
3. KB0010265  score 2.16
```
→ recall@3 = hit, top-1 = hit, reciprocal rank = 1/1 = **1.0**.

**Worked example — a near-miss (where the 84.9% comes from):**
Query `"nothing happens when I click the outlook icon"` (expected **KB0013608**, "can't open").
The tool returns:
```
1. KB0010265  score 3.1    ← "stuck loading" — confusable, ranked #1
2. KB0013608  score 2.9    ← expected, but only #2
3. KB0015622  score 2.3
```
→ recall@3 = **hit** (it's in the top 3), but top-1 = **miss**, reciprocal rank = 1/2 = **0.5**.

**v54 result:** recall@3 **98.6%** (the right doc is almost always retrieved),
top-1 **84.9%** (ranking trips on confusable pairs like can't-open vs stuck-loading).
Because there's no LLM here, 84.9% is exact and stable — improving it is a
**search/ranking** task (differentiate the confusable docs), not a prompt task.

---

## Layer 2 — Threshold calibration (the two-threshold model)

**Question it answers:** *Are we showing the right docs and rejecting the rest?*
The app uses two thresholds on the reranker score (0–4 scale):
- `MIN_SCORE` (1.9) — below this, a candidate isn't returned at all.
- `DISPLAY_MIN_SCORE` (3.0) — below this, a candidate is still returned (so it can
  power a follow-up) but flagged `meets_display_threshold = false`, i.e. *not* shown
  as a resolution.

**How it's scored** (by tier):

```
display TP  = strong cases whose top candidate has meets_display_threshold = true
display FP  = out_of_kb cases that returned any_meets_display_threshold = true
display precision = TP / (TP + FP)        # of what we displayed, how much was right
display recall    = TP / (# strong)       # of what we should display, how much we did
out-of-KB rejection = (# out_of_kb with NO confident display) / (# out_of_kb)
weak correctly held = (# weak returned but NOT displayed) / (# weak)
```

**Worked example — strong (should display):**
`"how to turn off work offline in outlook"` → top score **3.703 ≥ 3.0** →
`meets_display_threshold = true` → counts as a **display TP** (correctly shown).

**Worked example — weak (should be held):**
`"something seems off with my outlook calendar"` → returns KB0015622 but top score
**2.703 < 3.0** → returned (so the agent can ask a question) but **not** displayed →
counts as **weak correctly held**.

**Worked example — out-of-KB (should be rejected):**
`"what is the weather today"` → no candidate clears MIN_SCORE →
`any_meets_display_threshold = false` → **correctly rejected** (and would be a
display FP only if it had shown something).

**The threshold sweep** re-scores the *same* live results at different
DISPLAY_MIN_SCORE values so you can pick the knee without code changes:

| DISPLAY_MIN_SCORE | display recall | false display |
|---|---|---|
| 2.4 | 92.4% | 0% |
| 3.0 (in use) | 57.6% | 0% |

Reading it: at the current 3.0, only 57.6% of true matches are "displayable," yet
false displays stay at 0% all the way down to 2.4 — i.e. **3.0 is conservative;
lowering it toward ~2.4 would surface more correct docs with no extra false hits.**

**v54 result:** display precision **100%**, out-of-KB rejection **100%**,
display recall 75.8%, weak correctly held 66.7%.

---

## Layer 3 — Clarification quality

**Question it answers:** *When the agent is unsure, does it ask a grounded follow-up
question — and does asking actually help?* This runs the **ambiguous** cases through
the **real agent**, with a simulated user answering its questions.

**How the simulated user answers:** each ambiguous case carries a `dialog` with a
keyword→answer map. When the agent asks a question, the simulator matches a keyword
in that question and replies accordingly. Example case `CLAR-01`:
```json
"query": "my email isn't working in outlook",
"dialog": { "answers": {
    "offline": "yes, it says Working Offline at the bottom right",
    "outbox":  "no, nothing is stuck in the outbox",
    "open":    "no, outlook opens fine" },
  "expected_after_followup": "KB0016493" }
```

**How it's scored:**
```
spread=high on first call = (# ambiguous where first retrieval returned spread="high") / N
clarification trigger     = (# that actually ASKED a question | spread was high) / (# spread high)
questions grounded        = (# whose follow-up reused the returned discriminating_symptoms) / (# that asked)
resolved after follow-up  = (# that reached expected_after_followup) / N
avg rounds                = average number of follow-up turns used
```
"Grounded" = the agent's question shares vocabulary with the `discriminating_symptoms`
the tool returned (token overlap ≥ 0.15), i.e. it asks about real differentiators
rather than inventing.

**Worked example:**
`CLAR-01` "my email isn't working" → first retrieval returns KB0016493 / KB0015711 /
KB0010265 with **spread = high** (3 plausible matches). The agent asks *"Is Outlook
showing 'Working Offline'?"* (grounded — drawn from the symptoms). Simulator matches
`offline` → "yes, it says Working Offline…". Agent then classifies **KB0016493** →
resolved.

**v54 result & important caveat:** trigger 100%, grounded 75%, but
resolved-after-followup **0%** with avg rounds 5.0 — which **contradicts Layer 4**,
where the same CLAR cases resolved 3/4. The cause: only **4 ambiguous cases** + the
model is **non-deterministic** (gpt-5.4-mini ignores `temperature=0`), so one unlucky
run swings the number hard. **Layer 3 needs more ambiguous cases and multi-run
averaging before its absolute number is trustworthy.**

---

## Layer 4 — End-to-end outcome

**Question it answers:** *Run a full conversation — is the final answer correct, and
routed correctly?* This runs **every** case through the real agent (simulator
answering any follow-ups) and scores the final result the user would see.

**How it's scored:**
```
end-to-end top-1   = (# in-KB cases whose FINAL classification == expected) / (# in-KB)
out-of-KB correct  = (# out-of-KB cases the agent did NOT assert a KB for) / (# out-of-KB)
guidance routing   = (# correct picks that called get_kb_guidance iff the KB's
                       guidance_troubleshoot flag is true) / (# correct picks)
avg rounds         = average follow-up turns across all cases
```

**Worked example — correct + routed:**
`"outlook says working offline how do I fix"` → agent retrieves, commits to
**KB0016493** (correct). KB0016493 has `guidance_troubleshoot = true`, so it then
calls `get_kb_guidance` to fetch the steps → **guidance routing correct**.

**Worked example — a miss:**
`KB0015711-05` "my mail only sends after I hit send receive" → the agent kept asking
follow-ups and hit the 5-round cap without committing → `predicted = None` → counts
as a **miss** (it didn't get the answer wrong, it failed to *commit*). This
"never-commits" mode was the main v49 problem and is now rare in v54.

**Worked example — out-of-KB handled right:**
`"how do I install excel"` → agent finds no valid doc and does **not** assert a KB →
`predicted = None` → **out-of-KB correct**.

**v54 result:** end-to-end top-1 **91.8%**, out-of-KB correct **100%**,
guidance routing 67.2%, avg rounds 1.63 (commits quickly).

---

## How the gate turns metrics into pass/fail (CI)

`config.json → gates` sets a target per metric; `run_all.py` compares actuals and
**exits non-zero if any target is missed** — so it can block a regression in CI.

| Check | Target | v54 actual | Status |
|---|---|---|---|
| layer1_recall_at_k | ≥0.98 | 98.6% | PASS |
| layer1_top1 | ≥0.85 | 84.9% | FAIL (0.1% under) |
| layer2_out_of_kb_rejection | ≥0.90 | 100% | PASS |
| layer2_display_precision | ≥0.85 | 100% | PASS |
| layer3_clarification_trigger | ≥0.75 | 100% | PASS |
| layer4_end_to_end_top1 | ≥0.90 | 91.8% | PASS |

A non-zero exit here means "something regressed, look before shipping" — not that the
run errored.

---

## Two caveats to state when presenting

1. **Reasoning-model nondeterminism.** gpt-5.4-mini ignores `temperature=0`, so the
   agent layers (3 & 4) vary run to run. Report a **3–5 run average** for the
   agent-dependent numbers; the tool layers (1 & 2) are deterministic and exact.
2. **Simulator realism.** The simulated user answers from scripted symptom text. A
   real user answering specifics would likely converge more often, so the
   conversational numbers are a **conservative floor**, not a ceiling.

## One-line summary of the strategy

> We label every test utterance with what *should* happen (find / display / clarify /
> reject), then score four independent layers — retrieval finds it, thresholds decide
> whether to show it, clarification handles ambiguity, and the full conversation lands
> the right answer — each with a simple, fixed formula and a CI gate, so quality is
> measured the same way whether we have 8 documents or 200.
