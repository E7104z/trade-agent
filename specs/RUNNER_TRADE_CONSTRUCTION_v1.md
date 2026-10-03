# Runner trade construction (v1) — 2026-10-02

**Status:** implemented; full suite passing. Not deployed. Ships with migration v28, runner exit-policy family v4 and structural pair spec v2. Sim version (7) and scan version (3) are unchanged, so no corpus reset.

**Trigger:** Jason's 2026-10-02 review of the runner lane's low win rate. Its argument: the trade's construction, not the picker, is the lever.

- The 9-EMA stop sits inside normal noise. In v9, 992 of 1,003 runner trades had a stop under 0.5 ATR, and the 10/02 median stop was 0.8% of price.
- 5 bps a side then costs a median 0.12R, which lifts the breakeven win rate at 2R from 33% to about 37%. The 10/02 field won 17% (61 of 352).
- The whole field averages −0.25R (n = 1,003, p < 0.001), so a picker has nothing to choose between.

The review asked for two measurement fixes, three new dimensions recorded at entry, three alternative constructions ("arms") priced on every candidate in the same FDR batch, and nothing live until something validates on forward sessions.

Standing-directive goals moved:

- **Goal 1 (validated edge):** a stop or management change can now be discovered, quarantined and forward-validated. Before this, there was no path for one to reach a live order honestly.
- **Goal 3 (seek data):** the miner could not learn a stop. With 99% of rows on one stop rule, there was no variation to mine, and the arms create it.
- **Goal 5 (structural mining):** "does price keep going up after entry" is now measured apart from the exit rule, by bucket, under FDR.

---

## Phase 0: the brief, checked against the code

| Brief item | Finding | Action |
|---|---|---|
| RVOL bucket mislabelled ("1.5–2" holds every RVOL < 2) | **Already fixed** in 17a55df (deployed 2026-09-30). The labels are now `<0.5, 0.5-1, 1-1.5, 1.5-2, 2-3, 3-5, 5+`. v9 was written 2026-09-26, before the fix. | Live edges kept (see T8). |
| Re-mine on current rows before reading v9 | **Already enforced.** `runner_research.current_candidate_rows` pins mining to the running scan × sim version. The corpus restarted 2026-10-02 at scan v3 × sim v7, so no v9-era row is mined. | None. |
| Selection verdict from one contested session | **Already gated.** `selection_edge` needs ≥ 3 contested sessions (17a55df). The *why* was never visible. | New per-session selection coverage (§6). |
| Index tape, extension, staleness at entry | Not recorded anywhere. | §1–2 |
| Stop floor, dashboard Runner rules, combined | Not priced. Family v3 varied only the exit, on a fixed stop. | §4 |
| "Go live only on forward validation" | Exit policies had **no forward-validation path**: RUNNER_EXIT_SPEC Phase 3 was proposed, never built. A surviving arm had nowhere to go. | §5 |

**Why only one contested session (v9).** A session is contested only if both a selected and an unselected candidate are priced under the current scan and sim versions. Production causes in the code and telemetry:

- **The selector failed.** On 2026-09-22 the Anthropic call failed on credit balance, and `run_selection` records `selection_failed` and makes no picks. That is deliberate: a fallback heuristic would contaminate the selector's record.
- **Sessions were excluded by the version pin.** The weekly reads only the current scan × sim pair, and regrades were pending at the time.
- **Selection stamps were lost.** Before 2026-09-26, `save_runner_candidate_outcome` was DELETE-then-INSERT. `restore_runner_selection` repairs this.

The local ledger shows the same pattern: stamps on 5 sessions, only 3 graded, and one `selection_failed`. Production cannot be read here. The coverage table (§6) now names the reason for every session, so the next weekly shows it rather than leaving it to inference.

**Production data:** blocked, because Claude has no production read path. To verify after deploy, Jason pulls:

