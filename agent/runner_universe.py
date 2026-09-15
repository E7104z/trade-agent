"""
Runner universe construction.

DTO has TWO populations, and this reproduces the larger one:

  the 3-minute watching loop   DEFAULT_UNIVERSE(1,095) u actives u gainers
                               ~1,190 symbols
  the once-a-day main scanner  getSharedLiquidUniverse() - /v2/assets screened
                               on dollar volume, ~2-5k symbols, unioned with
                               DEFAULT_UNIVERSE

DTO's own note (js/runner-scanner.js:992) says the liquid universe "is correct
for the once-a-day main scanner and backtest" and is excluded from the watching
loop only because it "turns every scan into a 60-90s bar-fetch marathon".

Since this lane makes ONE scan-and-pick decision a day, the main scanner's
population is the right one, and the 60-90s cost is paid once per tick with the
expensive part cached per session.

── THE BUG THIS FILE USED TO HAVE ───────────────────────────────────────────
The first version pulled movers + most-actives only, capped at 150, and landed
on 84 symbols. Detection had been proven correct against DTO's JavaScript to
the last decimal, so nothing looked broken - but the population was ~50x too
small, candidates came in at ~1.6 a session against a real desk's ~50, and that
emptiness read as a quiet market rather than a wrong universe.

Measured on 2026-09-11 as the population was corrected:
      84 symbols  ->  1 candidate
   1,194 symbols  ->  2 candidates
   4,282 symbols  ->  7 candidates

Three guards against a silent repeat:
  - a universe below EXPECTED_MIN_UNIVERSE is recorded as degraded, so a thin
    candidate list can never be mistaken for a genuine stand-down
  - `universe_size` is written to runner_sessions every tick
  - tests/test_runner_phases.py asserts the standing list survives, that the
    degraded flag fires, and that the cap cannot truncate the real population
    (a 1,500 cap against 4,224 symbols cost 8 of 11 candidates)
"""
from __future__ import annotations

from datetime import datetime

from . import ledger
from .providers import is_common_stock_symbol
from .runner_default_universe import DEFAULT_UNIVERSE
from .runner_scan import EXCLUDED_TICKERS
from .util import log, prev_market_day

__all__ = ["build_universe", "liquid_universe", "EXPECTED_MIN_UNIVERSE",
           "LIQUIDITY_FLOOR_USD"]

# A healthy session unions ~1,095 standing names with the live screeners. If the
# population collapses far below that, a provider is down or a filter has gone
# wrong - either way it is an outage, not a market condition, and the session
# must be recorded as degraded so a thin candidate list is never mistaken for a
# genuine stand-down.
EXPECTED_MIN_UNIVERSE = 800


LIQUIDITY_FLOOR_USD = 5_000_000


def liquid_universe(md, con, d, *, floor_usd: float = LIQUIDITY_FLOOR_USD,
                    force: bool = False) -> tuple[list[str], dict]:
    """The liquid tradable universe for a session, plus prev close/volume.

    Returns (symbols, prev) where prev maps SYM -> {"close": c, "volume": v}.

    DTO calls this `getSharedLiquidUniverse()` and its own comment says it is
    "correct for the once-a-day main scanner and backtest" while being too slow
    for the 3-minute watching loop. The first port of this lane only reproduced
    the watching loop's universe and therefore scanned a small fraction of what
    the real desk screens.

    Measured 2026-09-15 on 2026-09-11 data:
        /v2/assets                     12,890
        after the tradable filter       7,182
        after a $5M/day dollar floor    4,068
        + DEFAULT_UNIVERSE, minus ETFs  4,224 scanned
        candidates at 10:30                11   (was 2 on the 1,190 universe)

    The daily-bar fetch that powers the dollar-volume floor takes ~77s, and it
    is the same answer all session, so the result is cached per session date in
    runner_universe_cache. That fetch also yields prev_close and prev_volume for
    every symbol - which RVOL needs anyway - so caching it removes a 77-second
    step from every subsequent tick.
    """
    if not force:
        cached = ledger.get_runner_universe_cache(con, d)
        if cached:
            payload = cached["payload"]
            prev = {k: {"close": v[0], "volume": v[1]}
                    for k, v in (payload.get("prev") or {}).items()}
            return list(payload.get("liquid") or []), prev

    alp = getattr(md, "alpaca", None)
    if alp is None:
        return [], {}

    assets = alp.tradable_assets()
    if not assets:
        log("runner", "WARN tradable asset list empty - liquid universe unavailable")
        return [], {}

    pd_ = prev_market_day(d)
    try:
        daily = alp.multi_daily_bars(assets, pd_, d)
    except Exception as e:
        log("runner", f"WARN daily-bar liquidity screen failed: {e}")
        return [], {}

    liquid: list[str] = []
    prev: dict[str, dict] = {}
    for sym, rows in (daily or {}).items():
        if not rows:
            continue
        close = rows[0].get("close") or 0
        volume = rows[0].get("volume") or 0
        prev[sym] = {"close": close, "volume": volume}
        # Dollar volume, not share volume: a $2 stock trading 3M shares is not
        # the same liquidity as a $200 stock trading 3M shares, and the runner
        # needs enough depth to fill and exit inside the session.
        if volume * close >= floor_usd:
            liquid.append(sym)

    liquid.sort()
    try:
        ledger.save_runner_universe_cache(
            con, d,
            {"liquid": liquid,
             "prev": {k: [v["close"], v["volume"]] for k, v in prev.items()}},
            source=f"assets={len(assets)} floor={floor_usd:.0f}")
    except Exception as e:
        log("runner", f"WARN could not cache liquid universe: {e}")

    log("runner", f"liquid universe built: {len(liquid)} of {len(assets)} tradable "
                  f"assets cleared the ${floor_usd/1e6:.0f}M/day floor")
    return liquid, prev


