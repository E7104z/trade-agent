# Morning entry window (v1) — 2026-10-02

**Status:** implemented and deployed with migration v27 and analyst prompt v11. The first plan it applies to is 2026-10-05; the 10/02 picks carry no window and trade and grade exactly as they were placed.

## The defect

The analyst prompt has always said "no entries in the first 15 minutes after the open", but nothing in the code enforced it.

- **Live orders:** the morning run sent every order at ~07:11 ET as a day order, live from 09:30.
- **The simulator** filled limit and breakout entries from 09:30 too.
- **Result on 2026-10-02 (NFP):**
  - The plan read "no entries before 09:45 ET".
  - The NKE short limit (31.70) sat 0.3% through the premarket price (31.80), and the WDC short limit (424) sat 0.6% through 426.65. Both were set to fill on the opening print.
  - That contradicts NKE's "retest after NFP settles". WDC's "continuation below the premarket low" needs a stop entry, not a limit.
  - The entry-type check allows a limit up to 1% through the price (a buffer for stale quotes), so neither was flagged.

## The fix: one rule, read by everything

- **The plan carries it.** The morning validator stamps `entry_not_before = 09:45` (`morning_entry_not_before`, default `grading.MORNING_ENTRY_NOT_BEFORE`) on every limit or breakout pick, including coverage and floor picks.
  - The same stamp goes on every shadow-ledger rejection and every paired-arm pick, so both comparison arms are priced under the rule the desk trades under.
  - `open` entries carry none: the opening print is the point of an open entry.
- **The simulator honors it.** `simulate_pick` starts a windowed pick's fill search at the window. Untriggered diagnostics (missed move, trigger gap) are measured over the bars the order was live for.
  - A row without the field (everything before 10/02) grades exactly as before, so there is no sim_version bump.
- **The live order goes in at the window.**
  - The morning run sends only `open` entries (`morning._place_premarket_orders`).
  - The intraday tick sends windowed picks at the first tick at or after 09:45 (`live_monitor.place_deferred_orders`), with the morning run's own sizing, bracket and per-day cap.
  - It does not send a limit whose price is already through its stop; the simulator grades that fill `invalid_fill`, so the two agree.
  - It does not send a pick whose time stop passed first.
  - Every outcome is recorded in `deferred_placements`. A broker refusal is retried on the next tick; a skip is final.
- **Nothing silently goes missing.** The evening reconcile names any windowed pick that never reached the broker (for example, no intraday tick ran at 09:45).
- **The analyst is told how each order type executes** (prompt v11):
  - a limit rests on the far side of the market and fills at that price or better;
  - a limit already through the price at 09:45 is a market order at 09:45;
  - "continuation through X" is a `breakout` at X;
  - the thesis text and the order type must describe the same trade.
- **The plan says it.** The email and the dashboard plan table show "limit @ 31.70 (live 09:45 ET)".

## Known limit

Breakout entries are still not placed live while `live_paper.breakout_two_step` is off (unchanged; the simulator grades them). The placer records that as a skip with its reason instead of dropping it silently.
