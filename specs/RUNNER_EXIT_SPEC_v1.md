# RUNNER EXIT SPEC v1 — measure every exit before changing one

**Status:** Phases 1–2 implemented and deployed 2026-09-26 (997 tests pass). Phase 0 steps 1–2 blocked for Claude (production reads are refused); steps 3–4 are Jason's to run. Phases 3–5 are proposed, not built.
**Goals served:** 1 (primary), 3, 5, 4
**Scope:** new `agent/runner_exits.py`, `agent/migrations_v24.py`; `agent/ledger.py`, `agent/loops/evening.py`, `agent/integrity.py`, `api/index.py`, `run.py`; `tests/test_runner_exits.py`.
**Depends on:** nothing new. Phase 3 reuses S2 (FORWARD_VALIDATION_SPEC_v1) as it stands.
**Writer isolation:** unchanged. Replays are a ledger table. The weekly miner sees them only as scored comparisons, and only the miner writes `playbook.md`.

---

## 0. The problem

Jason, 2026-09-25, on REAX (runner candidate, detected 10:25 ET at 16.66, stop 16.19, target 17.59): the stock rose +1.41R to 17.18 around noon, then flushed through the stop at about 12:15 and closed back at 16.43. The plan booked −1.02R. His read: "many of the runners would have performed far better had they exited at the right time … we would have a much higher win rate with proper exits."

The give-back is real. But the claim has two parts, and the data treats them differently.

- **"Higher win rate" is true of any tighter exit, and says nothing about edge.** The local corpus (sim v3, 3 graded sessions, not production) replayed with fixed targets from stored MFE:

  | target | win% | mean R |
  |---|---|---|
  | 0.5R | 72 | +0.07 |
  | 1.0R | 59 | +0.15 |
  | 2.0R (plan) | 48 | +0.24 |

  Win rate rises as mean R falls. The score is mean R per trade, never win rate.
- **"Exit at the right time" in hindsight is the MFE bar, which no rule can see.** Choosing an exit rule by sweeping stops, targets, trails and clock times across the same trades it is scored on is the easiest way this system could manufacture edge.
- **Path-dependent exits were unmeasurable.** Only summary statistics were stored (MFE, MAE, EOD). Breakeven stops, trails and time stops depend on the order of prices, which needs the bars.
- **The runner lane had no honest realized R.** `ff6c3d4` added `runner_candidate_outcomes.r_planned` and `eod_r_planned` (v23), but `evening._grade_runner_candidates` never passed them, so every runner row kept them NULL. `runner_research` then fell back to the floored, inflated `r_multiple` for setup edge, selection edge and every runner dimension. **Fixed in this change.**

---

## Phase 0 — production verification (hard blocker; a self-check, not a sign-off)

Data access means an authenticated `/api/table/...` session, or a local `DATABASE_URL` that Jason sets himself. If neither is available, record Phase 0 as blocked and stop. Never search the machine for credentials.

1. Pull `/api/table/runner_candidate_outcomes?since=2026-09-01&limit=5000`. Confirm the bug: `r_planned` is NULL on triggered rows. Report sessions, candidates, and triggered counts per `sim_version`.
2. Pull `/api/table/runner_candidates?since=2026-09-01&limit=5000`. Report `scan_version` counts; the search reads current-version rows only.
3. Deploy, then Jason runs `python run.py regrade-runner --from <first runner session> --force` with `DATABASE_URL` set. This one opt-in command re-prices every candidate under sim v4, fills `r_planned`, and writes the exit replays. **Acceptance:** each session's log line reads `exit replays N` with no `parity failures`. A parity failure means the replay engine drifted from `simulate_pick`; stop and fix before reading any ΔR.
4. Jason runs `python run.py runner-exits` and pastes the output. That output, not the local numbers below, is the production baseline.

**Order:** step 3 is independent of the shadow-ledger repair. `regrade-runner` writes only `runner_candidate_outcomes` and `runner_exit_replays`, never the rejections, lessons or progress rows that repair is cleaning. If it runs before a weekly cron, that weekly reads honest runner R and sees the exit family (10 more looks, which makes the batch stricter, not looser).

---

## Phase 1 — replay every triggered candidate under a fixed exit family (implemented)

`agent/runner_exits.py`. `replay()` walks one triggered long candidate through each `ExitPolicy` on the same minute bars, fill, slippage and thin-bar rule the grader used. The rules below are pre-registered: fixed in code before any data was seen, versioned as `POLICY_VERSION = 1`, and never tuned on the rows they are scored on.

