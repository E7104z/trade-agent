# GRADER GROUNDING SPEC v1 — no confirmation without a computed fact behind it

**Status:** proposed, 2026-09-23 ([SELF_ASSESSMENT_2026-09-23.md](SELF_ASSESSMENT_2026-09-23.md) P6, G4.2)
**Goals served:** 1, 4
**Scope:** `prompts/evening_grader.md`, `agent/loops/evening.py`, `agent/ledger.py`, `agent/grading.py` (a tape summary built from bars already fetched), `agent/delivery.py`; the next free migration; tests.
**Depends on:** S2's predicate schema (`agent/buckets.py`) for Phase 3. Phases 1, 2 and 4 stand alone.
**Writer isolation:** unchanged. The evening loop writes the ledger only.

---

## 0. The problem

The evening grader is the daily learning step. What it asserts becomes durable memory:
- confirmation counts drive lesson promotion (`weekly_miner.md:89`);
- an A/B grade stamps the setup's rule confirmation (`evening.py:435-438`), which is the playbook's decay clock until S2 replaces it.

Several of those assertions are not checked against anything computed.

| Vector | Evidence |
|---|---|
| The contract demands a narrative the input cannot support | `what_happened: "1-2 lines of tape narrative"` (`evening_grader.md:45`), while the prompt says "You are given only the pick's planned levels and the mechanically-computed outcome — no bar-by-bar chart" (`:30`). The call runs without extended thinking (`evening.py:821`). `apply_grades` stores `why`, falling back to `what_happened`, as `grade_notes` (`ledger.py:760`). |
| Lesson confirmations are asserted, not observed | `lesson_updates[].action = "confirm"` goes straight into `update_lesson_feedback` (`evening.py:865-867`, `ledger.py:795-806`). Nothing checks that any trade that day met the lesson's condition. |
| Confirmations double-count on retry | `update_lesson_feedback` does `confirmations = confirmations + 1` plus an INSERT, with no key. Evening retries happen (runs 64–66 on 09-22). |
| Contradictions have no date | Confirmations write a dated `lesson_confirmations` row; contradictions only bump a counter (`ledger.py:805-806`). So the holdout split (`promoted_at`) sees confirmations out-of-sample but not contradictions, which biases every lesson's tier upward. |
| Cited numbers are never checked | The prompt requires lessons to quote exact computed values: VWAP, RVOL, ATR, `stop_dist_atr` … (`evening_grader.md:26-28`). Nothing verifies that the numbers in `lesson` or `evidence` appear in the computed input. |
| One letter grade confirms a whole setup | Any A/B on a fill-valid, non-anomalous pick stamps `rule_confirmations(rule_id = setup_type)`. The prompt asks the grader not to inflate grades (`:72`); nothing measures whether it does. |

Existing guards that stay:
- `invalid_fill` reviews are dropped in code (`evening.py:85-110`);
- fully tainted days extract no lessons (`:848-856`);
- near-duplicate lessons are rejected at insert.

---

## Phase 0 — production verification (hard blocker; a self-check, not a sign-off)

Data access means an authenticated `/api/table/...` session or a local `DATABASE_URL`. If neither is available, record Phase 0 as blocked and stop. Never search the machine for credentials.

1. **Ungrounded confirmation rate.**
   - Pull `/api/table/lesson_confirmations?limit=1000`, `/api/table/lessons?limit=500`, and `picks` and `outcomes` since 2026-08-19.
   - For each confirmation, test whether any triggered, fill-valid pick on that `session_date` shares the lesson's `setup_type` (and `regime`, where the lesson carries one).
   - Report the share with no such trade. **If it is ~0, Phase 3's premise is weak: downgrade Phase 3 to monitoring only, and say so.**
2. **Duplicate confirmations.** Count `lesson_confirmations` duplicated on `(lesson_id, session_date)`. That is the retry double-count, or the LLM listing a lesson twice.
3. **Citation spot-check.** For the latest 20 lessons:
   - extract numeric tokens with a unit (`R`, `ATR`, `x`, `%`) and `HH:MM` times from `lesson` and `evidence`;
   - compare them with that date's `outcomes` and `runner_candidate_outcomes` fields.

   Report the mismatch rate, with examples.
4. **Grade distribution.** From `outcomes.grade`: the A/B share by lane and `setup_type`, next to `execution_anomaly` counts and `stop_atr_bucket` `<0.5` counts.

---

## Phase 1 — replace the unsupported narrative with a computed one

1. Add `grading.tape_summary(bars, outcome)`, deterministic and built from bars the evening loop has already fetched. It reports:
   - open;
   - high and low of day, with times;
   - fill time and price;
   - exit time and reason;
   - VWAP side at fill;
   - MFE and MAE in R;
   - minutes held.