- `/api/table/runner_candidate_outcomes?since=2026-10-02&limit=2000`. Check that `entry_px`, `staleness_min`, `ext_vwap_atr`, `spy_vwap_pct`, `qqq_vwap_pct` and `drift_atr` are filled on graded rows.
- `/api/table/runner_exit_replays?since=2026-10-02&limit=10000`. Check for `policy_version` 4 rows, `arm_*` policies, and `stop_used`/`cost_r` filled.
- `/api/table/runner_sessions?limit=20`. Check `selection_skipped_reason` per session; it is the coverage input.

---

## 1. Entry-time context and continuation (schema v28, `agent/runner_entry.py`)

The evening grader writes these for **every candidate with an enterable bar**, traded or not and invalid fills included.

**Entry:**

- `entry_px` / `entry_et`: the first enterable price, which is the open of the first minute bar at or after `runner_scan.honest_entry_time`. That is the price `simulate_pick` fills an `open` entry at, before friction.

**At-entry fields** read **only bars that closed before the entry bar opened**. The entry bar itself is never read; the tests pin this with an outsized entry-bar print.

| column | meaning |
|---|---|
| `staleness_min` | minutes from Held (`first_pass_et`) to `entry_et` |
| `vwap_at_entry`, `ext_vwap_atr` | session VWAP through the bar before entry, and (entry − VWAP) ÷ ATR14 |
| `spy_vwap_pct`, `qqq_vwap_pct` | (last close − session VWAP) ÷ VWAP × 100 for each index, through the bar before entry |

**Outcome fields** are never shown to the selector.

| column | meaning |
|---|---|
| `drift_atr` | (session close − entry_px) ÷ ATR14; no stop, no target |
| `run_up_atr`, `draw_down_atr` | highest high and lowest low from entry to the close, vs entry_px, in ATR |

ATR14 is the daily ATR from completed sessions, so it was known at entry.

## 2. Three new mined dimensions (`agent/buckets.py`)

Edges were fixed on 2026-10-02 before any outcome under them existed.

| dimension | labels |
|---|---|
| `runner_index_tape` | `both_above`, `both_below`, `split` (SPY and QQQ vs their session VWAP at entry), `?` |
| `runner_ext_bucket` | `<0`, `0-0.15`, `0.15-0.3`, `0.3-0.5`, `0.5-1`, `1+` (ATR above VWAP at entry; the same ATR grid as the stop bucket) |
| `runner_staleness_bucket` | `<15m`, `15-30m`, `30-60m`, `60-120m`, `120m+` |

**Pre-registered pair (PAIR_SPEC v2):** `runner_index_tape × runner_ext_bucket`. The review's two suspects are "long momentum bought into a fading tape" and "the losers were extended above VWAP". The pair asks whether they compound. It is admitted to the batch only under the existing marginal-n rule.

## 3. Continuation after entry: the drift family

Realized R mixes "did price keep going up after entry" with "did the stop or target capture it". Drift answers the first on its own.

- **Tests:** `runner_research.drift_comparisons` tests lane-wide drift against 0, then every (dimension, bucket) against 0. Both use (session, industry) units.
- **Batch:** the tests sit in the **same** Benjamini–Hochberg batch, as dimensions prefixed `drift:`, in **ATR, not R**.
- **No rules on drift:** no rule can name a `drift:` dimension; `validate_predicate` rejects it. A positive drift bucket is a map of where to look. Anything the desk acts on still validates forward in R, on the runner dimension itself, under the plan or an arm.
- **Miner section:** "### 5. Continuation after entry".
- **Playbook:** the Runner Lane carries a one-line summary.

## 4. The construction arms (exit family v4, `agent/runner_exits.py`)

Each arm is replayed for every plan-valid triggered candidate, on the same minute bars, fill, slippage and thin-bar rule as the grader. The `plan` arm still has to reproduce the grader's `r_planned` (the parity check is unchanged).

