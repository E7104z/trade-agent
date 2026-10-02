# Runner pool integrity (v1) — 2026-10-02

**Status:** implemented; full suite passing. Deployed with scan_version 3, sim_version 6 and migration v26.

**Trigger:** Jason's review of the 2026-10-01 production runner panel (534 candidates, 10 picks). It found four integrity problems. Each was checked against the code and the session's own numbers before anything was changed. Tracing them turned up six more defects, marked *found in audit*.

Standing-directive goals moved:

- **Goal 1 (validated edge):** the miner and forward validation now count independent events, not correlated rows.
- **Goal 2 (data every day):** the pool is operating companies only, and every plan is formed at the moment of action.
- **Goal 5 (structural mining):** 2-D pairs, win frequency and consistency all count units.

---

## Phase 0: the claims, checked

Everything below was recomputed from the pasted panel (`price/stop/target/R/exit` for all 534 rows). Then yfinance profiles were fetched for all 534 tickers, and Alpaca's registered name for all 12,952 active assets.

| Claim | Check |
|---|---|
| Planned target 1.82–2.18R, median 2.0 | 1.82 / 2.00 / 2.18 (2-decimal display rounding of an exact 2R) |
| 194 target exits; 22 > 2.5R; 62 < 1.0R; 10 ≤ 0.11R | 194 / 22 / 62 / 10 |
| 62 target exits filled > 0.5 planned R from detection, 59 above it | 62 / 59 |
| SEPN 7.46R, FUL 5.37R and BEPC 5.10R imply fills 7–11¢ above the stop | implied fills 0.66 / 0.54 / 0.51 planned R *below* the plan; r_planned 1.89 / 1.90 / 1.97 |
| PKE −0.03, PGY −0.02 and FLR −0.01 are target exits at a loss | implied fills 1.92 / 1.97 / 1.94 planned R above the plan, within ~1¢ of target |
| Selected −0.13R vs field +0.11R | −0.131 / +0.109; both fill-based (`r_multiple`) |
| 12 verified wrappers, 5 suspected | All 17 are funds by registered name. **70 of 534 rows (13%) were not an operating company's stock**: 2x single-stock ETFs, thematic, commodity and bond funds, a closed-end fund (BSTZ), ProShares products, and Redwood Trust's senior notes (RWTQ). |
| Six of ten picks are one shipping trade | Yahoo puts BWLP, FLNG, CMBT, TEN and TRMD in one industry (Oil & Gas Midstream) and CMRE in Marine Shipping |
| *(found in audit)* Bank cluster | 35 of the pool's 40 regional banks fired between 11:55 and 12:20 |
| *(found in audit)* Row inflation | 444 triggered operating-company rows were 110 (session, industry) units |

Not checked: `entry_fill`, `first_pass_et` and `scan_pass_et` for PKE/PGY/FLR/SEPN/BALY. Claude has no production read path. The implied fills above are derived from the panel; the mechanism is confirmed in code and in the local ledger (below).

---

## What was wrong, and what changed

### 1. The "movers" panel ranked a detection-time feature

`top_movers()` sorted by `change_pct` = (detection price − previous close) ÷ previous close. That is the move made *before* the system could act, and the selector reads it as a feature, so the panel listed the most-extended names and labelled the skipped ones as misses.

**Fix:** `runner_view.biggest_moves()` ranks by `post_move_atr` = (session close − plan price) ÷ ATR14. The evening simulator now stores this as `runner_candidate_outcomes.post_move_atr` / `session_close`. `change_pct` stays as a context column. The text, HTML, API (`runner.moves`, `runner.outcomes`) and web UI all moved.

The universe-wide question ("why did names move today": close vs previous close across everything scanned) is **not built**. It needs a ~4,000-symbol daily fetch in an evening run that is already near its budget.

### 2. Funds, notes and thin names in the pool

- `EXCLUDED_TICKERS` is the frozen 117-symbol DTO parity list, and `LANE_EXCLUDED_TICKERS` covers only BTC/ETH spot products.
- The standing list and both screeners were unioned in with no liquidity floor. LITG (≈$0.4M/day, listed August) came in through movers.
- The float gate fails open (`null_gates: dto`), and a fund has no float.

**Fix (lane-only; the parity set is untouched):**

- `agent/instruments.py`
  - `classify(name)` sorts Alpaca's registered name into common / fund / debt / preferred / unit. It was checked against all 12,952 assets; every rule is pinned to a real name in the tests. "Trust" alone is *not* a fund marker, so REITs, banks and royalty trusts stay in.
  - `fund_from_profile()` is the independent second read on scan survivors. It uses yfinance `quoteType` (ETF, MUTUALFUND, …) and flags a closed-end fund as "Asset Management" with no float (catches DXYZ, which the name misses).
- `providers.Alpaca.asset_records()` returns names. `runner_universe.instrument_classes()` caches the class map per session (`runner_universe_cache` kind `classes`, all tradable assets, including 1,402 non-fractionable commons).
- `build_universe` applies the class map and the $5M/day floor to **every** source, and records drops by class and source. Symbols whose prior session is unknown at build time are floored in the scan (`rejected_liquidity`).
- `runner_sessions.universe_stats` / `rejected_liquidity` / `rejected_instrument` make every drop visible on the panel.

### 3. The stale bracket (root cause, not only the symptom)

- The scan walked `first_pass_et` back up to 12 bars, then took the **whole plan** (detection price, 9-EMA stop, 2R target, every feature) at that walked-back bar.
- Since sim v4 the fill happens at the scan tick (`honest_entry_time`). A name that kept running was therefore bought at the tick on a stop and target up to an hour old.
- *(found in audit)* The scan also read Alpaca's bar **still forming at the tick**. The local ledger shows a 10:20:02 tick stamping candidates at the 10:20 bar with walkback 0, i.e. a two-second-old bar.