def build_universe(md, cfg: dict, *, at: datetime,
                   con=None) -> tuple[list[str], dict]:
    """Return (symbols, stats) for one scan tick.

    `md` is an agent.providers.MarketData, `cfg` the `runner` config block,
    `con` an open ledger connection (optional; used for sticky candidates).
    """
    price_min = float(cfg.get("price_min", 5.0))
    price_max = float(cfg.get("price_max", 50.0))
    cap = int(cfg.get("universe_size", 1500))

    sources = {"liquid": 0, "default": 0, "most_actives": 0, "movers": 0}
    raw: list[str] = []
    prev_daily: dict[str, dict] = {}

    # 0. The liquid tradable universe - the bulk of the population, and the
    #    thing whose absence made the first version of this lane scan 84
    #    symbols instead of ~4,200. Cached per session; see liquid_universe().
    if con is not None:
        try:
            liquid, prev_daily = liquid_universe(md, con, at.date())
            for s_ in liquid:
                raw.append(s_)
                sources["liquid"] += 1
        except Exception as e:
            log("runner", f"WARN liquid universe unavailable, falling back to the "
                          f"standing list + screeners: {e}")

    # 1. The standing list. This is the bulk of the population and needs no
    #    network call, so a screener outage degrades the universe rather than
    #    emptying it.
    for s in DEFAULT_UNIVERSE:
        raw.append(s)
        sources["default"] += 1

    alp = getattr(md, "alpaca", None)
    if alp is not None:
        # 2. Most-actives, 100 deep (DTO uses 100 by trades and 100 by volume;
        #    Alpaca's screener exposes one `by` at a time, so both are pulled
        #    and unioned).
        for by in ("volume", "trades"):
            try:
                data = alp.most_actives(top=100, by=by) or {}
                for row in data.get("most_actives", []) or []:
                    sym = (row.get("symbol") or "").upper()
                    if sym:
                        raw.append(sym)
                        sources["most_actives"] += 1
            except Exception as e:
                log("runner", f"WARN most-actives({by}) fetch failed: {e}")
        # 3. Top movers.
        try:
            movers = alp.movers(top=50) or {}
            for side in ("gainers", "losers"):
                for row in movers.get(side, []) or []:
                    sym = (row.get("symbol") or "").upper()
                    if sym:
                        raw.append(sym)
                        sources["movers"] += 1
        except Exception as e:
            log("runner", f"WARN movers fetch failed: {e}")

    seen = list(dict.fromkeys(raw))

    # 4. Common stock only - drops warrants, units, rights and preferreds,
    #    which are also the symbols with no float/earnings coverage.
    common = [s for s in seen if is_common_stock_symbol(s)]
    dropped_not_common = len(seen) - len(common)

    # 5. Leveraged / inverse / volatility products.
    kept = [s for s in common if s not in EXCLUDED_TICKERS]
    dropped_excluded = len(common) - len(kept)

    # NOTE: there is deliberately no snapshot price pre-filter here. An earlier
    # version called /v2/stocks/snapshots to drop out-of-band names before
    # fetching bars. At this population that is 12 extra requests to save
    # nothing - bars for the full list fetch in ~15s either way - and the
    # endpoint intermittently returns 403, which would silently shrink the
    # universe. The authoritative price gate runs on bars inside the scan.

    # 6. Sticky candidates: once a ticker is detected today it stays in the
    #    universe for the rest of the session even if it drops off the
    #    screeners, so the session's own candidate stays re-observable.
    sticky_added = 0
    if con is not None:
        try:
            for s in ledger.runner_candidate_tickers(con, at.date()):
                if s not in kept:
                    kept.append(s)
                    sticky_added += 1
        except Exception as e:
            log("runner", f"WARN sticky-candidate union failed: {e}")

    # 7. Cap. Sticky names are moved to the front so a truncation can never
    #    drop a ticker already detected today.
    if len(kept) > cap:
        sticky = kept[len(kept) - sticky_added:] if sticky_added else []
        rest = kept[: len(kept) - sticky_added] if sticky_added else kept
        kept = list(dict.fromkeys(sticky + rest))[:cap]

    degraded = len(kept) < EXPECTED_MIN_UNIVERSE
    if degraded:
        log("runner", f"WARN universe is {len(kept)} symbols, below the expected "
                      f"floor of {EXPECTED_MIN_UNIVERSE} - recording session as "
                      f"degraded so a thin candidate list is not read as a stand-down")

    stats = {
        "universe_size": len(kept),
        "prev_daily": prev_daily,
        "universe_sources": sources,
        "dropped_not_common": dropped_not_common,
        "dropped_excluded": dropped_excluded,
        "dropped_price": 0,
        "sticky_added": sticky_added,
        "provider_degraded": degraded,
    }
    return kept, stats