| arm | stop | target | management |
|---|---|---|---|
| `arm_a_floor` | the **lower** of the 9-EMA stop and entry − 1.0 ATR14 | 2R **on the new risk** | none; the 15:55 desk flatten |
| `arm_b_dashboard` | the 9-EMA stop | none | the DTO dashboard's Runner rules card, as written: at +1R sell half and move the stop to breakeven; ratchet the stop up to the 5m 9 EMA on each 5m close (never down), and a 5m close below the EMA exits at the next open; out at 2:00 PM CT (15:00 ET) |
| `arm_c_floor_dashboard` | arm A's stop | none | arm B's management, with the 9-EMA trail **armed at the 1R partial** |

**Arm B's source.** The rules come from `Desktop/Trading/index.html`, the trade-rules card: "At 1R profit: sell half, move stop to breakeven · Continue trailing stop to 9 EMA on each bar close · Hold to 2:00 PM CT · Price closes below 9 EMA: exit on next bar open."

**Sizing is by risk, exactly as today.**

- A wider stop buys fewer shares for the same dollars at risk.
- Each arm books R in **its own risk unit**, so mean R and the paired ΔR against the plan compare dollars at risk.
- Every row stores `stop_used`, `target_used` and `cost_r`, the friction charged in that arm's R. Same cents on a wider stop means fewer R.

**Honesty rules:**

- **Intra-bar order** is the grader's conservative one: a pending exit at the open, then stop, flatten, time and target.
- **Delayed moves.** Breakeven and a ratcheted stop move from the **next** bar.
- **Trail arming.** The 1R touch that arms arm C's trail happened before that bar's close, so that close may trail.
- **Missing ATR.** A floor arm without ATR14 has no row.
- **Shortened sessions.** A `hold_until` past a half-day close ends at the desk flatten; the row is kept.

**Why arm C arms the trail at 1R.** Taken literally, combining A's stop with B's trail-from-entry undoes A. The first 5m close after the fill would ratchet the stop straight back up to the 9 EMA, so the floor would protect only about five minutes. Arming the trail at the partial keeps the question arm C exists to ask: a wide initial stop, then the dashboard's management.

**What "win rate" means here.** Arm B's 1R partial raises win rate by construction. The tested quantity is the per-session paired ΔR (the session is the unit; n < 8 sessions is never significant), in the same FDR batch as everything else. Win rate is printed beside it and never argued from.

**Trailing stops on a stalled name.** On a name that stops rising, the 9 EMA converges up to price and the ratcheted stop takes the position out. The dashboard trail does not hold a dead name to 2 PM. A test pins this, because it is the rule's real behaviour, not a bug.

### 4b. Beating the plan is not an edge: construction expectancy

This was added after the 10/02 replay (§9) and is not in the brief.

**Why it was added.**

- Sized by risk, a 1-ATR stop holds about a fifth of the plan's shares.
- On a field that drifts down after entry, any wider-stop arm loses less, simply because it is less exposed.
- On 10/02 every arm beat the plan by +0.3 to +0.5R per trade, and every arm still lost money.

**What changed.** `runner_exits.arm_expectancy_comparisons` adds 4 looks to the same batch: dimension `runner_arm_expectancy`, which tests the plan and each arm's per-session mean R against 0. Consequences:

- An arm becomes **live-eligible** only when it is forward-VALIDATED on ΔR **and** its own expectancy is significantly positive.
- A validated arm that only loses less is listed as "better than the plan but NOT making money on its own, so not a live candidate".
- The stop condition (§7) counts a construction only if its own expectancy is positive.

## 5. Forward validation for arms and exit policies (RUNNER_EXIT_SPEC Phase 3, built)

**Registration.**

- The weekly run calls `research.register_exit_policy_candidates`. Every exit policy or arm whose paired ΔR is **significant and positive** in that week's batch becomes a QUARANTINED rule in lane `runner_exit`, with predicate `runner_exit_policy=<name>`, `source = system`.
- A policy significantly *worse* than the plan is not a candidate for anything and is not registered.
- If the miner names the same predicate, it resolves to the same rule id.

