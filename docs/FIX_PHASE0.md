# FIX SESSION — PHASE 0 REPORT (read-only)

**Session date:** 2026-10-16 (evening) · **Author:** Claude Code · **Repo:** `E7104z/trade-agent`, `master` @ `7f8053d`
**Status:** Gate 0 passed. D1 and D9 are both explained (below), so nothing here is a hard stop.

## What was and was not touched

| Surface | What I did |
|---|---|
| Repo | Nothing changed. `git status` before and after: only the pre-existing untracked `specs/PULLBACK_LANE_PHASE0_REPORT.md`. This file is the only addition. |
| Broker | **GET requests only**, through the project's own `live_broker` wrapper with a guard that raises on any other verb. Read: positions, orders (nested legs), account activity. No order placed, cancelled or closed. |
| Production DB | **Not read.** No read path exists for me (memory: `production-data-access`); `/api/*` needs Jason's session and the classifier refuses DSN use. Where a prod row is needed, the claim is marked *inferred* and the evidence used instead is stated. |
| Local DuckDB | `data/ledger.duckdb` opened `read_only=True`. It is a **local** ledger (7 plans, 8/19–9/30), not production. Labeled "local" wherever used. |
| Market data | Read-only minute/daily bars through `MarketData`. |
| Experiments | A scratch copy of the repo under the session scratchpad (no `credentials.json`, no `data/`), never the repo itself. |

**An order I did not place:** the broker holds a market `sell_to_close` for the full IOVA 250 shares, status `accepted`, submitted 2026-10-17T01:11:13Z (= 10-16 21:11 ET = 20:11 local CDT). My first command ran at 21:10 CDT, 59 minutes later, and my broker wrapper cannot send a non-GET. It is queued for the next open. Most likely Jason. It resolves "close IOVA if still held" from the prompt's end-of-session list; the position itself is still open tonight.

---

## Q1 — Baseline

| Item | Value | Source |
|---|---|---|
| Suite | **1232 passed, 0 failed, 0 skipped** in 164.5 s | `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`, `DATABASE_URL` unset. (This shell has the **production** `DATABASE_URL` set; `tests/conftest.py:22` pops it, and every command I ran unset it first.) |
| `CURRENT_SIM_VERSION` | **7** | `agent/grading.py:138` |
| `EOD_FLATTEN_MINUTES` | 5 | `agent/grading.py:143` |
| Schema | **v35** (`agent/migrations_v35.py:28`); next free number **36** | `agent/ledger.py:443-444` |
| Prompt versions | analyst **13**, risk **16**, grader **10**, trader's log 1, weekly 18, persona 1 | first line of each `prompts/*.md` |
| What production runs | `6c774d1` (`dpl_8G3fsXQe3o1Df1HY8aWEHLLMajyb`, READY, created 2026-10-11T01:54Z) | Vercel `list_deployments`. HEAD `7f8053d` adds only docs on top, so **prod code == this working tree**. |

---

## Q2 — D1: positions carried overnight  →  **CONFIRMED, with a larger cause than the prompt states**

### Do the EXECUTION_FIX names exist?

`grep -rn "eod_backstop|orphan_sweep|eod_backstop_closes|acquire_tick_lease|allow_overnight_symbols|FLATTEN MISSED"` over `*.py *.md *.ps1 *.json`: **one hit**, `docs/EXECUTION_FIX_PHASE0.md:173` ("`allow_overnight_symbols` … does not exist yet (Phase 1 adds it)"). **No code.** No migration creates `eod_backstop_closes` (latest is v35), and prod runs the same code, so prod has no such table. `claude/EXECUTION_FIX_SPEC_v1.md` is **not in the repo** (there is no `claude/` directory); only the Phase 0 verification report exists, and its sign-off boxes are blank. Phase 1 therefore implements the behavior from the prompt's own bullet list, not from a spec I can read.

### What actually happened to IOVA (broker evidence)

Position now: `IOVA long 250 @ avg 14.36276, market value 3,405, unrealized −185.69`.

Complete IOVA order history at the broker (`GET /v2/orders?symbols=IOVA&after=2025-01-01`), two orders ever:

```
buy  limit 14.40  bracket qty 250  submitted 2026-09-30T12:10:59Z (08:10:59 ET)  FILLED 13:38:08Z @ 14.36276
  leg sell limit 15.30 (TP)   status expired                      
  leg sell stop  13.90 (SL)   status canceled 2026-09-30T20:01:06Z (16:01:06 ET, day-order expiry)
sell market 250 sell_to_close  status accepted  submitted 2026-10-17T01:11:13Z   <- not this session
```

