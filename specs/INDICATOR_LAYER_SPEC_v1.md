# INDICATOR LAYER SPEC v1 — VWAP / RVOL / 9EMA

Status: implemented (2026-08-25)
Scope: `agent/indicators.py` (new), `agent/providers.py`, `agent/grading.py`,
`agent/loops/morning.py`, `agent/loops/evening.py`, `agent/delivery.py`,
`agent/mascot.py`, `prompts/morning_analyst.md`, `prompts/morning_risk.md`,
`prompts/evening_grader.md`, weekly miner stats, ledger schema, tests.

## 0. Design rules (non-negotiable)

1. **Deterministic code computes every indicator number. The LLM never derives
   one.** Same contract as `best_edge`, fills, MFE/MAE. Prompts receive
   indicator values as data and may only interpret them.
2. **No look-ahead.** Every indicator value is stamped with the time it was
   knowable. The morning bundle contains nothing observed after bundle
   generation time (~premarket). Grading fields are checkpoint-accurate
   (the `dipCountAt30m` pattern): `rvol_at_1000`, not an EOD number presented
   as if known intraday.
3. **Offline mode parity.** All indicator functions run identically on
   `synthetic_minute_bars` output. No network dependency inside
   `indicators.py`.
4. **Missing data degrades, never fabricates.** If ADV20 or premarket bars are
   unavailable for a ticker, the field is `null` and the ticker is listed in
   `bundle["missing"]` semantics (per-field), mirroring existing provider
   behavior. A null indicator is honest signal, same as an `unmeasurable`
   rejection.

## 1. `agent/indicators.py` (new module) — IMPLEMENTED

Pure functions over the existing bar dict shape
`{"t": datetime, "o","h","l","c","v"}`. No I/O, no config, no clock reads.

```python
def vwap_series(bars: list[dict]) -> list[float]:
    """Session-anchored cumulative VWAP per bar.
    Typical price (h+l+c)/3 weighted by v. Anchor = first bar given
    (caller decides premarket-inclusive vs 9:30-anchored)."""

def ema_series(closes: list[float], period: int) -> list[float]:
    """Standard EMA, seeded with SMA of the first `period` values.
    Values before the seed index are None (not enough data ≠ a number)."""

def rvol_simple(cum_volume: int, adv20: float) -> float | None:
    """v1 RVOL: cumulative session (or premarket) volume ÷ 20-day average
    DAILY volume. Honest label: 'pct_of_adv20', not time-of-day RVOL."""

def vwap_events(bars: list[dict], vwap: list[float]) -> dict:
    """{'first_reject_et': ..., 'first_reclaim_et': ...,
        'pct_time_above': 0.0-1.0, 'crosses': int}
    A cross counts only on bar CLOSE through VWAP (wick-touch is noise)."""

def vwap_distance(price: float, vwap_value: float) -> float:
    """Signed % distance of price from VWAP; positive = above."""

def ema_loss_time(bars, ema, direction) -> datetime | None:
    """First bar-close through the EMA against the position's direction."""
```

Tests: `tests/test_indicators.py` — hand-computed fixtures (5-bar VWAP by
hand, EMA seed math, reclaim/reject on a constructed tape).

### v2 (explicitly out of scope now)
Time-of-day RVOL (cum vol at t ÷ 20-session average cum vol at same t).
Requires a per-symbol minute-volume profile cache in the ledger DB
(`volume_profile(symbol, minute_of_day, avg_vol, n_days, updated)`),
back-filled lazily from Alpaca minute history. Do not build until v1 fields
prove themselves in the ledger.

## 2. Providers / morning bundle — IMPLEMENTED

`MarketData` additions:

- `adv20(symbols: list[str]) -> dict[str, float]` — 20-day average daily
  volume. Alpaca `multi_daily_bars` batched; yfinance (`yf_history_volumes`)
  fallback; offline mode returns a deterministic synthetic value.
- Gap-scan + catalyst-quote enrichment (`MarketData._enrich_indicators`): for
  each row in `premarket_gappers` and `catalyst_quotes`, attach when
  fetchable: `pm_vwap`, `vs_pm_vwap`, `pm_vol_pct_adv20`, `adv20`. One batched
  premarket-bars fetch (`Alpaca.multi_bars`, 1Min, 04:00→now) for every
  ticker in a single call, plus one batched `adv20()` call. Nothing after
  bundle generation time enters this — no look-ahead.
- `render_bundle_md`: gap-scan and catalyst-quote lines carry the suffix
  `+0.6% vs pmVWAP, pm vol 18% ADV20` (or `n/a` per-field when unavailable).

Config (`data_cfg`): `indicator_enrich: true` (default on),
`gap_scan_price_band: [null, null]` — optional `[min, max]` last-price filter
on the gap scan (INDEX_ETFS are exempt). Default off; recommendation stands
to leave it off and let the ledger argue via the shadow ledger before
hard-filtering the scan universe.

## 3. Prompt changes — IMPLEMENTED

`prompts/morning_analyst.md` (bumped to v5): new hard-constraint bullet
defining `vs pmVWAP` / `pm vol %ADV20`, instructing the analyst to anchor
theses/invalidations on VWAP for momentum/continuation setups and to justify
low-participation entries.

