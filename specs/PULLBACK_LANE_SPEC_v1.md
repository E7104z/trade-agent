# PULLBACK LANE SPEC v1 — First Pullback Buy (9 EMA / VWAP confluence), $10–$15

**Status:** proposed 2026-10-10. Revised the same day for the runner lane's removal (`specs/RUNNER_REMOVAL_v1.md`). Not started.
**Delivery:** Cline, in VS Code, one phase at a time. Each phase ends at a gate. Cline does not start the next phase until Jason says so.
**Lane name:** `pullback` (setup label `first_pullback_9ema_vwap`).
**Branch:** `feat/pullback-lane`, cut from the commit that contains the runner removal. One commit per phase.

---

## Preconditions (Jason, before Phase 0)

1. The runner removal is **committed on its own** and **deployed**.
2. At least one full session has run on the new code: morning, intraday ticks, evening (including the one-time retirement of runner rules in the research registry). `/api/table/runs?limit=40` shows those runs `ok` or `degraded`, none `error`.
3. `git status` is clean. Nothing uncommitted (including `webui.py`) sits in the tree when Cline starts. Cline's allowlist check compares against commits, and leftover edits would show up as violations.

Why: if the removal and the new lane ship together and something breaks, there is no way to tell which change caused it. One change per deploy.

---

## How to use this spec in Cline

Paste this as the first message of a fresh Cline task, with this file attached:

> Read `specs/PULLBACK_LANE_SPEC_v1.md` in full before doing anything. Follow the "Rules for Cline" section exactly. Execute **Phase 0 only**. Write the Phase 0 report to `specs/PULLBACK_LANE_PHASE0_REPORT.md`, then stop and wait. Do not write or modify any code in Phase 0.

For each later phase, start a new message:

> Execute **Phase N** of `specs/PULLBACK_LANE_SPEC_v1.md`. Re-read the "Rules for Cline" section and the Phase 0 report first. Stop at the Phase N gate and report.

---

## 0. What this lane is, and what it is not

**It is** a new, isolated, fully mechanical intraday lane. It scans liquid $10–$15 names after the opening surge, waits for the first 2–3 bar pullback into the 9 EMA / VWAP zone, enters on a green bar that holds above both, banks half at +1R, and trails the rest on the 9 EMA.

**It is not** a proven edge. The source material's claims (RVOL > 3, "one of the most reliable edges", the $0.30–$0.45 stop) come from outside this system's evidence base. Per Trade Agent principle ("don't inherit numbers without re-deriving them"), every threshold below is a **pre-registered parameter**, written down before any outcome exists. The lane records every candidate and every rejection so the system can test the hypothesis on its own data.

**It is not the runner lane back.** It shares no code with the removed runner. Two small pieces of runner code are worth keeping (the liquid-universe screen and the intraday volume curve); this lane gets its **own copies**, recovered from git history and pinned by tests (§5).

**No LLM is involved.** Detection, entry, sizing and exits are deterministic. Nothing ranks candidates; first trigger wins, ties broken alphabetically.

---

## 1. Invariants (R-rules). Every phase must hold all of them.