**Forward evidence.**

- `research.observe_session` records **one observation per later session**: that session's mean paired ΔR, source `runner_exit` (counted), key `runner_exit:<date>:<policy>`.
- The forward-only guard is unchanged.

**Graduation** uses the existing gate:

- forward n ≥ 8 sessions;
- ≥ 10 days in quarantine;
- cohort Benjamini–Hochberg;
- the same sign as at discovery.

Decay and reversal apply as for every rule (reversal now reads every counted source).

**Live orders do not change.** A VALIDATED arm is listed in the playbook as awaiting Jason's approval for live management (RUNNER_EXIT_SPEC Phase 4: `live_monitor` managing the exit legs). Phase 4 is not built. Live runner orders keep the plan bracket.

## 6. Selection coverage

`runner_research.selection_coverage` gives one row per session, whatever its version, with the reason it is or is not contested:

- contested;
- not graded yet;
- excluded by the version pin (versions named);
- the selector failed (reason quoted);
- no selection;
- no candidate filled validly;
- no selected name filled validly;
- every priced candidate was selected.

It appears in the miner's runner section and in `run.py runner-evidence`.

## 7. The lane-level stop condition (pre-registered)

`runner_research.LANE_VERDICT_SESSIONS = 20`. Once 20 sessions under the current versions are measured, the Runner Lane section reports **MET** if, in the weekly batch:

- no drift bucket is significantly positive; and
- no construction (the plan or an arm) has significantly positive expectancy of its own.

It then states:

> As built, the detection carries no edge: the lane needs a redesign or a pause, not tuning.

Two refinements of the brief's wording:

- **An arm that only beats the plan does not count** (§4b).
- **Before 20 sessions the line reads "too early", never "NOT met".** The drift cells count (session, industry) units, and every unit of one session shares its tape. On 10/02, 95 units from one fade day already gave the lane-wide drift q = 0.001, so one strong up-day could do the same in reverse. Positives before 20 sessions are listed as early signals only.

The condition was fixed in code before any session under the current versions was mined. The decision stays with Jason; the system never pauses the lane.

## 8. Where to see it

- `python run.py runner-evidence`: continuation by bucket (drift, % up, median run-up/draw-down), the selection coverage table, and how many candidates traded more than 1 ATR below entry.
- `python run.py runner-exits`: the family v4 report, with arms, cost R, and ΔR per session.
- `run.py runner-status`, the dashboard API (`runner.construction`) and the web Runner page:
  - the session's plan vs arms A/B/C;
  - continuation by index tape;
  - candidate columns Stale (min), Ext (ATR), Tape and Drift (ATR).

## 9. Local evidence: 10/02 replayed under this code (not production; one session; not a finding)

**How it was run.**

- The whole 10/02 runner session was replayed into a scratch DuckDB with the production DSN removed: real Alpaca bars, every 5-minute scan tick from 10:20 to 15:00, the 10:30 decision tick, and this evening code.
- It produced **367 candidates and 355 triggered**, and the plan won **17%**. That is the same pool size and win rate as production's panel (61 of 352).
- Parity failures: 0. Entry context: 367 of 367. Arm A and C rows: 354 (one candidate had no ATR).

**Constructions:**

| construction | mean R | win % | median cost R | ΔR vs plan |
|---|---|---|---|---|
| plan | −0.700 | 17 | 0.081 | — |
| arm_a_floor | −0.241 | 24 | 0.014 | +0.458 |
| arm_b_dashboard | −0.386 | 21 | 0.081 | +0.313 |
| arm_c_floor_dashboard | −0.164 | 28 | 0.014 | +0.535 |

**Continuation:**