`prompts/morning_risk.md` (bumped to v6): kill-check 13, Participation
(RVOL) — kill or demand justification for candidates without elevated
`pm vol %ADV20`; unverified participation is treated the same as an
unverified price quote.

`prompts/evening_grader.md` (bumped to v4): new paragraph requiring any
VWAP/RVOL/9EMA-referencing lesson to cite the mechanically computed numbers
supplied in the outcomes block, never a recollection.

No prompt generates signals. Prompts interpret computed values. Only.

## 4. Grading enrichment (`grading.py` + outcomes schema) — IMPLEMENTED

Per graded pick (`simulate_pick`, now takes `adv20: float | None = None`),
computed from the same minute bars already fetched, stored on the outcome
row:

- `vwap_at_fill`, `fill_vs_vwap_pct` (signed; from the fill bar),
  `vwap_side_at_fill` ("above"/"below") — fill-dependent, triggered picks only
- `first_vwap_reclaim_et`, `first_vwap_reject_et`, `vwap_crosses`,
  `pct_time_above_vwap` — session-level (9:30-anchored VWAP over the full
  bar series), computed regardless of trigger status
- `rvol_at_1000`, `rvol_at_1200`, `rvol_close` (pct-of-ADV20, via
  `_clock_to_bar_index` checkpoint mapping; null if ADV20 unavailable or the
  bars don't reach that checkpoint)
- `ema9_5m_loss_et` — first 5-minute close through the 9 EMA against the
  trade's direction, evaluated from the fill's 5-minute bucket onward
  (`_resample_5m` + `agent.indicators.ema_loss_time`); null if never lost
- `adv20`

`grade_day` takes `adv20_map: dict[str, float] | None = None`; the evening
loop fetches it once via `md.adv20([...])` before grading. `outcomes_md`
renders an `Indicators:` line per triggered pick for the grader prompt.

Ledger: 12 new columns on `outcomes` (idempotent `ADD COLUMN IF NOT EXISTS`
migrations). These fields are grader INPUT and miner FUEL. They do not gate
fills or change R math.

Shadow-ledger (`rejections`) rows are NOT enriched in v1 — out of scope; the
shared `simulate_pick` still computes the fields internally for rejections
but nothing persists them, keeping the schema change contained to `outcomes`.

## 5. Weekly miner / stats dimensions — IMPLEMENTED

`agent/ledger.py`: `stats_by` refactored to share its aggregation body
(`_stats_rows`) with a new `stats_by_outcome(con, dim)`, which groups by an
OUTCOMES-table dimension instead of a picks column:

- `rvol_bucket` — buckets `rvol_at_1000` into `<1x`, `1-2x`, `>2x` (or `?`
  when null)
- `vwap_side_at_fill` — `above` / `below` / `?`

`build_weekly_memory` exposes `by_rvol_bucket` / `by_vwap_side`; the weekly
prompt renders both via the existing `_fmt_stats` helper. The miner may
promote rules from these exactly as it promotes any other rule.

Two new open questions logged by the evening loop (`Q_RVOL_SEPARATION=7`,
`Q_VWAP_REVERSAL_EDGE=8`): one increment per triggered pick with a non-null
`rvol_at_1000`, and one per long pick whose thesis text cites VWAP.

## 6. Arby / dashboard — PARTIAL

Chart overlays (client-side, zero payload change): `setChartData` already
receives real minute bars with `v` when available. VWAP + 9EMA polylines are
drawn in `drawChart` when volume data is present.

Not implemented in v1: the RVOL HUD pill (`adv20` is not yet threaded through
`/api/status` / `delivery.py`'s mascot boot payload — would need a batched
`MarketData.adv20()` call in the dashboard render path, mirroring how
`_pick_bars` is fetched in `api/index.py`). Left as a follow-up; the chart
overlays and quip pools ship without it.

## 7. Rollout order (each step shippable alone) — followed as spec'd

1. `indicators.py` + tests. Pure code, no behavior change.
2. Grading enrichment (§4). Evidence starts accumulating immediately;
   grader prompt bump rides along.
3. Bundle enrichment + morning prompt bumps (§2, §3).
4. Miner dimensions + open questions (§5).
5. Arby overlays (§6, partial).

## 8. Open questions (decide before or during implementation)

- Price-band filter: hard-filter the gap scan to $10–$50 (`gap_scan_price_band`)
  or keep full scan and let prompts weight the band? Shipped as config-on,
  default OFF, per the recommendation — let a few weeks of ledger data argue.
- Premarket bars coverage on the IEX feed may be thin for some names —
  measure the null-rate of `pm_vwap` in week 1 before trusting the analyst
  rule that leans on it.
- 9EMA timeframe: 5m chosen for grading (intraday desk, 1m too noisy for a
  process metric). Revisit if time_stops move earlier.
- ADV20 source of truth: Alpaca daily bars vs yfinance mismatch — currently
  Alpaca-first with yfinance filling gaps; no divergence logging yet.

## 9. Failure handling — IMPLEMENTED

Indicator computation never fails a loop: every computation site
(`providers._enrich_indicators`, `grading._indicator_fields`, the
fill-dependent block in `simulate_pick`) is wrapped so an exception leaves the
affected fields `null` and logs a warning rather than raising. A grading run
with all-null indicators still grades (fields are additive).