| policy | question | rule |
|---|---|---|
| `plan` | reference | static stop, 2R target, else close |
| `no_target` | does the 2R target cut winners short? | static stop, hold to close |
| `breakeven_1r` | is giving back open profit the leak? | plan; after +1R, the stop moves to the fill from the next bar |
| `ema9_trail` | exit when the setup breaks | static stop; exit at the next bar's open after a 5m close below the 5m 9 EMA |
| `vwap_trail` | exit when price loses VWAP | the same, with VWAP |
| `scale_1r_ema9` | bank half, trail the rest | half off at +1R, remainder on `ema9_trail` |
| `time_1100` … `time_1500` | when should a runner be flat? | plan, exit at the close of the first bar at/after HH:00 |

**Where the numbers come from:** 1R and 2R are the plan's own units. The 9 EMA and VWAP are the setup's own definition (`runner_scan.check_runner_conditions`). The only new numbers are the hourly clock times, which are a grid, not a finding.

**Honesty rules:**
- **Conservative intra-bar order,** the same as the grader's: stop first, then flatten, time stop, target; then anything that arms for a later bar.
- **No self-protection:** breakeven never protects the bar that armed it.
- **Trails fire late, never early:** a trail exits only after its 5-minute bar has closed, at the next bar's open.
- **Only followable time stops:** a time stop at or before the fill, or at or after the session close, yields no row, because the trade could not have followed it.

**Storage.**
- **Table:** `runner_exit_replays`, schema v24, one row per (session, ticker, policy): `r_planned`, `exit_reason`, `exit_et`, `mfe_r`, `mfe_et`, `policy_version`, `sim_version`.
- **Clock times:** `exit_et` and `mfe_et` are naive ET wall-clock. A tz-aware write shifts into the writer's zone (Central locally, UTC on Neon); the first local run caught this.
- **Write pattern:** replaced per session with one DELETE plus chunked multi-row INSERTs. Row-at-a-time writes would cost about 3,000 Neon round trips a night.

**Self-check.** The `plan` arm must reproduce the grader's `r_planned` for the same trade (tolerance 1e-3). If it does not, that candidate's replays are not stored, and the evening logs and counts a parity failure.

**Readable by:**
- `/api/table/runner_exit_replays` (whitelisted);
- `python run.py runner-exits [--from D]`, which prints mean R and win rate side by side, plus give-back and peak-hour counts.

## Phase 2 — score the family inside the one FDR batch (implemented)

`runner_exits.exit_policy_comparisons` emits one `Comparison` per non-reference policy, with dimension `runner_exit_policy`. `integrity.build_search` adds them to the same Benjamini-Hochberg batch as every other family.

- **The unit is the session.** The value is each session's mean paired ΔR (policy − plan, same candidates); n is the number of sessions. Several hundred candidates on one tape are one observation of "did this exit beat the plan today". The n<8 rule therefore means 8 sessions, not 8 trades.
- **No replays, no family.** Once replays exist, all 10 policies are counted looks every week.
- **The miner reads a dedicated section:** "### 4. Exit policies" (`exit_md`). It shows ΔR and q, and states that a significant ΔR is a candidate, not a rule, and that win rate is not an argument.

## Phase 3 — forward-validate a surviving policy (proposed)

A policy with q < 0.10 at n ≥ 8 sessions becomes a QUARANTINED rule with the machine-readable predicate `runner_exit_policy=<name>` (FORWARD_VALIDATION_SPEC_v1 Phase 2).

1. **Record forward observations.** The evening loop records one observation per session dated after `quarantined_at`: that session's mean paired ΔR, with source key `runner_exit:<date>:<policy>`. The forward-only guard is untouched.
2. **Graduate through the existing gate.** Cohort Benjamini-Hochberg, same sign as at discovery, and decay after 30 silent sessions (S2 Phase 4). No new graduation logic.
3. **Keep writer isolation.** A graduated exit policy may appear in the playbook's runner section, written by the weekly miner only.

## Phase 4 — live execution (proposed; needs Phase 3 graduation and Jason's approval)

Live runner orders are static Alpaca brackets (`live_broker.submit_bracket_order`), so a trailing, breakeven or time exit needs `live_monitor` to manage the exit leg:

- replace the stop leg on arm;
- cancel both legs and market-sell on a trail or time trigger.

Paper first. Add a per-policy kill switch in config. Live exits must be graded against the replay of the same policy, so drift between the simulated rule and its execution is itself measured.

## Phase 5 — the exit may depend on the day (proposed; pre-register before looking)

The local replay's strongest pattern is not any single policy but the day. Per-session plan means (local ledger, sim v4, not production):

| session | n | plan | no_target | breakeven_1r | ema9_trail | vwap_trail |
|---|---|---|---|---|---|---|
| 09-11 | 65 | −0.26 | −0.19 | −0.24 | −0.16 | −0.10 |
| 09-15 | 20 | +0.27 | +0.33 | +0.02 | 0.00 | +0.15 |
| 09-17 | 300 | −0.20 | −0.40 | −0.16 | −0.14 | −0.35 |
| 09-18 | 248 | +0.26 | +0.44 | +0.17 | −0.09 | +0.36 |
| 09-21 | 313 | +0.13 | +0.21 | +0.20 | +0.13 | +0.19 |
| 09-22 | 324 | +0.14 | +0.27 | +0.14 | +0.01 | +0.24 |
| 09-23 | 266 | −0.45 | −0.48 | −0.33 | −0.23 | −0.40 |