So IOVA was bought **2026-09-30**, its day-order legs expired at that close, and nothing ever flattened it: **16 sessions, not 2**. It is sized 0.25R (`250 × 0.50 risk/share ≈ $125`), the minimum-size lane signature.

### Which lane opened it — a *local* run, not production

The local DuckDB holds the 9/30 plan the broker orders came from:

```
picks 53 INKT  long limit 15.2  daily_floor     live_orders: pending_new, exit_reason NULL
picks 54 KMX   long limit 59.0  coverage        ...
picks 55 QURE  long limit 24.0  coverage        ...
picks 56 IOVA  long limit 14.4  stop 13.9 target 15.3  size 0.25R  catalyst_momentum  coverage   live_orders: pending_new, exit_reason NULL
picks 57 JBL   short limit 312  coverage        ...
picks 58 MBLY  short breakout 7.15  coverage    (no live order)
```

Those five `live_orders` rows were submitted 08:10:55–08:11:00 ET with `qty` matching the broker exactly (125/83/62/250/20). They sit in the **local** ledger, `pending_new` forever. A run with `DATABASE_URL` unset wrote real paper orders to the shared account and recorded them in a ledger the production monitor never reads. Corroboration from the local tick log (`~\.trade-agent\intraday.log`): on 9/30 the 09:35 and 09:40 ticks both reported `"updated":0` although INKT, KMX and JBL had been stopped out by their own legs at 09:33–09:34. Production never saw them. This also reconciles with the earlier Phase 0 finding "IOVA in no prod table (0 picks / 0 live_orders)".

**Answers to the prompt's lane questions:** the opener was the **coverage lane** (local pick 56). No pullback lane exists: `PULLBACK_LANE_SPEC_v1.md` is a spec; `grep pullback` finds no lane code and `config.json` has no pullback section. "Pullback order table" does not exist. Phase 1's pullback-lane test therefore uses the only claim source that exists, a `live_orders` row, behind a claim interface a future lane can use.
"Is any ledger row still open for IOVA?" Prod: no row at all (inferred from the monitor and carry logs). Local DuckDB: yes, pick 56's row, never reconciled.

### Why nothing closed it (code)

1. **The monitor is ledger-blind to the broker.** `agent/live_monitor.py:190-194`:
   ```python
   for row in ledger.live_orders_for_date(con, d, open_only=True):
       pick = picks.get(row.get("pick_id"))
       if not pick:
           continue
   ```
   It iterates *today's open ledger rows*. A broker position with no such row is never looked at. The 10-15 and 10-16 15:55 ticks both logged `"updated":0,"carry":{"stale":0}` while IOVA sat open.
2. **`carry_cleanup` is the same blind spot.** `live_monitor.py:323-325` selects `live_orders WHERE exit_reason IS NULL AND session_date < today`. Zero rows for IOVA ⇒ `{"stale":0}` on every morning. Across the whole local log it found stale rows on exactly four days: 9/30 (BEC, SK), 10/02 (3), 10/05 (1), 10/12 (IONS). Never IOVA. It also skips any ticker that has an order today (`live_monitor.py:369-371`, "left for the EOD flatten").
3. **One flatten attempt per session.** `scripts/intraday_cron.ps1:49,130,143` — `EndEt = "15:59"`, `if ($t -gt $end)` exits, ticks are wall-clock multiples of 300 s: the last tick is **15:55:02**, then `[16:00:02 ET] past window`. In-tick retries are `_CLOSE_RETRY_WAITS = (0.5, 1.0, 1.5, 2.0)` (`live_monitor.py:108`), five seconds in total. The earlier Phase 0 already showed IONS lost that race on 10-09.
4. **No tick was logged on 10-14 at all** (the local task did not run that day). Nothing places, monitors or flattens when the local trigger is down; Vercel Hobby crons cannot substitute (daily only, minute offset up to ~59 min).

**D1 verdict: confirmed.** The EOD fix must be broker-truth-driven (positions, not ledger rows) and must not depend on the ledger to know what to close.

---

## Q3 — D2: simulator fills limit orders on a touch  →  **CONFIRMED in code; DIFFERENT CAUSE for the RKLB incident**

### The fill condition (`agent/grading.py`)