**Fix, scan_version 3 (`runner_scan.scan`):**

- The gate chain reads completed bars only (`completed_bars`).
- The plan is taken at the last completed bar at the tick (`plan_bar_et`), the bar the gate chain just passed. DTO itself took its price from the current bar.
- `first_pass_et` is still walked back, as a research timestamp (`detection_minutes`), never a plan.

**Backstop, sim_version 6 (runner lane only):**

- A runner fill must leave at least `RUNNER_MIN_REWARD_R` = 0.5 planned R below the target, else `invalid_fill` with `invalid_reason = stale_bracket`. The 0.5 is the `execution_anomaly` line read from the other side.
- The same rule runs live before every runner order (`runner_selection.stale_bracket_reason`, using the latest trade price). The paper account and the ledger therefore hold the same trades.
- Morning and shadow semantics are unchanged from v5. `grading.same_semantics` keeps the chart drawer from flagging every old morning chart.

**Display and decisions read planned-risk R** (`r_planned`, falling back to `r_multiple` only on legacy rows). `r_multiple` divides by the fill-to-stop distance, so it inflated favourable fills (SEPN +7.46R) and shrank adverse ones (a 1.5R loss booked −1.0R). It is no longer shown anywhere a decision or headline reads it:

- runner panel, API and web UI
- evening grader prompt (both the pick text and the runner table)
- chart drawer result and replay guard
- equity curve, overall avg/total R, streak and the drawdown breaker
- the morning analyst's recent-trades table and the weekly miner's CSV
- live-vs-sim divergence

### 4. Concentration and pseudo-replication

**Selection:**

- The selector prompt (v2) told the model "several names from one sector is one bet" and then gave it no sector. Its table now has `industry` (Yahoo's, from the same info call as float).
- A per-industry cap (`runner.max_picks_per_industry`, default 2) is stated in the prompt and **enforced in code** on both the LLM and auto-select paths. Dropped names are not selected and are priced with the field. The measured policy is "selector + cap".
- Prompt v2 also removes v1's false guarantees: "float inside the band" (a null float passes) and "RVOL above the minimum" (there is none). It states the new ones: operating companies only, and the $5M floor.

**Counting:** `agent/clusters.py` defines the runner lane's unit of evidence as one (session, industry), valued at its rows' mean R. A row with no known industry is its own unit. Every runner statistic now counts units:

- the miner's 1-D cells, setup edge and selection edge
- 2-D pairs, win frequency and consistency strata
- 2-D discovery stats
- the evidence backlog
- forward validation: one observation per unit (`runner:{session}:{cluster}`). Before this, a runner rule could collect its whole forward n in one morning.

The morning lane is unchanged (a handful of hand-picked rows a day). The random-selection control stays a within-session permutation, which is valid under correlation.

### Found in audit

| Defect | Fix |
|---|---|
| `grade_day` applied the runner entry time to **every** pick sharing the ticker. A morning gap-and-go on a name the runner also detected was graded from 10:30, not the open. | Runner picks only. The chart drawer's replay had the same bug, plus a fallback that re-simulated a runner pick from 09:30 when no bar followed its entry. Both fixed. |
| The friction stress ladder re-simulated runner picks from the 09:30 open. | Sliced to the honest entry, under the runner rules. |
| Every yfinance lookup 404'd for share-class tickers (BF.B is BF-B on Yahoo): float, earnings, industry and history were all empty. | `providers.yf_symbol` on every yfinance call. Batched downloads are keyed back to our spelling. |
| Fundamentals cache rows predate quote_type/industry. | NULL quote_type = refetch once; Yahoo's "none" is cached as "". |

---

## Trade-offs (T-style), stated rather than resolved silently

- **T1, the corpus resets.**
  - scan_version 3 changes what a row *says* (the plan bar), so v2 rows cannot be relabelled from stored inputs. `runner_regate.UNREACHABLE` says so, and the version-pin test requires it.
  - The weekly search mines v3 × sim v6 rows only, so the 10/3 weekly has one runner session.
  - This costs data volume (Goal 3) in exchange for rows that describe trades the system could take (Goals 1 and 2). The v2 rows were contaminated by funds and stale brackets in exactly the cells being mined.
- **T2, fewer candidates per session.** The instrument filter removed 13% of the 10/01 pool.
- **T3, Q2/Q4 stay on `r_multiple`.** Their progress notes are parsed back by `question_eval`, and every earlier note carries `r_multiple`. Switching only new notes would mix two measures in one test series. Moving them to planned R needs a recompute from outcomes.
- **T4, the shadow ledger and paired arms stay on `r_multiple`.** Both sides of those comparisons use it (rejections and `paired_arm_picks` store no planned R), so the comparisons are internally consistent. Moving them needs new columns plus a regrade.

## How to check it worked (first session after deploy)

1. `runner_sessions.scan_version = 3`. `universe_stats` shows instrument and liquidity drops by source, and `instruments_classified` is true.
2. No candidate whose registered name is an ETF/ETN/fund/note. `industry` is filled for most rows.
3. `plan_bar_et` equals the last completed 5-minute bar before `scan_pass_et`, and is ≥ `first_pass_et`.
4. Target exits cluster just under 2.0R in `r_planned` (2R less slippage and any latency since the plan bar). No target exit at a loss. `invalid_reason = stale_bracket` rows exist but are few.
5. The selected set holds ≤ 2 names per industry, and the session note names any cap drops.
6. The panel shows "Biggest moves after detection" ranked by ATR move, with planned-risk R.