- 32% of candidates closed above their first enterable price.
- Mean drift was −0.14 ATR.
- The median draw-down after entry was 0.39 ATR, against a median stop of about 0.2 ATR: the stops sit inside the noise, as the review said.
- Only 14 of 366 candidates traded more than 1 ATR below entry before the close.
- No bucket had positive drift with any size; the mass of detections (10:00–10:30, RVOL < 0.5) faded hardest.
- **RVOL check:** 262 of 366 candidates (72%) sat in `<0.5`. The brief's `<1` cell would have held 340 of 366 (93%), which is degenerate (T8).

**Reading.** On one fade day the wider stop cuts the loss by a factor of about three, mostly by being smaller, and loses money all the same. That is exactly what §4b exists to keep from being read as an edge.

**What it is not.** One session is not evidence of anything. It shows only that the machinery runs on real bars and measures what it says.

---

## Trade-offs (T-style), stated rather than resolved silently

- **T1, search width (Goal 5 vs every family's power).** The batch grows by:
  - 3 runner dimensions (~16 R cells, plus their win-frequency looks);
  - the drift family (52 cells on the 10/02 replay; more as rarer buckets fill);
  - 3 exit looks;
  - 4 expectancy looks;
  - 1 pair (up to 24 cells, only once admissible).

  The 10/02 replay's batch held 209 looks. Every q-value in the batch rises. This is accepted, because correcting these apart would leave the width uncontrolled, and that is the failure the batch exists to stop.
- **T2, speed (Goal 1).**
  - **Arms:** the session is the unit. An arm needs 8 sessions to be testable, then 8 forward sessions and 10 days to validate, so nothing goes live before about mid-November.
  - **Drift cells:** these count (session, industry) units and are readable sooner. They are a map, not a rule.
- **T3, evening budget (Goal 2).** The evening adds two minute-bar fetches (SPY, QQQ), three more replays per triggered candidate (CPU only) and an O(bars) entry-context pass. Small against the bar fetch; watch `runs.stages`.
- **T4, drift ends at the session close (15:59), not the 15:55 flatten.** This matches Move (ATR). It describes the stock, not the desk's exit.
- **T5, arm C's trail is armed at 1R** rather than from entry (§4). This is stated, not hidden.
- **T6, arms are priced on plan-valid fills only,** so the comparison stays paired. A candidate whose plan bracket was stale has no arm rows, even though arm A's wider bracket could have been valid. Drift still covers it.
- **T7, at-entry fields are computed in the evening** from point-in-time bars, so they are honest but not visible to the live selector. A filter that validates on them needs a live computation before it can be enforced.
- **T8, RVOL edges kept.** The brief proposed `<1, 1–1.5, 1.5–2, 2–3, 3+`.
  - With 357 of 367 candidates under 1.5x (median 0.33x), a `<1` cell would hold ≥ 90% of the dimension. `stats._mark_degenerate` would mark it degenerate and not test it, and the dimension would compare nothing.
  - The live edges (17a55df) split that mass at 0.5x.
  - The upper labels (3–5, 5+) are a finer cut of the brief's 3+.
- **T9, Q2/Q4, shadow and paired arms are unchanged.** Their R measure is as RUNNER_POOL_INTEGRITY T3/T4 left it.

## Deploy and first-session checks

**Deploy.**

1. Migration v28 is additive (13 nullable columns), so warm up with `scripts\intraday_cron.ps1 -Once` right after deploy.
2. Then Jason runs `python run.py regrade-runner --from 2026-10-02` (fewer than 500 rows, so no `--force` is needed). It fills v28 and the v4 replays for 10/02 and rewrites the same sim-v7 R.

**First graded session after deploy.**

1. `Runner candidates priced: … entry context N` in the evening log, with N ≈ the scored count and no parity failures.
2. `runner_exit_replays` has v4 rows and all three `arm_*` policies. Missing arm A and arm C rows equal missing-ATR candidates.
3. `run.py runner-evidence` prints continuation by bucket, and its "% up" for all candidates matches the panel's "closed above entry".
4. The next weekly prompt carries "### 4. Exit policies" with arm rows, "### 5. Continuation after entry", the coverage table, and the stop-condition line.
