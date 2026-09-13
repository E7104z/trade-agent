"""
Runner universe construction (RUNNER_PORT_SPEC_v1 1.2).

`SCAN_UNIVERSE` is the wrong population for this setup: it is mega/large-cap,
and most of it fails the $5-$50 band before any condition is evaluated. The
Phase 0 parity fixtures measured exactly that - roughly half of a movers +
most-actives universe rejected on price alone. So the runner universe is built
fresh each session from the movers/most-actives screeners the morning loop
already uses.

No new data source is introduced. Everything here comes from
agent.providers.Alpaca, which is already fault-isolated and already the feed
the rest of the desk trusts.
"""
from __future__ import annotations

from datetime import datetime

from . import ledger
from .providers import is_common_stock_symbol
from .runner_scan import EXCLUDED_TICKERS
from .util import log

__all__ = ["build_universe"]


def build_universe(md, cfg: dict, *, at: datetime,
                   con=None) -> tuple[list[str], dict]:
    """Return (symbols, stats) for one scan tick.

    `md` is an agent.providers.MarketData. `cfg` is the `runner` config block.
    `con` is an open ledger connection used for step 5 (sticky candidates); it
    is optional so the function stays callable from a dry run with no database.

    stats keys: universe_size, universe_sources, dropped_not_common,
    dropped_excluded, dropped_price, sticky_added, provider_degraded.
    """
    price_min = float(cfg.get("price_min", 5.0))
    price_max = float(cfg.get("price_max", 50.0))
    cap = int(cfg.get("universe_size", 150))

    raw: list[str] = []
    sources = {"movers": 0, "most_actives": 0}
    alp = getattr(md, "alpaca", None)

    # 1. Movers + most-actives. Each is wrapped independently: one screener
    #    being down must degrade the universe, not empty it.
    if alp is not None:
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
        try:
            actives = alp.most_actives(top=50) or {}
            for row in actives.get("most_actives", []) or []:
                sym = (row.get("symbol") or "").upper()
                if sym:
                    raw.append(sym)
                    sources["most_actives"] += 1
        except Exception as e:
            log("runner", f"WARN most-actives fetch failed: {e}")

    seen: list[str] = list(dict.fromkeys(raw))

    # 2. Common stock only. is_common_stock_symbol already filters warrants,
    #    units, rights and preferreds - exactly the class of symbol for which
    #    float and earnings lookups have no coverage, and therefore exactly the
    #    class that would pile up in gates_unavailable under R-4.
    common = [s for s in seen if is_common_stock_symbol(s)]
    dropped_not_common = len(seen) - len(common)

    # 3. Leveraged / inverse / volatility products.
    kept = [s for s in common if s not in EXCLUDED_TICKERS]
    dropped_excluded = len(common) - len(kept)

    # 4. Cheap snapshot price pre-filter. The AUTHORITATIVE price gate is
    #    re-applied on bars inside the scan; this only avoids paying for bars
    #    on names that cannot possibly qualify. A symbol whose snapshot is
    #    missing is KEPT, not dropped - a missing snapshot is not evidence of
    #    an out-of-band price, and the bar-level gate will judge it properly.
    dropped_price = 0
    if alp is not None and kept:
        try:
            snaps = alp.snapshots(kept)
            priced = []
            for s in kept:
                snap = snaps.get(s) or {}
                px = ((snap.get("latestTrade") or {}).get("p")
                      or (snap.get("dailyBar") or {}).get("c")
                      or (snap.get("prevDailyBar") or {}).get("c"))
                if px is None:
                    priced.append(s)
                    continue
                if price_min <= float(px) <= price_max:
                    priced.append(s)
                else:
                    dropped_price += 1
            kept = priced
        except Exception as e:
            log("runner", f"WARN snapshot pre-filter failed; keeping full list: {e}")

    # 5. Sticky candidates. Once a ticker has been detected today it stays in
    #    the universe for the rest of the session even if it falls off the
    #    movers list. Without this, a name that ran at 10:05 and cooled by
    #    10:30 would vanish from the scan, and the session's own candidate
    #    would stop being re-observable.
    sticky_added = 0
    if con is not None:
        try:
            already = ledger.runner_candidate_tickers(con, at.date())
            for s in already:
                if s not in kept:
                    kept.append(s)
                    sticky_added += 1
        except Exception as e:
            log("runner", f"WARN sticky-candidate union failed: {e}")

    # 6. Cap to bound API cost and function runtime. Sticky candidates are
    #    appended last but must never be the ones truncated, so they are moved
    #    to the front of the cap.
    if len(kept) > cap:
        sticky = kept[len(kept) - sticky_added:] if sticky_added else []
        rest = kept[: len(kept) - sticky_added] if sticky_added else kept
        kept = list(dict.fromkeys(sticky + rest))[:cap]

    stats = {
        "universe_size": len(kept),
        "universe_sources": sources,
        "dropped_not_common": dropped_not_common,
        "dropped_excluded": dropped_excluded,
        "dropped_price": dropped_price,
        "sticky_added": sticky_added,
        # An empty universe is a pipeline failure, never a market condition.
        # The scan records this rather than silently reporting zero candidates.
        "provider_degraded": len(kept) == 0,
    }
    return kept, stats
