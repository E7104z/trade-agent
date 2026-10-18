# PULLBACK LANE — PHASE 0 REPORT

Written 2026-10-10 (Saturday) by Claude Code, read-only. No code was changed in Phase 0.
Spec: `specs/PULLBACK_LANE_SPEC_v1.md`.

## CONFLICTS (read first)

**C-1 (blocking for Phase 2): the spec's `build_universe(md, con, d, cfg)` is not what the runner had.**
The runner's `build_universe(md, cfg, *, at, con=None)` is a different function: it unions the liquid list with `DEFAULT_UNIVERSE` (1,095 names), `most_actives` and `movers`, then filters through `runner_scan.EXCLUDED_TICKERS` / `LANE_EXCLUDED_TICKERS`, with sticky candidates and runner stats. It imports `runner_default_universe` and `runner_scan`, both deleted. The piece the spec describes (tradable assets → previous-session dollar-volume floor → ETF/fund exclusion, cached) is two other functions in the same file: `liquid_universe(md, con, d, *, floor_usd, force)` and `instrument_classes(md, con, d, *, force)`, plus `below_liquidity_floor`. Their cache goes through `ledger.get_runner_universe_cache` / `save_runner_universe_cache`, which were renamed to `get_universe_cache` / `save_universe_cache` (the table is still named `runner_universe_cache`, kept for the gap field). The spec's own port rules allow only three substitutions; this needs a fourth (drop `build_universe`'s source union, keep `liquid_universe` + `instrument_classes`). **Jason to confirm: port `liquid_universe` + `instrument_classes` + `below_liquidity_floor` as the lane's `build_universe`.**

**C-2: `agent/gapfield.py` already has a copy of `instrument_classes`** (line 281, uses `ledger.get_universe_cache`). Not a conflict with the denylist, but Phase 2 could reuse the pattern. Spec §5's allowed-imports list does not include `agent.gapfield`, so the lane would still copy it.

**C-3 (Q5): there is no per-symbol, time-windowed news function.** `Alpaca.news(self, limit=40)` (`agent/providers.py:326`) calls `/v1beta1/news` with only `limit` and `sort=desc`. No symbols, no start/end. `MarketData._news` wraps it. The §2.2 catalyst gate ("news count in [prior session 16:00, trigger bar end]" for one ticker) cannot be done with it. Options: (a) add a `symbols/start/end` method to `Alpaca` (touches `providers.py`, not on any phase allowlist), (b) record `catalyst_count = NULL` in v1. **Jason to decide.** `catalyst_gate` defaults to "record", so (b) loses nothing that gates.

**C-4 (Q12): there is no slippage helper.** Slippage is inline arithmetic in `grading.simulate_pick` (`agent/grading.py:699`, `slip = slippage_bps/10000`, fill × (1±slip)). Spec Phase 3 says "via the helper named in Phase 0 Q12". The sim will have to apply the same arithmetic itself (both sides). Only `_is_thin` and `EOD_FLATTEN_MINUTES` are importable.

**C-5 (Q4): `live_broker` has neither an OTO order nor a plain market-qty order.** Phase 4 adds both (already planned). Existing relevant functions: `submit_stop_entry_order`, `submit_oco_exit`, `close_position`.

**C-6 (Q14): no existing test proves a failing step leaves `/cron/intraday-check` working.** `tests/test_live_route.py` has only a missing-bearer test (line 7) and a disabled-noop test (line 20). Phase 2 test 10 can't "mirror" one; it has to be written from the pattern of the disabled-noop test.

**C-7 (Q13): no already-held check at placement.** See Q13. The spec's `ticker_in_use` gate for the lane is new behaviour; the morning lane can place an order on a ticker the pullback lane holds, and the reverse. R-13 (own shares only) is the only protection against `close_position` from the other lane.

**C-8 (precondition 1): the removal commit is not "on its own".** `c8ede6d` also adds `research/intraday/` (REGISTER.md, context.py, etfcache.py, fetch.py, lab.py, noise.py; about 1,300 lines) and modifies `agent/webui.py`. The earlier note that the `webui.py` trade-card edits would be a separate commit did not happen. Not harmful, but if a regression appears, those are in the same deploy.

**C-9 (Rules for Cline, denylist): `webui.py` is at `agent/webui.py`**, not the repo root. The denylist entry should read `agent/webui.py`. `agent/instruments.py` is on the denylist and `fund_from_profile` still lives there, so nothing needs porting (Q7).

**C-10: warm tick did nothing.** The deploy's post-deploy tick (`scripts\intraday_cron.ps1 -Once`) ran on a Saturday and returned `not a market day`, so the schema warm-up and `carry_cleanup` did not run. The first real warm is the first Monday tick. Precondition 2 (a full session on the new code) is therefore still open, as already planned.

## Preconditions (Q1)

- Removal commit: `c8ede6d85e2629146bbe13dc5b0f0350cc72ae87` ("Remove the runner lane entirely"), 104 files, +1,816 / −13,942.
- Docs commit on top: `6c774d1b6349a643c2da847a0adf4d051cd55b7b`. HEAD = `6c774d1`, branch `master` (the spec's `feat/pullback-lane` is not cut yet).
- Deployed 2026-10-10 evening via `scripts\deploy.ps1`: rehearsal PASS (`reports/rehearsals/6c774d1b6349-20261010-204552`), full suite passed, `git push` ok, `vercel --prod` ok (`dpl_8G3fsXQe3o1Df1HY8aWEHLLMajyb`, READY, aliased to https://iwilltrade.com). Production now runs `6c774d1` (code identical to `c8ede6d`).
- `git status` clean; `git log origin/master..master` empty.
- Four scratch files (`_phase0_check.py`, `_probe.py`, `_grep_r_multiple*.txt`) were deleted. The only mention of them is `specs/PERSONA_OVERHAUL_REPORT.md:14`.
- The first rehearsal attempt failed on a too-low Anthropic credit balance; it passed after the top-up.

## Code facts

### Q2 Migrations
`agent/ledger.py` ends with (lines 431–444): `migrations_v32`, `migrations_v33`, `migrations_v34`, `migrations_v35`, each as
```
from . import migrations_v35  # noqa: E402
_MIGRATIONS = _MIGRATIONS + migrations_v35.STATEMENTS
```
Last three imports: v33 (L435), v34 (L439), v35 (L443). Files on disk: `migrations_v30`–`v35`. **Next free number: 36** → `agent/migrations_v36.py`. The runner removal added no migration.

### Q3 Signatures
- `indicators.ema_series(closes: list[float], period: int) -> list[float | None]` (L50). SMA-seeded; `None` before index `period-1`; returns all-None when `len(closes) < period`.
- `indicators.vwap_series(bars: list[dict]) -> list[float]` (L29). Bars `{"t","o","h","l","c","v"}`; cumulative typical price × volume anchored at `bars[0]`; zero-volume bars carry the prior value forward.
- `live_broker.quantity_for_r(size_r: float, dollars_per_r: float, entry: float, stop: float) -> int` (L108) = `floor(size_r*dollars_per_r/|entry-stop|)`, 0 if risk ≤ 0.
- `live_broker.submit_bracket_order(ticker, qty, side, entry_type, entry_price, stop_price, target_price, *, cfg=None) -> dict | None` (L115). `qty<1` → None. `entry_type` "open" → market, "limit", "breakout" → stop-entry bracket. Payload `order_class: bracket`, `time_in_force: day`, take_profit/stop_loss legs.
- `live_broker.list_positions(*, cfg=None) -> list[dict] | None` (L199). **None = account unreadable, not "holds nothing".**
- `live_broker.recent_orders(limit=50, *, cfg=None) -> list[dict]` (L211); `status=all&direction=desc&nested=true`; `[]` on failure.
- `live_broker.cancel_order(order_id, *, cfg=None) -> bool` (L180).
- `live_broker.get_order(order_id, *, cfg=None) -> dict | None` (L184).
- `live_broker._request(method, path, *, cfg=None, payload=None) -> dict | list | None` (L56). Fail-soft: returns None on API/network failure, reason kept in `last_error()`.
- `live_broker.last_error() -> str | None` (L26) returns module-global `_last_error`. Also `auth_rejected() -> bool` (L30).
- Also present: `get_account(*, cfg=None) -> dict | None` (L206), `list_open_orders`, `close_position(ticker)` (L194; the lane must never call it), `submit_stop_entry_order`, `submit_oco_exit`, `tick_price`.

### Q4 OTO / market sell
Neither exists (see C-5). `live_broker` has `submit_bracket_order`, `submit_stop_entry_order` (L151), `submit_oco_exit` (L165), `cancel_order`, `close_position`. Phase 4 adds `submit_oto_order` and `submit_market_order`.

### Q5 News
See C-3. `Alpaca.news(self, limit: int = 40) -> list[dict]` (`providers.py:326`) returns `data.get("news", [])`; items carry `headline`, `symbols`, `source`, `created_at`. No start/end/symbol arguments. `MarketData._news()` (L1046) wraps it as `{"headline","symbols","source","at"}`.

### Q6 Alpaca data methods (all four still exist)
- `Alpaca.multi_bars(self, symbols, timeframe, start: datetime, end: datetime, *, feed=None, adjustment="raw", workers=1) -> dict[str, list[dict]]` (L410). Pages `next_page_token`, batches of 100. `t` is **tz-aware, converted to ET** (`datetime.fromisoformat(...).astimezone(ET)`), and it is Alpaca's **bar start** time. Every requested symbol is present as a key (empty list if no bars).
- `Alpaca.multi_daily_bars(self, symbols, start: date, end: date) -> dict[str, list[dict]]` (L492); split-adjusted; rows `{"close","volume"}`.
- `Alpaca.tradable_assets(self) -> list[str]` (L255) and `asset_records()` (L272).
- `Alpaca.snapshots(self, symbols) -> dict[str, dict]` (L235).
- Also `most_actives(top, by)` (L320), `movers(top)`, `is_common_stock_symbol` (module level, L153), `Alpaca._clamp_end` (SIP recent-data clamp, off on this plan).
No provider method was removed.

### Q7 Instruments
`agent/instruments.py` exists. `fund_from_profile(quote_type: str | None, industry: str | None, float_shares: float | None) -> str | None` (L105): non-EQUITY quoteType → reason; "Asset Management" with no float → closed-end fund; null quoteType decides nothing. Also `classify(name) -> str` (L85) and `COMMON = "common"` (L36). I did not find the `md` call that supplies a yfinance profile within the time spent; Phase 2 needs to locate it (`grep -n "quote_type\|float_shares" agent/*.py`). Nothing to port.

### Q8 Ported sources
Both files were deleted by `c8ede6d`; sources are at `c8ede6d^`.
- `runner_universe.py` imports: `from . import instruments, ledger` (still exist; ledger cache functions renamed, see C-1), `from .providers import is_common_stock_symbol` (still exists), `from .runner_default_universe import DEFAULT_UNIVERSE` (**removed**), `from .runner_scan import EXCLUDED_TICKERS, LANE_EXCLUDED_TICKERS` (**removed**), `from .util import log, prev_market_day` (exist). Needed for the lane: `LIQUIDITY_FLOOR_USD = 5_000_000`, `below_liquidity_floor`, `instrument_classes`, `liquid_universe` (full text retrievable with `git show c8ede6d^:agent/runner_universe.py | sed -n 49,330p`; functions at lines 73, 83, 131, 226). `liquid_universe` logic: `alp.tradable_assets()` → `prev_market_day(d)` → temporarily force `alp.feed = "sip"` → `alp.multi_daily_bars(assets, pd_, d)` (fallback to the original feed) → `prev[sym] = {"close","volume"}` from `rows[0]`, `liquid` = symbols with `volume*close >= floor_usd`, sorted; cached as `{"liquid": [...], "prev": {sym: [close, volume]}}`. Note `rows[0]` is the first of the returned daily bars (start = previous session).
- `runner_indicators.py` imports: `from decimal import ROUND_HALF_UP, Decimal` (stdlib, not needed by `expected_volume_fraction`). Needed: `INTRADAY_VOL_CURVE` (19 points, 5 → 0.08 … 390 → 1.00) and `expected_volume_fraction(minutes_in)` (L222): `None` if `minutes_in` is None or ≤ 0; piecewise-linear interpolation between curve points; first segment is `0.08 * minutes_in/5`; past 390 stays 1.0; clamped to [0.01, 1.0]. `SESSION_OPEN_ET_MINUTES = 570`.
- Deleting commit for both: `c8ede6d85e2629146bbe13dc5b0f0350cc72ae87`.

### Q9 `expected_volume_fraction` values (computed by loading the pre-removal source)
| minutes_in | value |
|---|---|
| 5 | 0.08 |
| 15 | 0.17 |
| 60 | 0.35 |
| 195 | 0.59 |
| 385 | 0.9833333333333334 |
| 390 | 1.0 |

Phase 1 test 10 should pin 195 with an approximate comparison (0.57 + 0.5×0.04) and 385 as 0.9833333333333334.

### Q10 `/cron/intraday-check` (`api/index.py:737`)
Handler order: (1) `_is_intraday_trigger()` auth, 401 if not; (2) `cfg = load_config()`; (3) `monitor = check(cfg)` unguarded; (4) `result = dict(monitor); result["monitor"] = monitor`; (5) if `live_paper.enabled` and market day: guarded `carry_cleanup` → `result["carry"]`, guarded `place_deferred_orders` → `result["deferred"]`; (6) guarded Alpaca-auth alert; (7) guarded heartbeat `ledger.beat(con, "intraday_check", ...)`; (8) `return json_response(result)`.
**Pullback call goes after the deferred-orders block and the auth alert (after L~790, `except Exception as e: log("intraday", f"WARN credential alert failed...`) and before the `# Heartbeat:` comment (L~795).** Placed there it comes after every existing step but before the heartbeat, so a pullback tick that hangs would delay the monitor's proof-of-life. **Recommend instead placing it after the heartbeat block and just before `return json_response(result)`.**
**No request-start time is captured.** Phase 2 must add `time.monotonic()` as the handler's first line (the spec's fallback).
The handler also gates the whole block on `live_paper.enabled`; the pullback block as specced is independent of that, which is intended.

### Q11 Evening loop end (`agent/loops/evening.py`)
`run(cfg, run_date=None, force=False)` (L652). The success path ends at L1089 `render_dashboard(con, cfg, d)` then L1090 `ledger.finish_run_lock(con, d, "evening", lock["request_id"], "complete")`, inside the big `try:`; `except Exception` logs the run as failed and re-raises; `finally: con.close()`. Budget: module constants `FUNCTION_BUDGET_S = 300`, `BUDGET_WARN_FRACTION = 0.70` (L37–39); the loop tracks `t_start` and passes `deadline=t_start + FUNCTION_BUDGET_S - reserve` to optional steps (see the trader's log block, L1047–1055). **The guarded pullback call goes immediately after L1090**, in its own `try/except`, using `deadline = t_start + FUNCTION_BUDGET_S - reserve_s`. Because it sits after `finish_run_lock`, a pullback failure cannot change the run's recorded status.

### Q12 Grading helpers
- `grading.EOD_FLATTEN_MINUTES = 5` (L143).
- `grading._is_thin(bar: dict, min_bar_volume: float) -> bool` (L276); disabled when `min_bar_volume` is falsy; a bar with no `v` is never thin.
- Slippage: no helper (C-4). Inline at `grading.py:699` inside `simulate_pick(..., slippage_bps: float = 0.0, flatten_at=None)` (signature near L445); entry fill ×(1+slip) long / ×(1−slip) short. `slippage_bps` default 5 per side comes from config.

### Q13 Held-ticker check
No check at placement. `live_monitor.place_deferred_orders` and the morning lane (`loops/morning.py`) do not call `list_positions` before submitting; the only `list_positions` use in `live_monitor.py` is in `carry_cleanup` (L328–331, `held = {symbol for p in positions}`), to settle stale rows. The morning lane's `_same_bet(pick, held, pairs)` (L1330) compares a pick against other picks (correlation), not against account positions.

### Q14 Test conventions
Temp ledger fixture pattern (`tests/test_live_monitor.py:12`):
```python
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "live-monitor.duckdb")
    return Config(raw={"live_paper": {"enabled": True, "alpaca_paper": True,
        "trading_base": live_broker.PAPER_BASE, "breakout_two_step": True, "flatten_at": None},
        "flatten_at": None}, alpaca_key_id="key", alpaca_secret_key="secret")
```
Route tests (`tests/test_live_route.py`): `_tmp_ledger(monkeypatch, tmp_path)`, `monkeypatch.setattr(index, "CRON_SECRET", "test-secret")`, `monkeypatch.setattr(index, "load_config", lambda: Config(raw={...}))`, then `index.app.test_client().post("/cron/intraday-check", headers={"Authorization": "Bearer test-secret"})`. No test of a failing guarded step (C-6).

### Q15 `util`
`ET = ZoneInfo("America/New_York")` (L14); `now_et() -> datetime` (L45, tz-aware); `today_et() -> date` (L49); `is_market_day(d: date | None = None) -> bool` (L53, weekday and not in `MARKET_HOLIDAYS`); `is_half_day(d=None) -> bool` (L58); `market_close_et(d=None) -> dtime` (L63, 13:00 on a half day else 16:00); `prev_market_day(d=None) -> date` (L71). All exist. `parse_date`, `log(loop, msg)` also exist.

### Q16 Removal commit `c8ede6d`
**Deleted (27):** `agent/loops/runner.py`, `agent/runner_default_universe.py`, `runner_entry.py`, `runner_exits.py`, `runner_indicators.py`, `runner_regate.py`, `runner_research.py`, `runner_scan.py`, `runner_selection.py`, `runner_universe.py`, `runner_view.py`; `mocks/runner_selector.json`; `prompts/runner_selector.md`; `scripts/backfill_change_pct.py`, `runner_parity.py`, `runner_parity_fixtures.py`, `runner_parity_golden.mjs`, `setup_runner_task.ps1`; `tests/fixtures/prompt_renders/runner_selector.md`; `tests/test_evening_mean_r.py`, `test_runner_exits.py`, `test_runner_parity.py`, `test_runner_phases.py`, `test_runner_playbook_loop.py`, `test_runner_pool_integrity.py`, `test_runner_research.py`, `test_runner_trade_construction.py`, `test_runner_view.py`, `test_weekly_writes_runner_section.py`.
**Added (6):** `research/intraday/REGISTER.md`, `context.py`, `etfcache.py`, `fetch.py`, `lab.py`, `noise.py` (unrelated to the runner; see C-8).
**Modified (shared files, 47):** `.vercelignore`, `README.md`, `agent/backlog.py`, `buckets.py`, `chart_payload.py`, `clusters.py`, `config.py`, `coverage.py`, `delivery.py`, `gapfield.py`, `grading.py`, `grading_context.py`, `grounding.py`, `health.py`, `instruments.py`, `integrity.py`, `ledger.py`, `llm.py`, `loops/evening.py`, `loops/weekly.py`, `persona_monitor.py`, `playbook_enforcement.py`, `rehearsal.py`, `research.py`, `selfcheck.py`, `sequential.py`, `structural.py`, `traders_log.py`, `webui.py` (`agent/webui.py`), `api/index.py`, `config.json`, `mocks/traders_log*.json`, `prompts/evening_grader.md`, `persona.md`, `traders_log.md`, `weekly_miner.md`, `run.py`, `scripts/capture_prompt_renders.py`, `db_preflight.py`, `intraday_cron.ps1`, `vercel.json`, and 17 existing tests (`test_alpaca_credentials`, `test_chart_payload`, `test_dashboard_payload`, `test_evening_review_fixes`, `test_failure_visibility`, `test_forward_validation`, `test_grading`, `test_integrity_repair`, `test_live_route`, `test_loop_hardening`, `test_paper_trading`, `test_persona*`, `test_playbook_audit`, `test_selfcheck`, `test_sequential`, `test_structural_mining`, `test_traders_log_*`, `test_verification_practice`, `test_webui_ticker_links`, `test_write_time_enforcement`).
Shared files most relevant to later phases that changed heavily: `agent/ledger.py` (−434), `api/index.py` (−182), `agent/loops/evening.py` (−417), `run.py` (−302), `scripts/db_preflight.py`, `agent/config.py`, `config.json`.
Also: `vercel.json` lost 8 lines (the runner crons), `scripts/intraday_cron.ps1` changed by 5 lines.

## Production facts (Q17–Q19)

**Q17: PENDING** (no production access; answer after the 2026-10-12 session).
**Q18: PENDING** (same). Context for whoever fills it: `/cron/intraday-check` is hit by the local task `scripts\intraday_cron.ps1` every 5 minutes (memory note); the Vercel crons fire later, so the tick must come from the local machine being on at 09:35 ET. Check heartbeat `intraday_check` and `pullback`-relevant first-tick times for the last five sessions.
**Q19: PENDING** (same). `runner_sessions.universe_size` should be ~4,000+ per the spec; the removal left the tables in place.

## Baseline (Q20)
`.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` on `6c774d1`: **1,232 passed, 0 failed, 0 skipped, 0 errors** (matches the expected 1,232). The deploy script's own full-suite run also passed.

## Gate 0 checklist
- ✅ Report written; Q1–Q16 and Q20 answered.
- ⚠️ Q17–Q19 PENDING by Jason's instruction.
- ✅ CONFLICTS section present (10 entries). **C-1 and C-3 need Jason's decision before Phase 2**; C-4 and C-6 change what Phases 2–3 can "mirror"; the rest are notes.
- ⚠️ Q7 sub-question (which `md` call supplies a yfinance profile) not located; Phase 2 must find it.
- Phase 1 stays blocked until the 2026-10-12 morning, intraday and evening runs have completed and Q17–Q19 are filled in.
