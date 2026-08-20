# Evening Grader — Spec Changes for Playbook v3

**File:** `evening_loop.py`
**Model:** Claude Sonnet
**Trigger:** 16:30 ET, weekdays
**Purpose:** Grade the day's picks against realized data AND populate the four new
v3 memory structures: calibration table, shadow-ledger outcomes, decay/last_confirmed
stamps, and open-question progress counters.

This spec is ADDITIVE. The existing grading behavior (per-trade grade, MFE/MAE,
lesson extraction) stays. Below are only the changes.

---

## 1. Grade rejected candidates, not just taken trades

The morning loop writes two record types to the ledger: `picks` (taken) and
`rejections` (killed by the adversarial reviewer, with a `reason` field). The
evening grader must now pull realized intraday data for BOTH.

For every rejection row, compute:
- `eod_outcome`: did the rejected thesis play out favorably by close? Enum:
  `would_have_won` | `would_have_lost` | `scratch` | `unmeasurable`.
- `forgone_r`: signed R the rejected thesis would have returned had it been
  taken at the plan's stated entry/stop/target. Positive = the filter cost us.
  Negative = the filter saved us. Null if `unmeasurable`.
- Preserve the original `reason` string verbatim so the weekly miner can group
  forgone R by rejection reason.

Write these back to the rejection rows. Do NOT let rejection outcomes touch the
taken-trade equity curve — they are counterfactual and live in the shadow ledger
only.

## 2. Populate the calibration record

For every TAKEN pick graded today, append one row to the `calibration` table:
`{session_date, ticker, stated_conviction (1-5), realized_r, win (bool)}`.

The grader does not compute the aggregate table — that is the weekly miner's job.
The grader only appends the atomic rows. Keep conviction as the agent stated it
in the morning plan; never back-fill or adjust it with hindsight.

## 3. Stamp last_confirmed on any rule the day's trades touched

When a taken trade's setup matches a playbook rule AND the trade's grade is a
process-pass (regardless of P&L — principle 2), emit a
`rule_confirmation` event: `{rule_id, session_date}`. The weekly miner uses the
latest such date as the rule's `last_confirmed`. A process-FAIL does not confirm
a rule even if it made money. Do not write to `playbook.md` from the evening
loop — only emit events; the weekly miner owns the file.

## 4. Advance open-question counters

Each open question defines exactly what observation increments it. The grader
checks today's trades/rejections against each and emits `open_question_progress`
events: `{question_id, increment, note}`. Specifically:
- Q1 (gap_fade mechanics): on any gap_fade candidate, log BOTH the failed-bounce
  fill price and the open-print price and the R each would have produced.
- Q2 (sympathy standalone): increment only on sympathy trades taken standalone
  (leader not concurrently held).
- Q3 (conviction): increments automatically with every calibration row (§2).
- Q4 (gap-size): on any gap trade, log the gap % bucket and realized R.
- Q5 (shadow filter cost): increments once per session that produced >= 1 rejection.

## 5. Output contract

`evening_loop.py` writes only to the ledger DB (new/updated rows + event tables).
It must NOT edit `playbook.md`. Emit a run summary to stdout: picks graded,
rejections scored, net forgone R today, calibration rows added, rule
confirmations emitted, open-question increments. This summary is what you eyeball
each night to confirm the loop ran clean.

## 6. Failure handling

If realized data is missing for a ticker (halt, data gap), mark that record
`unmeasurable` rather than guessing. An unmeasurable rejection contributes 0 to
forgone R and does not increment any counter. Never fabricate an outcome to keep
a counter moving — a stalled counter is honest signal that data is thin.