```python
649            if entry_type == "limit":
650                if long and b["l"] <= entry:                       # buy limit: touch
651                    fill_price = min(entry, b["o"]) if b["o"] < entry else entry
...
654                if not long and b["h"] >= entry:                   # sell limit: touch
655                    fill_price = max(entry, b["o"]) if b["o"] > entry else entry
658            else:  # breakout (stop-order semantics)
659                if long and b["h"] >= entry: ...                   # touch (correct for a stop)
663                if not long and b["l"] <= entry: ...
```
No touch-only tagging exists anywhere.

### Existing tests that assert a touch fill: **none**

I applied the trade-through rule (strict `>`/`<`) and the cushioned rule (below) in a **scratch copy** and ran the full suite against each: **1232 passed both times.** No existing test depends on a touch-only limit fill, so there is nothing to update and nothing to list.

### The prompt's fix does not reproduce the broker on its own headline case

Real RKLB 10-15 bars, live window 09:45 → 14:00 cancel (255 bars): the **only** bar whose high exceeds 54.00 is 11:15, **high 54.02**, a trade-through of 2¢ (3.7 bps). Next highest: 53.998 (11:16). `touch-only bars = 0`: no bar had `high == 54.00` exactly. So `high > entry` (the prompt's rule) still fills RKLB at 11:15, and Alpaca still did not.

I replayed the 3 sessions through the current simulator (levels reconstructed from the bracket legs; sizes check out at exactly 0.25R for all 11):

```
tkr  date        type      entry | sim: fill@time        exit@time           reason     r_plan
RKLB 2026-10-15 limit sell  54.00 | 53.973@11:15   53.457@14:00 time_stop   +0.207      broker: NEVER FILLED
CRWV 2026-10-15 limit sell  64.00 | 63.968@11:09   68.034@13:41 stop        -1.016      broker 64.0916 -> 68.0732 (stop leg 14:42:43)
INTC 2026-10-15 stop  sell  92.00 | 91.954@09:49   94.007@14:30 time_stop   -0.513      broker 91.9823 -> 93.89
CMC  2026-10-15 limit buy   66.50 | 66.063@09:45   65.537@14:00 time_stop   -0.385      broker 66.31 -> 65.52
NVDA 2026-10-15 limit sell 220.00 | no_trigger                                           broker: canceled unfilled
USO  2026-10-16 limit buy  155.50 | no_trigger                                           broker: canceled unfilled
AMAT 2026-10-16 stop  sell 418.00 | 417.791@11:07  416.948@14:00 time_stop   +0.056      broker 417.85 -> 417.06
```
The sim reproduces every ledger number in the prompt (CRWV 63.97→68.03, INTC 91.95→94.01, CMC 66.06→65.81 …). RKLB is the **only** divergence in these three sessions that is a fill-or-not question.

### Calibration against all 35 limit-entry and 10 stop-entry parents in the broker's history

`penetration` = how far the minute-bar extreme went past the order price between activation and the fill (or cancel). From `broker_orders_all.json` (158 orders, 9/10 → 10/17) × real minute bars:

| Outcome | n | penetration |
|---|---|---|
| Limit orders **never filled** (canceled/expired) | 14 | all negative (price never reached) **except RKLB: +0.02 = 3.7 bps** (one has no bars: SPY 9/24) |
| Limit orders **filled** | 21 | smallest are NVDA 10-08 **5.07 bps**, CVS 6.9, UBER 12.5, MAT 23, VFC 24.5 … the rest far larger or gap-through at the open |
| **Stop entries** (breakouts) filled | 5 | AMAT filled at **2.39 bps**, SPY at touch: stops trigger on touch |

Fill-minute vs first-touch-minute (the sharper test): MAT's first touching bar was 09:56, the order filled **10:00**, the first bar through by ≥ 1 tick and ≥ 5 bps. ALEC touched at 10:00 and filled at **10:01**. NVDA 10-08 filled at 12:57, the first bar clearing 5 bps; a 10 bps rule would have been 3 minutes late (13:00).

Exit legs, by contrast, are touch-accurate: **stop legs 67/68** filled within 1 minute of the first touching bar, **take-profit legs 12/14**. The single stop outlier is CRWV 10-15 (stop 68.00; bars printed 68.02–68.20 from 13:41 to 14:01, the broker's stop filled **14:42**). So: *resting limit entries* are the class where the simulator is more generous than the broker; stops, targets and stop-entries are not.

**Consequence for Phase 2 (stated now, not buried later):** strict trade-through is necessary but not sufficient. The consistent rule across all 45 broker observations is *"a limit entry fills only when the bar trades through the limit by at least max(1 tick, 5 bps of the limit)"*. The window of consistent thresholds is (3.7, 5.07] bps; I will use **5 bps**, the same number as the configured `slippage_bps`, i.e. "the market must trade through by at least the friction the sim charges". Gap-through at the open (`open` already through the limit) is unchanged. Stops, targets and `breakout` entries keep touch semantics (the broker evidence supports that). One of the prompt's literal Phase 2 tests changes accordingly (`high 54.01 → fills` becomes `54.03 → fills`, and the real 54.02 bar is added as an explicit "does NOT fill" case); I will flag this again in Phase 2.

---

## Q4 — D3: ledger never reconciled to broker  →  **CONFIRMED**

What is stored: `live_orders` (`agent/migrations_v7.py`): `entry_fill_price, entry_fill_at, exit_reason, exit_fill_price, exit_fill_at, live_r, reconciled, close_order_id` (v15); `outcomes.live_r`. `_settle_pending_close` (`live_monitor.py:160-172`) reads a flatten fill back; `reconcile_evening` (`live_monitor.py:532-599`) copies `live_r` onto `outcomes` and **only logs a mean**: `mean |live_r - simulated_r|` (`:578-580`). It never stores a divergence class and never names a pick.

Who reads it: `chart_payload.py` (drawer only). **Not** the evening review (`outcomes_md`, `grading.py:1050-1140`, prints simulated outcomes only), **not** the grader bundle (`evening.py:889-892` gives only "Live fills recorded for {d}: N"), **not** the trader's log (`traders_log.py:_day_facts` reads `outcomes.r_planned` only, `:119-136`).

Why RKLB was invisible: for a pick whose order never filled, `reconcile_evening` takes the third branch (`:561-577`), finds no fill and no exit leg, and does nothing. The simulator's +0.207R winner stands, with the ledger saying "filled", the broker "canceled", and no line anywhere noting it. *(Prod row for pick 162 not read; from the code and the broker the monitor cancelled it at 14:00:04 ET, so its row is `exit_reason='cancelled_unfilled', status='canceled', live_r NULL`.)*

Sim vs broker, 10-15, by the broker's own fills:

| | sim | broker |
|---|---|---|
| CRWV | 63.97 → 68.03, −1.02R | 64.09 → 68.07, −1.00R, **stop at 14:42, not 13:41** |
| INTC | 91.95 → 94.01, −0.51R | 91.98 → 93.89, −0.48R |
| CMC | 66.06 → 65.81 (−0.39R) | 66.31 → 65.52, −0.32R |
| RKLB | filled, +0.21R | **never filled** |
| day | −1.56R, 1 winner | −$222 realized, 0 winners |

---

## Q5 — D4: shadow ledger double-counts and misprices  →  **CONFIRMED (two separate causes)**

### `classify_shadow` and `forgone_r`

```python
950  def classify_shadow(outcome: dict) -> tuple[str, float | None]:
963      if outcome.get("exit_reason") in ("no_data", "invalid_plan", "invalid_fill"):
964          return "unmeasurable", None
965      if not outcome.get("triggered"):
966          return "scratch", 0.0
967      r = outcome.get("r_multiple") or 0.0          # <-- NOT r_planned
```
`r_multiple` is the diagnostic (fill-to-stop denominator, floored); the headline every taken-trade report uses is `realized_r` = `r_planned` (`grading.py:1012-1021`). **EXECUTION_FIX Phase 3 (`r_planned`) was not shipped.** The earlier Phase 0 addendum put it "on hold" over BMY (−0.71R → −1.01R). The prompt's invariant (*shadow R == taken R exactly*) settles the unit question: shadow must use the taken-pick measure. BMY moves as the addendum predicted; I will say so in Phase 3.

### Cause 1 — the shadow prices with no time stop

`agent/loops/evening.py:140-161`, docstring and shim:
```python
"""... Rejections don't carry a time_stop (not stored on the rejections table), which
behaves the same as a pick with no time_stop."""
shim = {"pick_id": r["rejection_id"], "direction": ..., "entry_type": ..., "entry_price": ...,
        "stop_price": ..., "target_price": ...,
        "entry_not_before": r.get("entry_not_before")}          # no time_stop
```
That is INTC: taken exits at the 14:30 time stop (−0.51R); the shadow rides to the 15:42 stop (−1.01R). The `rejections` table has no `time_stop` column (`ledger.py:175-192`; v6/v8/v9/v25/v27 add other columns). **But the analyst's candidate JSON, which carries `time_stop`, is stored per session in `daily_plans.analyst_json`** (`ledger.py:102`, written at `:700-706`, passed `candidates` at `morning.py:2313`), so even *history* can be repriced with its time stop without a schema change.

### Cause 2 — rows the filters never stopped are counted as "forgone"

`morning.py:943-949` keeps a test-lane pick's rejection row **on purpose**:
```python
# A test-lane pick the risk manager rejected keeps its shadow row: the shadow ledger goes on
# pricing every kill, whatever size the desk then traded it at.
taken = {p["ticker"].upper() for p in plan.get("picks", [])
         if not (p.get("exploration_lane") in _TEST_LANES and p["ticker"].upper() in rejected_rows)}
```
`_TEST_LANES = {coverage, daily_floor, hypothesis}` (`morning.py:1219`). `_grade_rejections` then sums every priced row into `net_forgone_r` (`evening.py:169`) and the trader's log reads the same column (`traders_log.py:216-231`: `SUM(forgone_r)` → slot "net forgone R … positive = the filters cost the desk"). Nothing excludes a rejection that was traded anyway. 10-15's "−1.41R: the filters saved money" is INTC + CMC, both coverage trades. 10-16's "+0.62R: the filters cost the desk" is AMAT alone, traded by coverage and priced +0.06R as taken (the shadow differs because of Cause 1/2 together).

Every consumer reads `rejections.forgone_r` with `IS NOT NULL` / `COALESCE(...,0)` (`ledger.shadow_summary` `:1914-2023`, `question_eval.py:229,350`, `research.py:1004`, `ablation.py:142`), so **writing `forgone_r = NULL` for a lane-traded row excludes it from every total, mean, t-test and verdict without touching those files**.

---

## Q6 — D5: entry order type contradicts the setup  →  **CONFIRMED**

| Where | What it does |
|---|---|
| `prompts/morning_analyst.md:64-69` (v13) | Prose: what `open`/`limit`/`breakout` do; "A short 'on continuation below X' is a breakout at X, never a limit at X"; "Pick the entry_type that matches the thesis you wrote". Setup taxonomy at `:79`. |
| `prompts/morning_risk.md` (v16) checks 2b, 2d | LLM-judged "entry-type match" and "setup fit". |
| `morning.py:625-643` (`_validate_plan`) | The only **code** gate: a `limit`/`breakout` on the wrong side of the last price is dropped as a "disguised market order" (1% tolerance). Plus `:616-624` the 15% grounding gate and `:658-674` trigger-already-crossed. |
| `live_monitor.place_deferred_orders` (`:412-529`) | Sends whatever `entry_type` the pick carries; converts a crossed `breakout` to a market order. |
| `live_broker.entry_order_params` (`:95-105`) | `limit→limit`, `breakout→stop`, anything else `→market`. |

**There is no rule linking `setup_type` to `entry_type`.** `setup_type` is a free label (no membership check in the validator). The incident entries pass every code gate: CMC (catalyst_momentum, buy limit 66.50 below the market, filled at 09:45:12 @ 66.31 the instant it went live as price was already falling through it), USO 10-16 (sector_rotation, buy limit 155.50 vs 156.80, never filled; real bars: open 156.97, low 155.80, close 161.76), RKLB (breakdown_short as a sell limit above the market). Every pick passes through `_validate_plan`, the main picks directly and lane picks via `_trial_validator` (`:1366-1391`), so one tagging point covers all lanes.

---

## Q7 — D6: the grader is not told how its orders work  →  **CONFIRMED**

What the grader receives about a pick (`grading.outcomes_md`, `grading.py:1054-1073`): `Plan: entry {entry_type} @ {entry_price}, stop, target, size, conviction, time_stop`, then `OUTCOME: filled @ X, exit @ Y (reason) … [fill_time -> exit_time]`. Nothing says a `breakout` short is a sell **stop**, that limit/breakout orders go live at `entry_not_before` (09:45), or that the fill time is the moment price traded through the level and not a desk decision. `prompts/evening_grader.md` (v10) asks "Did the entry logic make sense…" (`:23`) and lists "entry slightly chased" under grade B (`:17`), and contains no order-mechanics section. `review_audit.py` has four checks (`volume_contradiction`, `scratch_as_result`, `test_lane_penalized`, `forgone_conclusion`; header `:16-24`); none looks at mechanics. The numeric audit passed AMAT and INTC because no number was wrong, which is exactly what the prompt says.

---

## Q8 — D7: gapfield stats contaminated by forward data  →  **NOT REPRODUCED as stated; DIFFERENT CAUSE (a reporting gap, plus a design weakness I will report, not change)**

Facts from the code:

* A rule has a discovery date and a quarantine date: `research_rules.discovered_at / quarantined_at` (`research.py:198-266`). `discovery_n` and `discovery_effect` are frozen at registration. **Forward** statistics are separate counters (`forward_n, forward_sum_r, forward_sumsq_r`, `rule_states` `:333-395`).
* The guard exists and is exactly the prompt's test: `record_rule_observation` (`research.py:277-330`) returns `False` "in-sample: discovered on or before this session" when `obs_date <= quarantined_at`.
* **The numbers the prompt quotes are the forward counters.** `selfcheck.check_edge_pipeline` (`selfcheck.py:399-424`) prints `"{rid} ({lane}) n={forward_n} mean {forward_sum_r/n:+.2f}R, {looks_done} look(s)"`. `n 68→107, mean +0.30→−0.01, "0 looks"` is that line. They are not in-sample figures.
* **Why "forward observations recorded: 0" while forward rows grew.** The evening's `research.observe_session(con, d)` (`evening.py:830-833`) runs ~16:30 ET and reads `gapfield.forward_rows(con, d)` (`gapfield.py:522-525`), but the gap field for session *d* is priced by the **nightly** job at ~23:00 ET (`nightly.py:75-86`: `scan_session` then `observe_session(..., lanes=("gapfield",))`). At evening time the rows do not exist, so gap-field observations are structurally 0 there and are booked later that night. The jump appears in the next evening's self-check.
* **What is real:** a gap-field forward observation is one (session, industry) unit (`clusters.py`). One night can add ~35 units to one rule (+39, +32, +32 observed), all sharing that day's market tape. Forward `n` therefore overstates independent evidence, and a rule can pass `forward_n_required = 8` in its first night. The sequential test (`sequential.design_for(lane)`) does use a separate field-lane horizon, which is why this was a deliberate design choice, not a leak.
* `gapfield.cell_stats/_rows` (`:507-557`) do mix forward and backfill rows, but only feed the *weekly discovery batch* (`comparisons`, `:587-604`), never a registered rule's validation.

I could not read the three rules' prod rows (`discovered_at`, `quarantined_at`, per-session observation counts) to exclude a data-level problem. Per rule 1 I will not "fix" this in Phase 7; it is written up in the final report instead. A prod read-only block is in the last section.

---

## Q9 — D8: ETF entry levels not grounded  →  **CONFIRMED**

Real bars (this session, `MarketData.daily_bar`):

```
USO 10-15  O 154.78  H 158.10  L 153.80  C 157.31    analyst entry 68.50   (−55%)
USO 10-16  O 156.97  H 162.38  L 155.80  C 161.76    (matches the prompt: open 156.97, LOD 155.80)
XLV 10-15  O 176.43  H 178.86  L 176.22  C 177.19    analyst entry 148     (−16%, just past the 15% gate)
XLE 10-16  O 67.115  H 68.29   L 67.01   C 68.24     analyst entry 87      (+30%)
```
The gate's quote was right every time (USO ~153.6, XLV 176.70, XLE 66.85). The analyst's levels are its training-era memory of those ETFs.

Cause: the bundle gives the analyst the sector ETFs as **percent moves only**. `providers.render_bundle_md` (`:1757-1764`): `**Sectors:** XLE +1.2% | XLV -0.4% | …`, from `SECTOR_ETFS` (`providers.py:86`) via `_recap` (`:968-976`) or `market_recap`. They enter the price table (`premarket_gappers`) only if they gap ≥ 3% (`_gappers` `:1011`), and INDEX_ETFS (SPY/QQQ/IWM) are the only always-included ETFs. USO is not in `SECTOR_ETFS` at all. `prompts/morning_analyst.md:17-22` already forbids pricing from memory ("NEVER price a level from memory…"), which proves a prompt rule alone is not enough. The only enforcement is the post-hoc 15% gate (`morning.py:616-624`), which kills the pick, not the cause. `pick_quotes` (`providers.py:1103`) supplies the gate's reference price after the fact.

---

## Q10 — D9: same-direction market orders after a canceled entry  →  **NOT REPRODUCED: a misreading of bracket-leg timestamps**

From the broker's own records for 10-13 (`GET /v2/orders`, nested legs):

```
MU   sell STOP 940 (a breakout SHORT)  bracket qty 3   submitted 13:45:07Z  FILLED 14:50:50Z (10:50:50 ET) @ 939.34  sell_to_open
  leg  buy LIMIT 895 (take-profit)  submitted_at 14:50:50Z  <- leg ACTIVATION time, not a cancel      canceled 17:00:04Z
  leg  buy STOP 975  (stop-loss)                                                                       canceled 17:00:04Z
buy  MARKET 3  buy_to_close   submitted 17:00:05Z (13:00:05 ET)  filled @ 937.35        <- the time-stop exit of a SHORT

VFC  buy LIMIT 16.30 (a LONG)          bracket qty 138  submitted 13:45:06Z  FILLED 13:46:41Z (09:46:41 ET) @ 16.299275  buy_to_open
  leg  sell LIMIT 17.5 (take-profit) submitted_at 13:46:41Z  <- leg activation, not a cancel          canceled 18:00:04Z
  leg  sell STOP 15.4                                                                                  canceled 18:00:04Z
sell MARKET 138 sell_to_close  submitted 18:00:04Z (14:00:04 ET)  filled @ 15.99        <- the time-stop exit of a LONG
```
"MU buy limit canceled 10:50" is the take-profit **leg**, whose `submitted_at` is the parent's fill time; "VFC sell limit canceled 09:46" likewise. MU's entry was a **short**, so its market buy is `buy_to_close`; VFC's entry was a **long**, so its market sell is `sell_to_close`. Both are exits on the correct side. The local tick log agrees: `updated:1` at 13:00/13:05 and 14:00/14:05 (close, then settle) and nothing at 09:46 or 10:50.

Code paths that can submit a market order in the *entry* direction (complete list): `submit_bracket_order(..., entry_type="open")` from `_place_premarket_orders` (`morning.py:1795-1801`) and from `place_deferred_orders` when a `breakout` is already crossed (`live_monitor.py:496-509`, `send_as = "open"`). Both are idempotent per `pick_id` (`have_order` / `final`, `:446-448`), so a canceled entry cannot be re-sent. Every other market order is `close_position` (`DELETE /v2/positions`), which sends the opposite side by construction. Orders carry no custom `client_order_id` (random UUIDs), so a route cannot be read off the broker; correlation is by `live_orders.live_order_id`.

---

## Gate 0

| | Verdict | One line |
|---|---|---|
| D1 | **Confirmed — larger** | IOVA carried since 9/30 (local-run orphan, coverage lane); monitor and carry cleanup are ledger-driven; one flatten attempt; EXECUTION_FIX Phase 1 never built |
| D2 | **Confirmed in code; different cause for RKLB** | touch rule `>=` is real, but RKLB's fill bar was a 2¢ trade-through; fix needs a calibrated cushion, not only `>` |
| D3 | **Confirmed** | broker data stored but never surfaced, never classified |
| D4 | **Confirmed (two causes)** | no time stop in shadow pricing; lane-traded rows counted as forgone; `r_multiple` not `r_planned` |
| D5 | **Confirmed** | no `setup_type` ↔ `entry_type` rule; prose only |
| D6 | **Confirmed** | grader never told what its orders do |
| D7 | **Not reproduced as stated** | quoted numbers are forward counters; "0 recorded" is an evening/nightly timing artifact; real issue is unit correlation (reported, not changed) |
| D8 | **Confirmed** | bundle shows sector ETFs as % only |
| D9 | **Not reproduced** | leg-timestamp misreading; both market orders are correct-side exits |

D1 and D9 are both explained from direct evidence. **No STOP condition.**

---

## Where this report departs from the prompt (with the evidence above)

1. **No pullback lane exists** (spec only). Phase 1 tests use `live_orders` claims.
2. **No `EXECUTION_FIX_SPEC_v1.md` in the repo.** Phase 1 is built from the prompt's bullets and `docs/EXECUTION_FIX_PHASE0.md`.
3. **D2 cushion** (5 bps / 1 tick) on top of strict trade-through, because strict `>` alone leaves RKLB filled. One literal prompt test changes (54.01 → 54.03 fills). Stops, targets and breakouts stay touch-based, which the broker data supports.
4. **D9 and D7 are skipped in Phase 7** (not confirmed). D7 gets a written finding instead.
5. **"Existing regrade path as a dry run" does not exist for taken picks.** `run.py regrade` (`:102-155`) writes with no `--apply`; only `regrade-shadow` has a dry-run default and its dry run only counts rows. And I have no prod read path, so a 20-session prod number cannot come from me. What I can deliver: (a) real replays of the 3 sessions above from broker-derived levels, (b) the local ledger's rows (labeled local), (c) a read-only impact mode in `scripts/verify_fixes.py` that Jason runs once against prod.
6. **Shadow history can be repriced with time stops** without a schema change, via `daily_plans.analyst_json`.

## Allowlist reconciliation (rule 5)

Names confirmed. Items marked ⚠ need a file outside the phase's list.

| Phase | In-allowlist files that will change | ⚠ Needs outside the list |
|---|---|---|
| 1 | `live_monitor.py`, `live_broker.py`, `ledger.py` (new table), `loops/morning.py`, `api/index.py` (intraday route + status), `health.py`, `delivery.py`, `config.py`, `scripts/intraday_cron.ps1`, `tests/test_eod_backstop.py` | none |
| 2 | `grading.py`, `loops/evening.py`, `delivery.py`, `ledger.py` (schema), tests | ⚠ **`agent/traders_log.py`** (`_day_facts`): the trader's log builds its facts from SQL, not from `delivery.py`, so a broker-headline slot cannot be added in the listed files |
| 3 | `grading.py`, `loops/evening.py`, tests | ⚠ **`agent/traders_log.py`** (`_shadow_facts`) to make "saved/cost money" conditional; the data fix already removes lane-traded rows from its totals |
| 4 | `loops/morning.py` (validator), `config.py`, `ledger.py` (schema + `save_daily_plan` insert), tests | none (persisting the tag in `save_daily_plan` is part of the schema bump) |
| 5 | `grading.py`, `tests/test_grading.py` | ledger column for `r_if_held_to_flatten` rides in Phase 2's schema bump |
| 6 | `prompts/evening_grader.md`, `loops/evening.py`, `review_audit.py`, tests | none |
| 7 | no list given | D8 touches `providers.py` (bundle) and the analyst prompt |

If you want the two `traders_log.py` changes kept out, say so and the broker headline and the saved/cost sentence stay in `outcomes_md` and the evening summary only; the data fixes stand either way.

## Prod read-only block (optional; I could not run it)

Not required to proceed. It answers: any IOVA row in prod, pick 162's live row, and the three gap-field rules' dates. Reads only:

```powershell
cd "C:\Users\Quantum Lab\Desktop\Trade Agent"
$env:DATABASE_URL = ((Get-Content .env | Where-Object { $_ -like 'DATABASE_URL=*' }) -split '=',2)[1].Trim('"')
.\.venv\Scripts\python.exe -c "
from agent import ledger
c = ledger.connect()
print('IOVA picks/live:', c.execute(\"select count(*) from picks where ticker='IOVA'\").fetchone(), c.execute(\"select count(*) from live_orders where ticker='IOVA'\").fetchone())
print('pick 162 live:', c.execute('select status, exit_reason, entry_fill_price, live_r from live_orders where pick_id=162').fetchall())
for r in c.execute(\"select rule_id, state, discovered_at, quarantined_at, discovery_n, forward_n, forward_sum_r, looks_done from research_rules where rule_id in ('Rb3e9fd007078','R13b27a54f27c','R53f505c5b4df')\").fetchall(): print(r)
for r in c.execute(\"select rule_id, session_date, count(*), avg(r_multiple) from research_observations where rule_id in ('Rb3e9fd007078') group by 1,2 order by 2 desc limit 8\").fetchall(): print(r)
"
Remove-Item Env:DATABASE_URL
```

---

## Appendix — evidence kept in the session scratchpad

`broker_orders_all.json` (158 parent orders with legs), `bars_cache.pkl` / `bars_calib.pkl` (real minute bars), `calibrate_out.txt`, `calibrate2_out.txt`, the replay and probe scripts. The scratchpad is session-local; the tables reproduced above are the durable record. Phase 2 stores the RKLB/NVDA/MAT/ALEC bar slices as test fixtures.