| # | Rule |
|---|---|
| **R-1** | **Isolation.** The lane has its own tables, its own config block, its own modules. It never writes to `picks`, `outcomes`, `rejections`, `live_orders`, any research/integrity table, or the historical `runner_*` tables. It never reads the `runner_*` tables either (they are history, not inputs). |
| **R-2** | **Cannot break anything else.** In `/cron/intraday-check` it runs **after** the live monitor and every existing step, inside its own `try/except`. In the evening loop it runs **last**, inside its own `try/except`. A pullback failure is logged and recorded in `pullback_sessions`; it never raises past its call site. |
| **R-3** | **Completed bars only.** A 5-minute bar whose `t` is 10:00 is complete only when `now >= 10:05:00 ET`. The bar still forming at the tick is dropped before any calculation. (Lesson from the former runner's `scan_version 3`: Alpaca returns the forming bar.) |
| **R-4** | **A gate that cannot run has not passed.** Missing bars, missing EMA seed, missing ADV, missing price: the ticker is **rejected with a named reason** and counted. Never default to pass. (`pm_vol` null-rate lesson.) |
| **R-5** | **Detection is a pure replay.** `pullback_detect.evaluate()` is a pure function of (bars, config). Every tick re-runs it over all completed bars from 09:30. The same bars always give the same answer. No detection state is stored between ticks except the final candidate row. |
| **R-6** | **First trigger anchors.** A candidate row is inserted once per (session, ticker) with `ON CONFLICT DO NOTHING`. Later ticks never overwrite it. One trade per ticker per session. |
| **R-7** | **Naive ET timestamps.** Every TIMESTAMP column stores ET wall-clock with tzinfo stripped (`util.ET`, then `.replace(tzinfo=None)`). Tz-aware writes shift on Neon. |
| **R-8** | **Live and simulator use identical rules.** Same bars basis (native Alpaca 5Min for EMA/VWAP/trail decisions), same levels, same chase rule, same time stop. Where live cannot match the sim, the difference is recorded, never hidden. |
| **R-9** | **Versioned semantics.** `DETECT_VERSION` and `SIM_VERSION` constants live in the lane's modules and are written on every row. Any semantic change bumps the version. Rows of different versions are never pooled. |
| **R-10** | **Additive schema only.** New tables via one new migration file. Idempotent (`IF NOT EXISTS`), bare token `DOUBLE`, no sequences, every column nullable except primary-key columns. |
| **R-11** | **Stand-down vs outage is always distinguishable.** Every session writes `pullback_sessions` telemetry (ticks run, universe size, provider status, rejection counters). A zero-trade day must be explainable from that row alone. |
| **R-12** | **Open positions are always managed.** Turning off `pullback.live_orders_enabled` stops new orders only. The position manager runs whenever any `pullback_orders` row is open, regardless of flags. |
| **R-13** | **Own shares only.** The lane never calls `close_position(ticker)`. It sells exactly its own recorded quantity. |

---

## 2. Pre-registered strategy definition

All values below live in the `pullback` config block (§4). They are the hypothesis, stated before any data. Changing one later is a version bump, not a tweak.

### 2.1 Bars and indicators

| Item | Definition |
|---|---|
| Bar basis | **Native Alpaca `5Min` bars**, regular session only. *Why not 2-minute:* the intraday trigger ticks every 5 minutes, so a 2-minute signal cannot be acted on when it forms. A 5-minute basis is the only one the live system can follow honestly. |
| 9 EMA | `agent.indicators.ema_series(closes, 9)` over 5-min RTH closes, **continuous across sessions**: the previous session's full RTH 5-min bars are prepended, so the EMA is warm at 09:45. No premarket bars. If the prior session's bars are missing → reject `ema_unavailable`. |
| VWAP | `agent.indicators.vwap_series(bars)` over **today's** RTH 5-min bars anchored at 09:30 (typical price × volume). |
| RVOL | `cum_rth_volume_through_bar_k ÷ (adv10 × expected_volume_fraction(minutes_in))`. `expected_volume_fraction` is the lane's **own copy** of the intraday volume curve from the pre-removal `agent/runner_indicators.py` (§5). `minutes_in` comes from the last bar's ET timestamp minus 09:30, never from a clock. `adv10` = mean daily volume of the prior 10 sessions, fetched once per session and cached. Fewer than 5 valid prior sessions → reject `rvol_unavailable`. |
| One indicator module | EMA and VWAP come only from `agent.indicators`. No other EMA/VWAP implementation is used anywhere in the lane. |

### 2.2 Universe and scanner gates

| Gate | Rule | Reject reason |
|---|---|---|
| Base universe | `pullback_universe.build_universe(...)`: the lane's own port of the pre-removal liquid-universe screen (tradable assets, previous-session dollar-volume floor, ETF/fund exclusion), cached once per session in `pullback_universe_cache` | — |
| Prefilter | previous close in `[prefilter_prev_close_min, prefilter_prev_close_max]` = **[7.00, 20.00]** | (not counted per ticker; universe stat) |
| Price | trigger bar close in `[price_min, price_max]` = **[10.00, 15.00]** | `price` |
| Day volume | cumulative RTH volume through trigger bar ≥ **500,000** | `day_volume` |
| RVOL | ≥ **3.0** at the trigger bar | `rvol` / `rvol_unavailable` |
| Instrument | operating-company stock (re-checked on triggered names only, with `instruments.fund_from_profile` if that module still exists, otherwise the lane's own port of it; Phase 0 decides) | `instrument` |
| Catalyst | news count in [prior session 16:00 ET, trigger bar end] from the existing Alpaca news provider. `catalyst_gate = "record"` (default): recorded, not gated. `"enforce"`: zero news → reject. | `catalyst` |
| Ticker in use | any open position or open order on the paper account for this ticker, or the ticker appears in today's `picks` | `ticker_in_use` (live placement only; the candidate row still exists for the sim) |

### 2.3 Detection state machine (pure, in `pullback_detect.evaluate`)

Walk today's completed 5-min bars in order, `i = 0` is the 09:30 bar.

1. **Surge.** Track the high of day `HOD` and its index `h`. The surge is valid once bars 09:30, 09:35 and 09:40 are complete (the 09:45 tick) **and** `HOD ≥ session_open × (1 + min_surge_pct/100)` with `min_surge_pct = 1.0`, **and** the close of bar `h` is above VWAP at `h`. Until valid: `no_surge`.
2. **Pullback bars.** A bar after `h` whose high `< HOD` is a pullback bar. Count consecutive pullback bars `p` since `h`. A bar that makes a new high resets `HOD`, `h`, and `p = 0`, **but only while `p < pullback_min_bars`**. Once `p ≥ pullback_min_bars` (2), this is *the first pullback*; it either triggers or ends the setup for the day.
3. **Trigger.** Bar `k` triggers when **all** hold:
   - `pullback_min_bars ≤ p ≤ pullback_max_bars` (2..3) pullback bars precede `k` (bar `k` itself may also be a pullback bar);
   - bar `k` is green: `close > open`;
   - **zone touched:** `L ≤ max(ema9_k, vwap_k) × (1 + zone_tol_pct/100)` where `L` = lowest low of the pullback bars and bar `k`;
   - **zone held:** `L ≥ min(ema9_k, vwap_k) × (1 − zone_tol_pct/100)`;
   - **holds above both:** `close_k > max(ema9_k, vwap_k)`;
   - **confluence:** `|ema9_k − vwap_k| ≤ close_k × ema_vwap_max_gap_pct/100`;
   - **structure:** `ema9_k ≥ vwap_k`;
   - **VWAP slope:** `vwap_k > vwap_{k − vwap_slope_bars}` strictly (3 bars);
   - **window:** bar `k` ends at or before `entry_window_end_et` (11:00).
   - `zone_tol_pct = 0.15`, `ema_vwap_max_gap_pct = 0.50`.
4. **Terminal states.** Exactly one per ticker per session:
   - `triggered` (with `trigger_idx`);
   - `pullback_too_long` (`p > pullback_max_bars` with no trigger);
   - `broke_zone` (a pullback bar closes below `min(ema9, vwap) × (1 − zone_tol_pct/100)`);
   - `window_closed` (11:00 passed while waiting);
   - non-terminal (still waiting): `no_surge`, `no_pullback_yet`, `in_pullback`.
5. Each failed trigger condition on the bar that would have triggered is reported as `fail_reason` (`zone_miss`, `vwap_slope`, `ema_below_vwap`, `gap_too_wide`, `not_green`, `not_above`). The **first** failing check in the order listed above is the one reported.

### 2.4 Plan at trigger

| Item | Rule |
|---|---|
| Planned entry `E` | close of trigger bar `k` |
| Stop `S` | `min(L, ema9_k) − stop_buffer` (`stop_buffer = 0.01`) |
| Risk/share `R$` | `E − S` |
| Stop sanity | reject `stop_geometry` if `R$ < max(min_stop_abs, E × min_stop_pct/100)` (0.10, 0.5%) or `R$ > E × max_stop_pct/100` (5.0%) |
| Scale level | `E + scale_at_r × R$` (1.0R), measured from **planned** entry so the live take-profit can be set at submission |
| Decision time | `max(trigger bar end, tick_et)`: the tick that recorded the candidate. Stored as `decision_et`. |
| Chase rule | if the entry reference (live: latest trade; sim: first 1-min open at/after `decision_et`) `> E + max_chase_r × R$` (0.25R) → not taken, `chased`. If `≤ S` → not taken, `below_stop`. |
| R basis | realized R uses the **worse** of planned entry and fill, in units of planned `R$` (house convention). |

### 2.5 Sizing

```
risk_dollars = min(dollars_per_r × size_r, account_equity × max_risk_pct_equity / 100)
qty          = floor(risk_dollars / R$)
qty          = min(qty, floor(max_position_notional / E))
qty_a        = floor(qty × scale_fraction)        # the half sold at +1R
qty_b        = qty − qty_a                         # the half trailed
require qty_a ≥ 1 and qty_b ≥ 1, else skip "qty_too_small_to_scale"
```

Defaults: `dollars_per_r = 100`, `size_r = 0.25` (minimum size for an unproven lane: $25 at risk per trade), `max_risk_pct_equity = 1.0`, `max_position_notional = 5000`, `scale_fraction = 0.5`.

Source example check: $100 risk, $0.40 stop → 250 shares → $3,000 at $12. With `size_r = 1.0` and the notional cap raised, the formula reproduces it exactly. Test it (§Phase 1).

### 2.6 Exits

| Exit | Rule |
|---|---|
| Stop | full remaining quantity at `S`. Never moved. (Source does not move to breakeven; neither does v1.) |
| Scale | `qty_a` sold at the scale level. |
| Trail | **active only after the scale fills.** At each completed 5-min bar that **ends after** the scale fill: if `close < ema9` at that bar → sell `qty_b` at the next price (sim: next 1-min open; live: market at that tick). |
| Time stop | checked once, at the close of the `time_stop_bars`-th (3rd) completed 5-min bar that starts at/after the fill. If the scale has **not** filled **and** that close `< E + time_stop_progress_r × R$` (0.5R) → exit everything. |
| End of day | the desk's flatten: `grading.EOD_FLATTEN_MINUTES` before `market_close_et(d)` (15:55 on a full day, 12:55 on a half day). |
| Sim intra-bar order (per 1-min bar) | stop first; then EOD flatten; then scheduled exits (trail/time stop) at bar open; then the scale limit. If stop and scale both touch in one 1-min bar, the **stop wins**. |

---

## 3. Data model (one migration, next free number per Phase 0)

File: `agent/migrations_vNN.py`, where `NN` is the next free number found in Phase 0 (the runner removal may already have used one). Appended to `ledger._MIGRATIONS` with the same two-line pattern as the last existing migration block.

```sql
CREATE TABLE IF NOT EXISTS pullback_candidates (
    session_date DATE NOT NULL, ticker VARCHAR NOT NULL,
    detect_version INTEGER, tick_et TIMESTAMP, trigger_bar_et TIMESTAMP, decision_et TIMESTAMP,
    session_open DOUBLE, hod DOUBLE, hod_bar_et TIMESTAMP, pullback_bars INTEGER, pullback_low DOUBLE,
    trigger_open DOUBLE, trigger_close DOUBLE, ema9 DOUBLE, vwap DOUBLE, vwap_slope DOUBLE,
    ema_vwap_gap_pct DOUBLE, rvol DOUBLE, adv10 DOUBLE, day_volume DOUBLE, prev_close DOUBLE,
    change_pct DOUBLE, catalyst_count INTEGER, catalyst_headline VARCHAR,
    entry_price DOUBLE, stop_price DOUBLE, scale_price DOUBLE, risk_per_share DOUBLE,
    instrument_class VARCHAR, config_json VARCHAR,
    PRIMARY KEY (session_date, ticker)
);

CREATE TABLE IF NOT EXISTS pullback_outcomes (
    session_date DATE NOT NULL, ticker VARCHAR NOT NULL,
    sim_version INTEGER, detect_version INTEGER, graded_at TIMESTAMP,
    triggered BOOLEAN, invalid_reason VARCHAR,
    entry_fill DOUBLE, entry_fill_et TIMESTAMP,
    leg_a_exit DOUBLE, leg_a_exit_et TIMESTAMP, leg_a_reason VARCHAR, leg_a_r DOUBLE,
    leg_b_exit DOUBLE, leg_b_exit_et TIMESTAMP, leg_b_reason VARCHAR, leg_b_r DOUBLE,
    r_total DOUBLE, mfe_r DOUBLE, mae_r DOUBLE, scale_hit BOOLEAN, time_stop_hit BOOLEAN,
    eod_r_unmanaged DOUBLE, detect_replay_match BOOLEAN,
    PRIMARY KEY (session_date, ticker)
);

CREATE TABLE IF NOT EXISTS pullback_sessions (
    session_date DATE PRIMARY KEY,
    detect_version INTEGER, sim_version INTEGER,
    first_tick_et TIMESTAMP, last_tick_et TIMESTAMP, ticks_run INTEGER, ticks_expected INTEGER,
    ticks_skipped_budget INTEGER, provider_degraded BOOLEAN, degraded_reason VARCHAR,
    universe_size INTEGER, prefiltered INTEGER, adv_coverage_pct DOUBLE,
    counters_json VARCHAR, candidates INTEGER, orders_placed INTEGER, orders_skipped_json VARCHAR,
    graded BOOLEAN, graded_at TIMESTAMP, grade_error VARCHAR, last_error VARCHAR
);

CREATE TABLE IF NOT EXISTS pullback_orders (
    pullback_order_id VARCHAR PRIMARY KEY,        -- broker order id of the entry
    session_date DATE, ticker VARCHAR, leg VARCHAR, -- 'a' (scale) | 'b' (trail)
    qty INTEGER, planned_entry DOUBLE, stop_price DOUBLE, scale_price DOUBLE,
    submitted_at TIMESTAMP, status VARCHAR,
    entry_fill_price DOUBLE, entry_fill_at TIMESTAMP,
    exit_reason VARCHAR, exit_fill_price DOUBLE, exit_fill_at TIMESTAMP,
    exit_order_id VARCHAR, live_r DOUBLE, reconciled BOOLEAN, raw_order_json VARCHAR
);

CREATE TABLE IF NOT EXISTS pullback_universe_cache (
    session_date DATE PRIMARY KEY,
    universe_json VARCHAR,     -- {"liquid": [tickers], "prev": {ticker: [prev_close, prev_volume]}}
    universe_built_at TIMESTAMP,
    adv_json VARCHAR,          -- {ticker: [adv10, n_sessions]} for the prefiltered set only
    adv_built_at TIMESTAMP
);
```

`counters_json` keys (all integers, cumulative for the session): `price`, `day_volume`, `rvol`, `rvol_unavailable`, `ema_unavailable`, `no_bars`, `no_surge`, `pullback_too_long`, `broke_zone`, `window_closed`, `zone_miss`, `vwap_slope`, `ema_below_vwap`, `gap_too_wide`, `not_green`, `not_above`, `stop_geometry`, `instrument`, `catalyst`, `triggered`. Each ticker's **terminal** reason is counted once per session (not once per tick).

`ticks_expected` = number of 5-minute marks from `scan_start_et` to `entry_window_end_et` inclusive on that session.

---

## 4. Config block

Add to `agent/config.py` DEFAULTS and to `config.json`. **Every switch defaults OFF.** With `enabled: false` the lane changes nothing.

```json
"pullback": {
  "enabled": false,
  "grade_enabled": false,
  "live_orders_enabled": false,
  "prep_start_et": "09:30",
  "scan_start_et": "09:45",
  "entry_window_end_et": "11:00",
  "universe_min_dollar_volume": 5000000,
  "prefilter_prev_close_min": 7.0,
  "prefilter_prev_close_max": 20.0,
  "price_min": 10.0,
  "price_max": 15.0,
  "min_day_volume": 500000,
  "min_rvol": 3.0,
  "adv_sessions": 10,
  "adv_min_sessions": 5,
  "catalyst_gate": "record",
  "min_surge_pct": 1.0,
  "pullback_min_bars": 2,
  "pullback_max_bars": 3,
  "zone_tol_pct": 0.15,
  "ema_vwap_max_gap_pct": 0.5,
  "vwap_slope_bars": 3,
  "stop_buffer": 0.01,
  "min_stop_abs": 0.10,
  "min_stop_pct": 0.5,
  "max_stop_pct": 5.0,
  "max_chase_r": 0.25,
  "scale_at_r": 1.0,
  "scale_fraction": 0.5,
  "time_stop_bars": 3,
  "time_stop_progress_r": 0.5,
  "dollars_per_r": 100,
  "size_r": 0.25,
  "max_risk_pct_equity": 1.0,
  "max_position_notional": 5000,
  "max_live_orders_per_day": 5,
  "max_open_positions": 3,
  "budget_reserve_s": 60
}
```

`universe_min_dollar_volume` carries over the removed screen's $5M/day floor as the lane's own explicit setting, rather than a constant buried in ported code.

Unit note: every `*_pct` key in this block is **percent** (`min_stop_pct: 0.5` means 0.5%). A test pins this.

A malformed value falls back to the default and logs a WARN; it never raises. Clock strings are parsed by one helper in `pullback_scan.py` that returns the default on any parse error. A test pins this.

---

## 5. Module layout

| File | Role | I/O? |
|---|---|---|
| `agent/pullback_detect.py` | `DETECT_VERSION`, `expected_volume_fraction` (own copy), `evaluate`, `plan`, `size`, `completed_bars` | **None.** No clock, no network, no DB. |
| `agent/pullback_universe.py` | `build_universe(md, con, d, cfg)`: port of the pre-removal liquid-universe screen, cached via `pullback_store` | network + DB |
| `agent/pullback_store.py` | All SQL for the five tables (insert-once candidate, upsert session, counters merge, orders CRUD, universe/ADV cache) | DB only |
| `agent/pullback_scan.py` | `tick(cfg, at, md, con) -> dict`: market-day/window checks, universe, ADV, bar fetch, call `evaluate`, write candidates and telemetry | network + DB |
| `agent/pullback_sim.py` | `SIM_VERSION`, `simulate(...)` (pure), `grade_session(cfg, d, md, con)` | pure core + thin I/O wrapper |
| `agent/pullback_live.py` | `place`, `manage`, `carry_cleanup` | broker + DB |
| `tests/test_pullback_*.py` | one file per module plus `test_pullback_wiring.py` | — |

**Ported code (recovered from git, never re-added under its old name):**

| Piece | Source | Destination |
|---|---|---|
| Liquid-universe screen (`build_universe` and only what it needs) | `agent/runner_universe.py` at the commit **before** its deletion | `agent/pullback_universe.py` |
| Intraday volume curve (`expected_volume_fraction` and its constants) | `agent/runner_indicators.py` at the commit before its deletion | `agent/pullback_detect.py` |
| `fund_from_profile` (only if `agent/instruments.py` no longer exists) | `agent/instruments.py` at the commit before its deletion | `agent/pullback_universe.py` |

Recover a file with:

```
git show $(git log --diff-filter=D -1 --format=%H -- agent/runner_universe.py)^:agent/runner_universe.py
```

Port rules: copy the logic unchanged. The only allowed changes are (a) cache reads and writes go through `pullback_store` and `pullback_universe_cache`, (b) the dollar-volume floor reads `pullback.universe_min_dollar_volume`, (c) imports of removed modules are replaced by the ported piece itself. Each ported function's docstring names the source file and commit hash. Anything else that would have to change is a CONFLICT to report, not to fix.

**Allowed imports from existing code:** `agent.indicators`, `agent.util`, `agent.config`, `agent.ledger` (connect only), `agent.providers`, `agent.instruments` (if it exists), `agent.grading` (constants and friction helpers only: `EOD_FLATTEN_MINUTES`, the slippage helper, `_is_thin`), `agent.live_broker`. Nothing else.

---

## Rules for Cline (read before every phase)

1. **Allowlist.** Each phase lists the files it may create or modify. Touch nothing else. At the end of the phase, run `git diff --stat <phase start commit>` and confirm every path is on that phase's allowlist. If not, revert the extra change.
2. **Denylist (never modify, any phase):** `agent/grading.py`, `agent/live_monitor.py`, `agent/integrity.py`, `agent/research.py`, `agent/buckets.py`, `agent/stats.py`, `agent/loops/morning.py`, `agent/loops/weekly.py`, `agent/chart_payload.py`, `agent/memory.py`, `agent/instruments.py`, `webui.py`, everything in `prompts/`, the playbook, `vercel.json`, every existing migration file, and **every existing test file**.
3. **Never restore the runner.** Do not re-create any `runner_*` module, route, cron, config key, prompt, or test. Ported pieces live only under the `pullback_` names in §5. Do not read or write the historical `runner_*` tables.
4. **Baseline first.** Phase 0 records the full test-suite result (`.venv\Scripts\python -m pytest -q`). Expected after the runner removal: 1,232 passing. Pre-existing failures are listed, not fixed.
5. **No regressions.** After each phase, run the full suite. Every test that passed at baseline must still pass. If one fails, the cause is in this phase's diff. Fix it **inside the allowlist** only.
6. **Never weaken a test.** Do not edit existing tests, add `skip`/`xfail`, loosen tolerances, or wrap code in a broad `except` to make a test pass.
7. **Two-attempt limit.** If a failure is not fixed after two attempts, stop. Report the test name, the full error, what you tried, and your best guess at the cause. Do not try a third approach on your own.
8. **No drive-by changes.** No refactors, renames, reformatting of existing files, dependency upgrades, or "while I'm here" fixes. If you see a bug outside scope, write it in the phase report.
9. **Ask instead of guessing.** If this spec is ambiguous or contradicts the code, stop and ask. Do not pick an interpretation silently.
10. **Additive edits to shared files.** Where a phase allows editing an existing file (`ledger.py`, `config.py`, `config.json`, `api/index.py`, `run.py`, `loops/evening.py`, `live_broker.py`, `scripts/db_preflight.py`), the edit must be purely additive at the exact location named. No existing line changes.
11. **Phase report.** End every phase with: files changed, tests added (count), full-suite result vs baseline, gate checklist with ✅/❌, and anything that surprised you.

---

## Phase 0 — Verify before building (hard blocker, read-only)

**Allowlist:** `specs/PULLBACK_LANE_PHASE0_REPORT.md` (create) and this spec file (copy into `specs/`). No code.

Answer each question with file path, line numbers, and the exact signature or SQL you found. If an answer contradicts this spec, say so at the top of the report under **CONFLICTS**.

**Preconditions**
1. Confirm the Preconditions section: the runner removal commit hash, `git status` clean, and the current branch.

**Code facts**
2. Next free migration number. List the last three `migrations_vNN` imports in `agent/ledger.py`.
3. Exact signatures and return shapes: `indicators.ema_series`, `indicators.vwap_series`, `live_broker.quantity_for_r`, `live_broker.submit_bracket_order`, `live_broker.list_positions`, `live_broker.recent_orders`, `live_broker.cancel_order`, `live_broker.get_order`, `live_broker._request`, `live_broker.last_error`.
4. Does `live_broker` have a function that (a) submits an **OTO** order (entry + stop-loss only), and (b) submits a plain **market sell for a given qty**? If not, say so. (Phase 4 adds them.)
5. The Alpaca news function in `agent/providers.py`: name, arguments, return shape, and whether it supports a start/end window.
6. `Alpaca.multi_bars`, `Alpaca.multi_daily_bars`, `Alpaca.tradable_assets`, `Alpaca.snapshots`: do they **still exist** after the runner removal? Signatures. Does `multi_bars` return tz-aware `t`, and is `t` the bar **start**? **If `tradable_assets` or `multi_daily_bars` was removed, list it under CONFLICTS and stop.** Restoring a provider method is Jason's decision.
7. Does `agent/instruments.py` still exist? If yes, the signature of `fund_from_profile` and which `md` call supplies a profile. If no, say so (Phase 2 ports it).
8. **Ported sources.** For `runner_universe.py` and `runner_indicators.py`: the commit hash that deleted each, and the full text of `build_universe`, `expected_volume_fraction`, and every helper/constant they need, as they existed in the parent of that commit. List every import those functions make, and mark each one **still exists** or **removed**.
9. The values `expected_volume_fraction` returned (pre-removal source) for `minutes_in` = 5, 15, 60, 195, 385, 390. Phase 1 pins these exactly.
10. How `/cron/intraday-check` in `api/index.py` orders its steps **now** (after the removal). Quote the full handler. Identify the exact line after which the pullback call should go, and whether the handler already captures a request-start time.
11. How the evening loop (`agent/loops/evening.py`) ends now. Identify the exact line where a final guarded pullback call should go, and how it tracks the 300 s budget.
12. `grading.EOD_FLATTEN_MINUTES`, the slippage/friction helper the evening grader uses, and `_is_thin`: names and signatures.
13. Does the morning lane check whether a ticker is already held before placing an order? Quote the code or say "no check".
14. Test conventions: quote a fixture that builds a temp ledger (e.g. from `tests/test_live_monitor.py`), and an existing test that proves a failing step does not break `/cron/intraday-check`.
15. `util`: `is_market_day`, `market_close_et`, `prev_market_day`, `now_et`, `today_et`, `ET`: confirm they exist and their signatures.
16. List every file the runner removal **deleted** and every file it **modified** (from the removal commit), so later phases know which shared files changed recently.

**Production facts** (use `/api/table/<name>` via an authenticated session, or a local `DATABASE_URL`. If neither is available, write "BLOCKED: no data access" and stop. Never search the machine for credentials.)

17. `/api/table/runs?limit=40`: paste the latest five runs and confirm the first post-removal morning, evening and intraday runs are `ok` or `degraded`.
18. On the last five sessions, what time did the **first** intraday tick of the day reach production? (heartbeat `intraday_check`). This lane needs ticks from 09:35 ET (prep) and 09:45 ET (scan). If the first tick is consistently later than 09:50, **flag it at the top of the report**: the lane cannot see its window.
19. Historical reference only (read once for this report, never by code): `runner_sessions.universe_size` on its last 5 sessions. The ported universe builder should produce a similar count (~4,000+). This is the sanity check for the port.

**Baseline**
20. Full test-suite result: passed / failed / skipped counts, and the names of any failing tests.

**Gate 0:** report written; every question answered or marked BLOCKED; CONFLICTS section present (may be empty). **Stop. Wait for Jason.**

---

## Phase 1 — Pure detection, planning and sizing

**Allowlist:** `agent/pullback_detect.py` (new), `tests/test_pullback_detect.py` (new).

Build `pullback_detect.py` exactly per §2.1–§2.5. Imports: `agent.indicators`, the standard library, `dataclasses`/`typing`. Nothing else.

```python
DETECT_VERSION = 1

def expected_volume_fraction(minutes_in: int) -> float | None   # own copy, source cited (§5)
def completed_bars(bars: list[dict], at: datetime, minutes: int = 5) -> list[dict]
def evaluate(bars_today: list[dict], bars_prev: list[dict], *, adv10: float | None,
             cfg: dict) -> dict
    # returns {"state", "reason", "fail_reason", "trigger_idx", "features": {...}}
def plan(result: dict, cfg: dict) -> dict | None      # entry, stop, scale, risk_per_share, or reject reason
def size(plan: dict, *, equity: float | None, cfg: dict) -> dict   # qty, qty_a, qty_b, risk_dollars, or skip reason
```

**Required tests** (synthetic bars, every expected number worked out by hand in a comment):

1. **Golden day.** One hand-built session that triggers on a specific bar. Assert trigger bar time, EMA9, VWAP, `L`, `E`, `S`, `R$`, scale level, RVOL, and sizing (qty_a, qty_b) to 1e-6.
2. **Source example.** `risk_dollars = 100`, `R$ = 0.40`, notional cap high → qty 250; at E = 12.00, notional = 3,000.
3. One test per **reject/terminal reason** in §2.3 and §2.4 (no_surge, pullback_too_long, broke_zone, window_closed, zone_miss, vwap_slope, ema_below_vwap, gap_too_wide, not_green, not_above, stop_geometry, ema_unavailable, rvol_unavailable). Each uses the golden day with **one** change.
4. **First pullback only.** A path with a valid 2-bar pullback that fails (`broke_zone`), followed later by a perfect setup → state stays `broke_zone`; no trigger.
5. **New high before min bars resets.** One red bar then a new high → HOD resets, no pullback counted.
6. **Completed bars (R-3).** At 10:07:30 the 10:05 bar is excluded; at 10:10:00 it is included.
7. **Replay determinism (R-5).** `evaluate` on bars truncated at each tick from 09:45 to 11:00 never changes its answer once terminal, and the triggered result equals the result on the full-day bars truncated at the trigger.
8. **Half day.** Window end clamps correctly on a 13:00 close (11:00 still before close; confirm no crash).
9. **Unit pin.** `min_stop_pct = 0.5` means 0.5%, not 50% and not 0.005.
10. **Volume curve pin.** `expected_volume_fraction` returns exactly the six values recorded in Phase 0 Q9.
11. **One indicator module.** The module's imports include `agent.indicators` and no other EMA/VWAP source (inspect `pullback_detect.__dict__` / source).
12. **qty too small.** `qty == 1` → skip `qty_too_small_to_scale`.

**Gate 1:** all new tests pass; full suite equals baseline; diff touches only the allowlist. **Stop.**

---

## Phase 2 — Schema, universe, scan tick, wiring (no orders, no grading)

**Allowlist:**
- `agent/migrations_vNN.py` (new; number from Phase 0)
- `agent/pullback_store.py`, `agent/pullback_universe.py`, `agent/pullback_scan.py` (new)
- `tests/test_pullback_universe.py`, `tests/test_pullback_scan.py`, `tests/test_pullback_wiring.py` (new)
- `agent/ledger.py`: **two lines only**, the migration import/append after the last existing migration block
- `agent/config.py`: add the `pullback` DEFAULTS block (§4)
- `config.json`: add the `pullback` block with `enabled: false`
- `api/index.py`: (a) add the five tables to `_QUERYABLE_TABLES` with their date columns; (b) add the guarded pullback call in `/cron/intraday-check` at the line named in Phase 0 Q10
- `run.py`: add subcommand `pullback` (one tick, or a loop, with `--at HH:MM` and `--no-orders`), following the pattern of an existing subcommand
- `scripts/db_preflight.py`: add a `PULLBACK_TABLES` list and its check, following the existing table-list pattern

**`pullback_universe.build_universe(md, con, d, cfg) -> (liquid, prev)`**: the port (§5). Reads `pullback_universe_cache` first; on a miss, runs the screen and writes the cache.

**`pullback_scan.tick(cfg, *, at=None, md=None, con=None, request_started=None) -> dict`**, in this order:

1. `pullback.enabled` false → `{"skipped": "pullback.enabled is false"}`.
2. Not a market day → skip. Before `prep_start_et` → skip. Between `prep_start_et` and `scan_start_et` → run steps 4–5 only (prep), then return. After `entry_window_end_et` → skip with `{"skipped": "window closed"}` (still update `last_tick_et`).
3. Budget: if hosted (`VERCEL` env set) and elapsed time since `request_started` > `300 − budget_reserve_s`, skip and increment `ticks_skipped_budget`.
4. Universe: `build_universe` (cached after the first build) → prefilter by previous close. Empty universe → `provider_degraded = true`, `degraded_reason = "universe_empty"`, return. Never recorded as a stand-down.
5. ADV: if `adv_json` is empty for today, fetch `adv_sessions` daily bars for the prefiltered set in one batched call and store. Record `adv_coverage_pct`. Below 90% → `provider_degraded = true`, `degraded_reason = "adv_coverage"`. **Prep pacing:** one prep tick does at most one of steps 4 or 5 if the universe build alone took more than half the remaining budget; the next tick continues. If prep is still incomplete at `scan_start_et`, the scan tick finishes it first (budget permitting) and records `degraded_reason = "late_prep"`.
6. Bars: one `multi_bars(..., "5Min", prev_session 09:30 ET, at)` call for prefiltered tickers that do not already have a candidate row today. Apply `completed_bars`. No bars for **any** symbol → `provider_degraded = true`, `degraded_reason = "no_bars"`.
7. For each ticker: `evaluate` → if triggered, `plan` → instrument check → catalyst lookup (triggered names only) → insert candidate (insert-once). Count each ticker's terminal reason once per session (keep the already-counted tickers inside `counters_json` under `_counted`).
8. Upsert `pullback_sessions`: tick counts, first/last tick, universe sizes, counters, candidates.
9. Return a compact dict (counts only, no bar data).

**Wiring in `/cron/intraday-check`:** after the last existing step (Phase 0 Q10), add:

```python
if (cfg.get("pullback", {}) or {}).get("enabled") and is_market_day(today_et()):
    try:
        from agent import pullback_scan
        result["pullback"] = pullback_scan.tick(cfg, request_started=<request start monotonic time>)
    except Exception as e:
        log("intraday", f"WARN pullback tick failed; monitor unaffected: {e}")
        result["pullback"] = {"error": str(e)[:400]}
```

If the handler has no request-start time, capture `time.monotonic()` as its **first line** (additive) and say so in the report.

**Required tests:**
1. Migration applies twice cleanly on DuckDB (idempotent); all five tables exist with the listed columns.
2. `enabled: false` → no network call, no DB write (stub `md` that fails on any call).
3. Universe port: with a stubbed provider, `build_universe` applies the dollar-volume floor at `universe_min_dollar_volume`, excludes funds/ETFs the same way the source did, and a second call reads the cache without any provider call.
4. Insert-once: two ticks that both see the trigger → one row; the second tick does not change any column.
5. Outage vs stand-down: empty universe → `provider_degraded` true and `candidates` 0; full universe with all tickers `no_surge` → `provider_degraded` false.
6. A forming bar at the tick never affects the result (stub returns a 10:05 bar at a 10:07 tick with an extreme print).
7. Counters count a ticker's terminal reason once per session across many ticks.
8. Budget skip increments `ticks_skipped_budget` and makes no bar fetch.
9. Prep ticks (09:35, 09:40) build the cache and never evaluate a ticker.
10. **Wiring:** with pullback enabled and `pullback_scan.tick` monkeypatched to raise, `/cron/intraday-check` returns 200 and every existing key in its response is present and unchanged. (Mirror the existing test found in Phase 0 Q14.)
11. **Wiring:** with pullback disabled, the `/cron/intraday-check` response has no `pullback` key and is identical to baseline for a fixed stubbed input.
12. Naive ET (R-7): every TIMESTAMP written has `tzinfo is None` and equals ET wall-clock.
13. `/api/table/pullback_candidates` returns 200 for an authenticated test client.
14. `run.py pullback --at 10:05 --no-orders` runs one tick against a temp ledger with a stub `md`.
15. A malformed clock string in config falls back to the default with a WARN.

**Gate 2:** tests pass; full suite equals baseline; `scripts/db_preflight.py` passes on a fresh local DuckDB; diff matches allowlist. **Stop.**

**Production soak after Gate 2 (Jason):** set `pullback.enabled: true` (orders and grading still off) and deploy. After **3 sessions**, check `/api/table/pullback_sessions`: `universe_size` near the historical figure from Phase 0 Q19, `ticks_run` close to `ticks_expected`, `provider_degraded` false, counters non-zero and plausible, any candidates have sane `entry/stop/scale`. Do not start Phase 3 until this looks right.

---

## Phase 3 — Evening simulator and report

**Allowlist:**
- `agent/pullback_sim.py` (new)
- `tests/test_pullback_sim.py` (new)
- `agent/pullback_store.py` (add outcome functions)
- `agent/loops/evening.py`: **one** guarded call at the very end, at the line named in Phase 0 Q11
- `run.py`: subcommands `pullback-grade --date D`, `pullback-report [--from D]`, `pullback-replay --date D`

**`simulate(candidate, minute_bars, bars5_today, bars5_prev, cfg) -> dict`** is pure and implements §2.4 (chase rule, R basis) and §2.6 (exits, intra-bar order) exactly.

- Entry fill: open of the first 1-min bar at/after `decision_et`, plus the house slippage (`slippage_bps`, both sides, via the helper named in Phase 0 Q12).
- Thin bars: the same `_is_thin` rule the grader uses.
- EMA9/VWAP for trail and time-stop decisions come from the **5-min** series (same basis as detection), evaluated only on completed 5-min bars.
- `eod_r_unmanaged`: R if the full position were held from fill to the EOD flatten with only the initial stop. A reference number, never mixed into `r_total`.
- `detect_replay_match`: re-run `pullback_detect.evaluate` on the day's 5-min bars truncated at `tick_et`; true if it reproduces the stored trigger bar and levels. False is logged and counted in `pullback_sessions.grade_error`; the outcome is still stored.

**`grade_session(cfg, d, md, con)`**: grades every candidate for `d` that has no outcome at the current `SIM_VERSION`. Catch-up: also grades up to 3 prior sessions with candidates but no outcome. Budget-guarded the same way as the scan. Writes `graded`, `graded_at`.

**Evening wiring:** only if `pullback.grade_enabled` is true, inside its own `try/except`, after everything else in the evening loop has finished.

**`pullback-report`** (read-only CLI): per session and pooled (same `SIM_VERSION` + `DETECT_VERSION` only): n candidates, n valid fills, invalid by reason, mean `r_total`, mean `leg_a_r` and `leg_b_r`, scale-hit rate, time-stop rate, mean `eod_r_unmanaged`, `detect_replay_match` rate. Print the n<8-sessions warning when fewer than 8 sessions exist. Print win rate **beside** mean R, never alone.

**`pullback-replay --date D`** (diagnostic CLI, never in the evening): runs `evaluate` over the full day's bars for the whole prefiltered universe and lists triggers the live ticks **missed** (no candidate row). This turns missed ticks into a measured number instead of a mystery.

**Required tests:**
1. Golden trade: fill, scale at +1R, trail exit on the first 5-min close below EMA9 → hand-computed `leg_a_r`, `leg_b_r`, `r_total` (with and without slippage).
2. Stop before scale → both legs at stop, `r_total ≈ −1 −` friction.
3. Stop and scale in the same 1-min bar → stop wins.
4. Time stop: scale not hit and third 5-min close below `E + 0.5R` → full exit at next 1-min open. Counter-case: third close above → no time stop.
5. Trail never fires on a 5-min bar that ended **before** the scale fill.
6. EOD flatten at 15:55 on a full day and 12:55 on a half day.
7. Chase rule: first open above `E + 0.25R` → `invalid_reason = "chased"`, no R. Below stop → `"below_stop"`.
8. Worse-of-planned-and-fill R basis.
9. Replay parity: a candidate written by `pullback_scan` in a test gets `detect_replay_match = true`.
10. Second golden trade from a different bar path (gap-through stop on the open), every number hand-computed in the comment.
11. Evening wiring: `grade_session` monkeypatched to raise → the evening loop completes and its existing outputs are unchanged.

**Gate 3:** tests pass; suite equals baseline; diff matches allowlist. **Stop.**

**Production step (Jason):** set `grade_enabled: true`. After 5 sessions, run `pullback-report` and `pullback-replay` on two of those dates. Missed triggers above zero mean the tick cadence is the problem, not the strategy.

---

## Phase 4 — Live paper orders and position manager

Starts only after Jason has read Phase 3 output. Default stays `live_orders_enabled: false`.

**Allowlist:**
- `agent/pullback_live.py` (new)
- `tests/test_pullback_live.py` (new)
- `agent/pullback_store.py` (add order functions)
- `agent/live_broker.py`: **add** (never modify existing functions) `submit_oto_order(ticker, qty, side, stop_price, *, cfg)` and `submit_market_order(ticker, qty, side, *, cfg)` if Phase 0 Q4 found them missing. Both must pass the existing paper-host safety checks the other submit functions use.
- `api/index.py`: extend the guarded pullback block from Phase 2 (see tick order below)
- `agent/loops/evening.py`: extend the guarded block to reconcile `pullback_orders.live_r` (no new call site)

**Order structure per trade** (two independent entries, so each half owns its own shares):

| Leg | Order | Qty | Exits |
|---|---|---|---|
| A (scale) | `submit_bracket_order` market buy | `qty_a` | take-profit limit at scale level, stop at `S` |
| B (trail) | `submit_oto_order` market buy + stop-loss | `qty_b` | stop at `S`; trail / time stop / EOD by the manager |

**Tick order inside the guarded block:** `carry_cleanup()` (first tick of the session only), then `manage()` (existing positions), then `scan.tick()`, then `place()` for new candidates.

**Guard change for R-12:** the Phase 2 guard `if pullback.enabled and is_market_day(...)` becomes `if is_market_day(...) and (pullback.enabled or pullback_store.has_open_orders(con))`. Inside, `manage()` always runs; `scan.tick()` and `place()` still require `enabled`. `has_open_orders` is one cheap `SELECT 1 ... LIMIT 1`, and its own failure is caught inside the same `try/except`.

**`place()`** for each new candidate with no order yet, in trigger-time order then ticker order:
1. Both `live_paper.enabled` and `pullback.live_orders_enabled` must be true.
2. Caps: `max_live_orders_per_day` (counted per trade, not per leg), `max_open_positions`.
3. Ticker in use (§2.2) → skip `ticker_in_use`.
4. Latest trade price → chase rule (§2.4). Skip with the reason.
5. Equity from `GET /v2/account` → `size()`. Equity unreadable → skip `equity_unavailable` (R-4).
6. Submit leg A, then leg B. If leg B is refused after leg A was accepted: cancel leg A if unfilled; if leg A already filled, record leg B as `refused` and let leg A run on its bracket (record it, do not improvise).
7. Persist each leg immediately after the broker accepts it. Record every skip in `pullback_sessions.orders_skipped_json` with the broker's own error text (`live_broker.last_error()`).

**`manage()`** for each open leg:
1. Read the order; record entry fills.
2. Leg A: take-profit or stop filled → record exit, `live_r`.
3. Leg B: stop filled → record. Trail: once leg A's take-profit has filled, on each tick read the last **completed** 5-min bar; if `close < ema9` → cancel B's stop leg, wait for the cancel to settle (bounded retries, same waits as `live_monitor._CLOSE_RETRY_WAITS`; read the constant, do not modify `live_monitor`), then `submit_market_order(sell, qty_b)`.
4. Time stop (§2.6): exit both legs the same way (cancel legs, sell each leg's own qty).
5. EOD at `market_close_et − EOD_FLATTEN_MINUTES`: exit every open leg.
6. **Reconcile against the account:** if a leg's shares are no longer in the account position and no exit is recorded (for example, another lane's `close_position` on the same ticker took them), record `exit_reason = "closed_externally"`, `live_r = NULL`, and flag it in the session row. Never guess a price.
7. Never call `close_position`. Never sell more than a leg's own filled qty.

**`carry_cleanup()`**: first tick of a session; settles any leg from a prior session the same way `live_monitor.carry_cleanup` does for its table, but only for `pullback_orders` rows and only its own quantity.

**Required tests** (stub broker; never hit the network):
1. Placement creates exactly two orders with the right qtys, prices, and order classes; persisted rows match.
2. `live_orders_enabled: false` → no orders placed, but an open leg is still managed (R-12).
3. `enabled: false` with one open leg → `manage()` is called, `tick()` and `place()` are not.
4. Ticker in use → skipped with reason; no order call.
5. Chase and below-stop → skipped with reason.
6. Leg B refused after leg A accepted → leg A cancelled if unfilled; recorded correctly if filled.
7. Trail fires only after leg A's take-profit has filled; sells exactly `qty_b`.
8. Time stop sells each leg's own qty; never calls `close_position` (the stub fails if called).
9. EOD flatten exits all open legs; cancel-then-sell retry is bounded.
10. Closed externally → recorded, `live_r` NULL.
11. Idempotency: two ticks in a row never double-place or double-sell.
12. Wiring: `pullback_live.manage` raising does not break the monitor or any existing step (same pattern as Phase 2 test 10).
13. Live/sim parity on a scripted day: the stub broker fills exactly at the sim's prices → `live_r` equals sim `r_total` within 1e-6.

**Gate 4:** tests pass; suite equals baseline; diff matches allowlist. **Stop.**

**Production step (Jason):** enable `live_orders_enabled` with `max_live_orders_per_day: 2` for the first week. Each evening compare `pullback_orders.live_r` with `pullback_outcomes.r_total`. A difference over 0.25R on any trade is an execution anomaly to investigate before raising the cap.

---

## Phase 5 — Acceptance checklist (Jason, production)

- [ ] 5 consecutive sessions with `ticks_run ≥ 0.8 × ticks_expected` and `provider_degraded = false`.
- [ ] `pullback-replay` finds zero missed triggers on 2 sampled days (or the misses are explained by tick gaps in `pullback_sessions`).
- [ ] `detect_replay_match = true` for every graded candidate.
- [ ] Every live trade has `live_r` within 0.25R of the sim, or an explained anomaly.
- [ ] No change in the morning lane's or the monitor's outputs (compare `runs` rows and `/api/status` before and after enabling).

---

## Out of scope for v1 (addenda, not rewrites)

- **Research integration.** Adding this lane's dimensions to the weekly FDR batch changes q-values for every lane. It waits until ≥ 8 graded sessions exist, and gets its own addendum (`PULLBACK_LANE_ADDENDUM_RESEARCH_v1.md`).
- **Chart drawer overlays** for the pullback lane (EMA9/VWAP basis, zone, scale level). Addendum after Phase 3.
- **Dashboard card** on `/api/status`. The table browser covers v1.
- **2-minute bars.** Only possible with a 2-minute (or faster) trigger. Not available on the current scheduler.
- **Breakeven stop after the scale**, short side, and any change to a pre-registered threshold: each is a version bump with its own spec.
