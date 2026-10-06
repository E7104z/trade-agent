# MORNING BUDGET SPEC v1: the morning in two invocations, and the 2026-10-05 evening review

**Status:** built 2026-10-05, the night of the first full-stack session (v28 arms, persona, trader's log). Jason forwarded a read of that evening's email and the dashboard's eight stuck-lock warnings and said: "Fix the issues."
**Goals served:** 2 (primary: a killed morning loses a day's plan, arm B and its lock), 1, 3, 4.
**Writer isolation:** unchanged. Nothing here writes `playbook.md`.
**Schema:** v35 (`morning_checkpoints`; `runner_sessions.selection_completed_at`).
**Prompts:** risk v16, grader v9. Analyst unchanged; its hypothesis section text changed, so it renders only when the lane is on.

---

## 0. What the review found

| # | Finding | Evidence | Part |
|---|---|---|---|
| 1 | The morning dies at Vercel's 300s limit | 291s of 300s on 10/05, with the risk manager at 127.6s. Lock left `in_progress` on 9/11, 9/15, 9/17, 9/28, 9/30, 10/01, 10/02 and 10/05. | 1 |
| 2 | The dashboard listed all eight dead locks and said "the next attempt takes it over automatically" | Nothing ever reruns a past morning. | 1 |
| 3 | The runner selected 0 of 533 candidates and gave no reason | `selection_made` was set and nothing else: the claiming request died before its result. | 2 |
| 4 | Triggers already crossed at the open | CWH: trigger 4.90 vs a 4.91 open, filled 4.60, 0.86R through. CHRW: opened 3.1% beyond its target. | 3 |
| 5 | +0.01R counted as a winner | ALEC: $0.0028 a share on a $2 stock, inside the spread. | 4 |
| 6 | The grader misread RVOL | It treated cumulative RVOL as pace (NVDA 0.16x at 10:00 is ~18% of the day). | 5 |
| 7 | "One-and-done" contradicted the grader's own numbers | ALEC: 2.49x → 6.88x. | 5 |
| 8 | NVDA got a C for being a coverage pick | The lane exists to force sampling. | 5 |
| 9 | "-0.14R forgone confirms filters are working" | That is 0.02R per rejection, and the line said 7 when the R covered 6. | 5, 6 |
| 10 | H-de095870 is a five-condition conjunction with a misread mechanism | It is unlikely ever to reach 8 sessions. | 5 |
| 11 | The exit-replay lines carry no names; "(one session)" follows every number | Trader's log | 6 |
| 12 | backlog_probe has not fired in 9 sessions | Coverage (since 9/29) trades every non-broken rejection; a probe was held to a stricter bar (rr/conviction kills only). | 7 |

---

## Part 1: the morning in two invocations (`agent/loops/morning.py`, `agent/dispatch.py`)

Each LLM stage takes ~130s against Opus with thinking. With the data bundle, one invocation cannot hold both stages, and every new prompt section adds to them. Bounding arm B's wait (7f8099d) could not help, because arm A alone was at 291s.

- **The analyst half (`run`)** covers the bundle, regime, memory, the analyst call with arm B's analyst on a thread beside it, the candidate quotes and the correlation matrix. It writes one checkpoint row: a JSON payload carrying everything the risk half reads, including the analyst half's bundle health, which a fresh MarketData cannot see.
- **The hand-off.** On Vercel, the analyst half calls `GET /cron/morning-continue?d=&rid=` on its own production domain with the cron secret. It stops waiting after 3s, and a read timeout means the request was delivered. Vercel does not cancel a function when its caller disconnects unless `supportsCancellation` is set, which it is not here. So the risk half starts in a fresh invocation with its own 300s.
  - If the hand-off fails and ≥200s of the budget is left, the risk half runs in the same invocation.
  - Otherwise the risk half is left to the safety net.
- **The risk half (`resume`)** atomically claims the checkpoint. It verifies that the run lock still belongs to this run, then runs the risk manager with arm B's risk manager on a thread beside it, validation, the lanes, persistence, carry cleanup, orders and the email. It releases the lock and marks the checkpoint `complete`, dropping the payload.
- **The safety net** is the `/cron/morning-continue` cron at `15 12 * * 1-5` (08:15–09:14 ET in summer, 07:15–08:14 in winter). It takes a checkpoint nobody claimed, or one whose holder died (claimed more than 10 minutes ago). With no plan and no live checkpoint, it reruns the whole morning, and the run lock decides whether that is allowed.
- **Never twice.** If a retaken checkpoint finds a plan already saved, it closes the run without re-running it. The picks and orders already exist.
- **Locally and in mock,** both halves run in one process through the same JSON round trip. `run.py morning` is unchanged.
- **`runs.stages`:** `timings_s.total` is now the slowest *invocation*, the number the 300s applies to. `invocations` names both halves.
- **Dead locks.** The dashboard warns only about the lock for the session a loop would run now, which is the only one an attempt can still take over. The self-check's "Morning time budget" keeps the history and now names which half died ("risk half never started", "risk half died mid-run") and an arm B killed mid-run.

**T1 (Goal 2 vs cost).** The hand-off depends on the platform not cancelling a disconnected request. That is documented behavior, and the cron is the backstop. If both fail, the morning is lost exactly as before, but it is now named, not silent.

## Part 2: a runner selection a killed request cannot lose (`ledger.claim_runner_selection`, `agent/loops/runner.py`)

- **Retaking a dead claim.** A claim can be taken over 10 minutes after it was made, but only when it left no trace of a result: no `selection_completed_at` (v35), no reason, no broker note, and no candidate marked selected. Any trace keeps the claim, so a finished selection is never made twice. The retake records its own decision time.
- **Budget guard.** A hosted tick whose scan has used more than 210s of its 300s does not claim; the next tick, five minutes later, starts fresh.
- **Reporting.** `ledger.runner_selection_note` explains the session: decided, unfinished (the claim died), not run, or no session. That reaches the trader's log (`runner.selection`) and a new self-check, "Runner selection".

## Part 3: plan-time gate for triggers the open will cross (`_validate_plan`)

These gates apply to every lane, including coverage, floor, hypothesis and probe picks.

- **Breakout trigger too close.** A breakout whose trigger sits less than `breakout_min_trigger_r` (0.25) of planned risk beyond the last price is dropped. That includes a trigger the price has already crossed. Such a trigger is crossed by an ordinary opening wiggle before the 09:45 window and fills wherever price is then.
- **Target already reached.** An `open` or `breakout` entry whose target the last price has already reached is dropped. A limit that buys a pullback below a price already past its target is a real trade (the reward is the bounce) and stays.

The simulator's invalid_fill gate stays the backstop for moves after the plan is written.

## Part 4: the scratch band (`grading.SCRATCH_R` = 0.05R, `grading.outcome_class`)

A result inside ±0.05R is a scratch: neither a win nor a loss. The band is the desk's own round-trip cost: 5 bps a side over a typical ~2% stop is 0.05R.

- **Where it applies:**
  - `stats_by`, `overall_stats` and the calibration aggregate, which now reads realized_r rather than the stored flag, so history counts the same way.
  - Structural win-frequency mining.
  - The trader's log (`day.scratches`) and the day's contrast table.
  - The losing-streak rule: a scratch neither extends nor breaks a streak.
- **Mean R, the sequential tests and the FDR batch are untouched,** because they read R itself.
- **The shadow ledger keeps its own ±0.2R band.**

**T2 (Goal 1).** Win rates now read lower wherever ±0.05R exits had counted as wins. History is reclassified the same way; no number is restated.

## Part 5: what the grader is shown (`grading.outcomes_md`, `prompts/evening_grader.md` v9)

- **RVOL pace.** RVOL@10:00 and RVOL@12:00 are shown with the share of the day's volume done by then (RVOL@T ÷ RVOL@close). That ratio is the stock's own intraday curve, so no outside volume profile is inherited.
  - The prompt now defines RVOL as cumulative over full-day ADV20.
  - A claim about participation must cite RVOL at two checkpoints and agree with them.
  - A low VWAP time-share with few crosses is a one-sided session, not chop.
  - A mechanism the numbers contradict is dropped.
- **Test lanes.** Every coverage, floor, hypothesis, R:R-control and probe pick carries a `Lane:` line. The grader may not mark one down for having been taken. A label that does not fit is reported as a construction finding.
- **Scratches** are marked in the outcome line. Their sign is noise.
- **Shadow line.** It now reads "N scored, M priced (k would have triggered; the rest unmeasurable), net forgone over the priced, R per priced rejection", followed by: one session confirms or refutes nothing.
- **Testable ideas.** A hypothesis statement naming more than three numbers is refused by the register (`MAX_STATEMENT_NUMBERS`), and both prompts ask for one core condition.
  - H-de095870 stays as written. If it is never tested, the existing 30-session expiry closes it.
  - The analyst is told that an idea left untested for many sessions is too narrow.

## Part 6: the trader's log (`agent/traders_log.py`)

- **Exit-replay values name their policy:** plan, arm A stop floor, arm B dashboard rules, arm C floor + dashboard.
- **The one-session scope is stated once.** The rendered "What happened" heading reads "(one session, not evidence)". A one-session value cited in any other section keeps its own "(one session)". This amends TRADERS_LOG_SPEC_v1 §103: same honesty, without the label after every number.
- **The shadow facts** now give scored, priced, net over the priced, and per priced rejection.

## Part 7: backlog probes on coverage's terms (`_add_exploration_pick`, risk prompt v16)

A rejected probe now trades as a probe unless its kill is a broken order (coverage's `blocking_kills`), a mislabel (`setup_fit`) or `hypothesis_fit`. A probe aims at a *cell*, not a setup, which is exactly the trade coverage cannot add once its setup is filled.

The self-check now counts probe-tagged candidates that reached the tape under *any* lane label. An approval or coverage pick collects the same observation, so the lane is flagged only when tagged probes reach the tape by no lane at all.

---

## Acceptance (read by the system, not a person)

- **Morning:** the evening SELF-CHECK "Morning time budget" shows both invocations under 300s and no "risk half" line. `morning_checkpoints` holds `complete`, with `dispatch` starting `http`.
- **Runner:** "Runner selection" shows `selected N` on every session with candidates.
- **Grader:** the next grader output carries no "confirms filters" and no lane penalty. Lessons that quote RVOL cite two checkpoints. The persona/grounding counters in the evening log are unchanged.

## Operations

The deploy carries migration v35. Warm the schema right after it, before the next morning cron: load the logged-in dashboard, or run `scripts\intraday_cron.ps1 -Once`. Vercel registers the new cron at deploy.
