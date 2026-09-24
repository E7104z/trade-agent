# DATA CONTINUITY SPEC v1 — no outage may cost data that didn't need the outage's service

**Status:** proposed, 2026-09-23 ([SELF_ASSESSMENT_2026-09-23.md](SELF_ASSESSMENT_2026-09-23.md) P5, G2.1–G2.3)
**Goals served:** 2 (primary), 4
**Scope:** `agent/loops/morning.py`, `agent/loops/evening.py`, `agent/population.py`, `agent/ledger.py`, `agent/health.py`, `agent/failures.py`, `agent/delivery.py`, `api/index.py`, the next free migration, tests.
**Writer isolation:** unchanged. Nothing here touches `playbook.md`.

---

## 0. The problem

The directive says the system is data-volume-bottlenecked and that every lost day is permanent. Three mechanisms still lose days.

### A — an LLM outage deletes deterministic data

Anthropic credit ran out on these runs (`cc28fbb`):
- morning on 2026-09-02 and 09-04;
- weekly on 09-04;
- runner run 63 and evening runs 64, 65 and 66 on 09-22.

It will happen again. On those days the loops also lose data that needs no LLM:

| Lost on LLM failure | Where | Why |
|---|---|---|
| Population snapshot: every premarket gapper, the only input to the long/short population monkeys | `integrity.on_morning`, `morning.py:1120` | runs after the analyst call (`:951`) and the risk call (`:982`) |
| Calibration rows, and the deterministic open-question increments (Q1, Q3–Q7, Q9–Q14) | `_v3_events`, `evening.py:870` | runs after the grader call (`:820`) |

Runner candidate pricing, shadow grading and outcome simulation already run **before** the grader call, which is correct. This spec extends that ordering to everything that doesn't need the model.

Nothing warns about credit before a run dies, and the 09-22 exhaustion was diagnosed from scratch after four failed runs.

### B — silent provider degradation

Alpaca daily bars returned 403 "on EVERY recent date" and fell back to yfinance, which returned nothing for listed names. That nulled day OHLC and prev close in grading (`85a7993`). It was found by audit, not by the system.

The evening loop has no session-level plumbing check. If bars are missing for every symbol, each row gets `exit_reason='no_data'` (`grading.py:444-445`) and the run is logged `ok`. The morning (`session_degraded`) and runner (`provider_degraded`) lanes have this check; grading does not. That is the directive's case of "simultaneous nulls across all tickers = plumbing", unhandled where the outcomes are written.

### C — the time budget has no guard

Evening runs took 455–518 s on runner-heavy days against Vercel's 300 s budget (`85a7993`). A killed function logs nothing (`health.py:5-9`). Prefetch brought a real day down to 72 s, but nothing records stage durations, so a regression will come back silently.

---

## Phase 0 — production verification (hard blocker; a self-check, not a sign-off)

Data access means an authenticated `/api/table/...` session or a local `DATABASE_URL`. If neither is available, record Phase 0 as blocked and stop. Never search the machine for credentials.

1. **Outage inventory.** Pull `/api/table/runs?limit=200` and list every `error` / `api_error` run with its loop, date and first line of error. Classify each: provider credit, provider other, timeout (no row at all — find it by the absence of a run for an expected session), or code.
2. **Data lost on those days.** For each LLM-failure date, check whether `population_universe` has rows for that `plan_date` (via S1 Phase 1), and whether `calibration` has rows for that `session_date`. Expected: missing on morning-failure and evening-failure days respectively. **If they are present, the premise is wrong: stop and amend.**
3. **Idempotency hazard.** `ledger.add_question_progress` (`ledger.py:1736-1742`) is a bare INSERT. Query `/api/table/open_question_progress?since=2026-08-19&limit=1000` and look for duplicate `(question_id, session_date, note)` rows. They would show that an evening has already run its v3 events twice.
4. **Null baselines.** For the last 10 clean sessions, compute the per-field null rate of the grading inputs:
   - `outcomes.day_open`
   - `outcomes.atr14`
   - `outcomes.rvol_at_1000`
   - `runner_candidate_outcomes.atr14`
   - `trend_side_200d`

   These baselines set the Phase 3 thresholds (see there). Do not invent thresholds.
5. **Budget.** From `runs`, `finished_at − started_at` for evening runs over the last 15 sessions; record the maximum.

---

## Phase 1 — deterministic work first

1. **Morning.**
   - Take the population snapshot immediately after the market bundle is built and before any LLM call: `population.snapshot_universe(con, d, gappers, picked=[])`.
   - The existing call at the end of the run stays. `snapshot_universe` already does DELETE-then-INSERT per `plan_date` (`population.py:148`), so the second call just sets `was_picked`.
   - If the LLM fails, the morning's population rows still exist, and the evening grades them.