2. The evening prompt input shows `Tape (computed): …` under each pick.
3. `what_happened` is **removed** from the output contract (`evening_grader.md`, prompt-version 6).
   - `why` must attribute the result to thesis, structure or market, using the computed fields.
   - `grade_notes` stores `why`; the computed tape summary is stored in a new `outcomes.tape_summary` column.
   - The email and dashboard render the computed tape, not model prose.
4. The prompt adds: "If a claim is not supported by a computed field shown above, do not make it."

Test: a fixture pick renders the same tape summary twice (deterministic), and the contract no longer contains `what_happened`.

## Phase 2 — numeric citation check

1. Add `evening._ungrounded_tokens(text, computed_strings)`. It:
   - extracts `-?\d+(\.\d+)?\s?(R|ATR|x|%)` and `\b\d{1,2}:\d{2}\b`;
   - checks each token against the set of strings **as they were rendered to the grader that day**: outcomes lines, indicator lines, tape summaries, the runner table.

   Matching is on the rendered string, ±1 in the last printed digit. Nothing is re-rounded.
2. **Lessons** (new or updated) containing an unmatched token are **not written**. Each is recorded in a new table `grader_rejections(session_date, kind, lesson_id, text, unmatched)`, which is added to `_QUERYABLE_TABLES`.
3. **Pick reviews** with an unmatched token keep their grade. They are marked `outcomes.grounded = FALSE`, and **an ungrounded review cannot stamp a rule confirmation**.
4. The RUN SUMMARY gains `lessons rejected (ungrounded)=N`.

Tests:
- "RVOL@10:00 0.8x" passes when the input rendered `0.80x`.
- "stop was 0.3 ATR" fails when the input rendered `stop_dist_atr=0.52`.
- Times match `10:12`.
- A lesson citing no numbers is unaffected. Phase 2 checks numbers that are present; it does not demand numbers.

## Phase 3 — confirmations must match a trade

1. `lessons[]` in the grader contract gains an optional `predicate`, in the S2 schema: lane plus 1–2 `{dimension, level}` conditions, validated against `agent/buckets.py` labels. A predicate that fails validation is dropped; the lesson is kept, without a predicate.
2. A `lesson_updates` entry (confirm **or** contradict) is accepted only if at least one of today's fill-valid triggered rows falls within the lesson's scope:
   - with a predicate: matched via `buckets.label`;
   - without one: `setup_type` equality, plus regime equality when the lesson has a regime;
   - with neither: the evening cannot confirm or contradict the lesson. Only the weekly miner can act on it, from ledger counts.

   Rejected updates are recorded in `grader_rejections` with kind `update_out_of_scope`.
3. **Idempotency.** Add a unique `(lesson_id, session_date, action)` event row. Contradictions get a dated row too (new table `lesson_contradictions`, or an `action` column on `lesson_confirmations`). The counters are recomputed from event rows, not incremented. A retry then changes nothing, and the holdout split sees both directions.
4. The weekly `holdout_tier` computation reads contradictions with dates too. The rule that lessons "contradicted more than confirmed get retired" (`weekly_miner.md:89`) now uses grounded counts.

Tests:
- A confirm on a day with no in-scope trade is rejected.
- A contradict is dated.
- Re-running the evening leaves the counters unchanged.
- A lesson with a predicate matches the same rows S2's `observe_session` would.

## Phase 4 — measure grade inflation; don't legislate it

1. Add a deterministic weekly table showing, by lane × setup:
   - A/B share;
   - the share of A/B grades where `stop_atr_bucket = '<0.5'` (stop inside noise, by the system's own Q9 bucket);
   - the share of A/B grades on `execution_anomaly` picks (which should be zero);
   - the share of A/B grades on ungrounded reviews (Phase 2).
2. It is shown to the weekly miner as fact, and on the dashboard. **No automated action.** The judgment stays with the grader; the system only makes its drift visible. This is the directive's "moves judgment earlier, never removes it".

---

## Trade-off (T-style, flagged)

Phases 2 and 3 will reduce the number of lessons written and confirmations counted. Promotion slows, which trades the *speed* of Goal 4 for Goal 1 integrity. Integrity wins: an ungrounded confirmation doesn't speed learning, it speeds the accumulation of text. Phase 0 check 1 quantifies the cost before shipping.

## Acceptance (production evidence)

- After one week, `/api/table/grader_rejections` shows the rejection rate by kind.
- `lesson_confirmations` has no `(lesson_id, session_date)` duplicates after the ship date.
- The evening email shows computed tape summaries.
- Update G4.2 in the self-assessment, citing the rates.