On up-tape days, holding longer wins. On fade days, tight exits lose less. Averaged over both kinds of day, every policy nets out near zero. If this holds in production, the lever is a **day-type signal known at entry time**, not a better universal exit.

That is a 2-D cell (exit policy × day type), and it must be pre-registered, with its day-type definition fixed in code, **before** any production data is examined for it. It then enters the batch under the 2-D cap like S7's pairs. Candidate day-type signals, all knowable by the detection tick:
- SPY above or below its session VWAP at 10:30;
- the session's runner-candidate count versus its trailing median;
- the opening-range break direction of SPY.

Choose one before looking. Choosing after looking is the artifact this spec exists to prevent.

---

## Local evidence (not production; 7 sessions, 1,536 triggered candidates, sim v4, 0 parity failures)

Replayed on a copy of `data/ledger.duckdb` with real Alpaca bars:

| policy | mean R | win% | ΔR/session vs plan (uncorrected) |
|---|---|---|---|
| plan | −0.025 | 38 | — |
| no_target | +0.006 | 33 | +0.041 |
| breakeven_1r | −0.000 | 43 | −0.013 |
| ema9_trail | −0.061 | 36 | −0.054 |
| vwap_trail | +0.008 | 31 | +0.029 |
| scale_1r_ema9 | −0.058 | 41 | −0.057 |
| time_1100 | −0.078 | 43 | −0.083 |
| time_1200 | −0.032 | 42 | +0.007 |
| time_1300 | −0.023 | 41 | +0.031 |
| time_1400 | −0.010 | 41 | +0.028 |
| time_1500 | −0.012 | 40 | +0.025 |

- **Give-back:** 154 of 845 plan stop-outs (18%) were up at least 1R first. Under the old sim-v3 walked-back entry it looked like 32%; part of the apparent give-back was look-ahead.
- **Peaks** (no_target, by ET hour): 10:00 297 · 11:00 295 · 12:00 237 · 13:00 174 · 14:00 154 · 15:00 369. The move usually peaks in the first 90 minutes after detection or into the close; midday is the trough.
- **Honest-entry baseline:** under sim v4, the plan exit's mean R is about zero (−0.025). The +0.19 the v3 rows showed was mostly the look-ahead entry. No exit rule on this list turns this setup's average into a clear edge; Phase 5 is where the leverage looks to be.

---

## Constraints

- The family is fixed per `POLICY_VERSION`. Adding or changing a policy bumps the version, and rows from different versions are never pooled.
- Win rate is never the objective or an argument. The tested quantity is paired ΔR in planned-R units.
- Sessions are the unit of evidence. The n<8 rule applies to sessions.
- Replays never change a live exit. Only Phases 3–4, gated as above, can.
- `r_multiple` stays diagnostic. Every runner statistic reads `r_planned`.

## Trade-offs (flagged, not silently resolved)

- **T1, Goal 5 vs every other family:** 10 new looks widen the weekly BH batch, which costs every other family a little power. Accepted: the alternative is an exit question answered outside the batch, which is worse.
- **T2, Goal 2 vs the evening budget:** replays are pure CPU on bars the evening already fetched, plus one DELETE and about nine INSERTs per session. Locally, replay time is small against bar fetch. Watch the evening's `stages` timing after deploy.
- **T3, Goal 1 speed vs integrity:** at about one runner session a day, 8 sessions are reachable in two weeks, but a policy with a ΔR near +0.03 will not reach significance for months. That is the honest answer. Phase 5 is where a large effect could appear sooner.

## Side findings

- **Fixed here:** runner `r_planned` / `eod_r_planned` were never written (see §0).
- **Not fixed (pre-existing, needs a sim-version bump):** Alpaca minute bars are fetched with an inclusive `end=16:00`, so some sessions include the 16:00 after-hours minute, and `simulate_pick`'s EOD exit can land on that print. Seen locally as 10 peaks stamped 16:00. A fix should drop bars at or after the session close in `simulate_pick` and bump `CURRENT_SIM_VERSION`.

## Acceptance (production evidence)

- Every session in the `regrade-runner` log shows exit replays with zero parity failures.
- Every triggered runner row graded after deploy has non-null `r_planned`.
- `runner_exit_replays` holds 11 rows per triggered candidate, fewer only where a time stop was not followable.
- The next weekly prompt contains "### 4. Exit policies" with batch q-values, and `research_looks` counts 10 more looks.