2. **Evening.** Split `_v3_events` in two:
   - `_deterministic_events`, run **before** the grader call:
     - calibration rows (`add_calibration_row` is already idempotent per `(session_date, ticker)`);
     - Q1, Q3, Q4, Q5, Q6, Q7, Q9, Q10, Q11, Q12, Q13, Q14;
     - Q2 and Q8, which read only stored plan fields.
   - `_llm_events`, run after the grader: rule confirmations only. Those are the only events that read `review`.
3. **Idempotency.** Before `_deterministic_events` writes, delete that session's rows for the evening-owned question ids. Then a retry replaces rows instead of adding them. Unit-test: running the evening twice for one date yields the same `open_question_progress` rows as running it once.
4. **Stage status on the runs row.** If the grader call fails after deterministic persistence, the run records `api_error` and a new `runs.stages` JSON (for example `{"deterministic": "complete", "llm": "failed"}`). `health.py` then reads "data collected, grading LLM failed" rather than "no evening recorded".

Tests:
- With a mocked LLM that raises, the evening still writes outcomes, runner outcomes, shadow outcomes, calibration and question progress, and the runs row carries both stage statuses.
- The morning with a raising LLM still writes `population_universe`.

## Phase 2 — fail loudly and early on provider outages

1. When `failures.classify_failure` returns `api_error` and the message matches the non-retryable credit or auth markers `cc28fbb` introduced, send an alert email through `delivery.send_email`. Subject: `LLM provider refusing requests — deterministic data still collected`. Send at most one per provider condition per day; dedupe with a row in a small `alerts` table.
2. The dashboard health panel shows a red banner for any `api_error` in the last 24 h, with the classified cause.
3. Money is not code (trade-off T5). Topping up credit or enabling auto-reload is Jason's decision. The spec only guarantees he hears about it on the first failure, not the fourth.

Test: a credit-exhaustion exception sends exactly one alert across three failing runs on the same day.

## Phase 3 — coverage ledger and plumbing detector

1. Add a new table, `data_coverage`, with columns `session_date, lane, stage, field, n_total, n_null, source, created_at`. Writers:
   - the morning bundle: quotes, earnings, ATR per candidate;
   - evening grading: minute bars, daily bar, prev close, ATR, RVOL, per graded row;
   - runner scan: RVOL, float, earnings, per candidate.
2. **Plumbing rule.** Mark a stage `degraded` with reason `plumbing:<field>` when both hold:
   - that field's null fraction is **at least Phase 0's clean-session baseline plus a margin**; derive the margin from the observed spread and record the derivation in a code comment;
   - `n_total` is at least 5.

   A degraded evening stage flags the session exactly the way `session_degraded` does for the morning. The weekly `DEGRADED SESSIONS` block (`weekly.py:329-371`) then tells the miner to exclude it from behavioural claims.
3. **All-null grading.** If every graded row in a lane is `no_data`, the evening stage is `degraded:plumbing:minute_bars` and the run status is `degraded`, not `ok`. `health.py` already treats `degraded` as a warning.
4. Add `data_coverage` to `_QUERYABLE_TABLES`, and add a dashboard panel with the last 10 sessions by lane × field.

Tests:
- A fixture with all bars missing yields `degraded`.
- A fixture with one missing symbol yields `ok`.
- The thresholds come from a constant whose comment cites the Phase 0 measurement.

## Phase 4 — time-budget guard

1. Record per-stage elapsed seconds into `runs.stages`: prefetch, simulate, shadow, population, runner, LLM, persist.
2. Log a warning, and show it in health, when any run exceeds 70% of 300 s.
3. Add a regression test with a synthetic runner-heavy day (300 candidates, mocked provider latency) that asserts the prefetch path is taken. It asserts call counts, not wall-clock time.

---

## Explicitly out of scope, flagged for Jason (T5)

- **Vercel Pro:** `*/5` crons would remove the dependency on the Windows machine running `TradeAgent-Intraday`.
- **Once-a-day crons have no retry.** Phase 1 of this spec narrows what an outage costs, but it can't re-fire a missed cron. A missed morning is still a missed morning.

## Constraints

- Reordering must not change any number. Outcomes, calibration values and increments are byte-identical to today on a clean run. A test compares a clean-day fixture before and after the change.
- Every new write path is guarded, so a failure in coverage accounting never fails a trading loop (v4-1).

## Acceptance (production evidence)

- The next `api_error` in `/api/table/runs` coexists with `population_universe` and `calibration` rows for the same date, and exactly one alert email.
- `/api/table/data_coverage?since=<ship date>` has rows for every lane and session.
- Update G2.1 and G2.2 in the self-assessment, citing the dates.
