"""Shared per-ticker grading context for the evening loop and `run.py regrade`.

Both graders need the same context to reproduce identical outcomes:
  - adv20_map          -> RVOL denominator (RVOL checkpoints go NULL, not 0.0,
                          when this is unavailable)
  - atr_map/trend_map  -> indicator-layer v2 fields
  - prev_close_map     -> gap-relative fields (keyed to the GRADED date, not now)
  - fundamentals_map   -> short/float/mcap fields
  - entry_after_map    -> runner picks detected intraday are simulated from the
                          first bar they could have been acted on, not the 9:30
                          open (else "open" fills land below their own stop)

Keeping this in one place means a regrade recomputes outcomes under exactly the
inputs the evening run that first wrote them used — no silent drift between the
two code paths.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from . import ledger
from .providers import MarketData
from .util import log, prev_market_day


def build_grading_context(md: MarketData, con: Any, d: date,
                          tickers: list[str], cfg, tag: str = "grade") -> dict:
    """Fetch and assemble all grader-input context for one grading date.

    Never raises: every fetch is guarded, and a failed fetch degrades the
    corresponding fields to null/empty for the run (grade_day/simulate_pick
    already treat every one of these as optional context, not something that
    can fail a grade).
    """
    tickers = [t.upper() for t in tickers]

    try:
        adv20_map = md.adv20(tickers)
    except Exception as e:
        log(tag, f"WARN adv20 fetch failed ({e}) — RVOL fields will be null tonight")
        adv20_map = {}

    # indicator layer v2 (ATR/levels/context): one widened daily_context fetch
    # + one fundamentals fetch, plus each ticker's prior-session close for
    # gap-relative fields.
    atr_map: dict[str, float] = {}
    atr_pct_map: dict[str, float] = {}
    trend_map: dict[str, dict] = {}
    fundamentals_map: dict[str, dict] = {}
    prev_close_map: dict[str, float] = {}
    try:
        ctx = md.daily_context(tickers)
        atr_map = {t: c["atr14"] for t, c in ctx.items() if c.get("atr14")}
        atr_pct_map = {t: c["atr_pct"] for t, c in ctx.items() if c.get("atr_pct") is not None}
        trend_map = {t: c["trend_snapshot"] for t, c in ctx.items() if c.get("trend_snapshot")}
    except Exception as e:
        log(tag, f"WARN daily_context fetch failed ({e}) — ATR/trend fields null tonight")

    # prev_close keyed to the GRADED date d (not "now") — daily_context's own
    # "today" cutoff tracks wall-clock now, which is wrong for a regrade of a
    # past date.
    for t in tickers:
        try:
            prev = md.daily_bar(t, prev_market_day(d))
            if prev and prev.get("close"):
                prev_close_map[t] = prev["close"]
        except Exception:
            pass

    try:
        fundamentals_map = md.fundamentals(tickers)
    except Exception as e:
        log(tag, f"WARN fundamentals fetch failed ({e}) — short/float/mcap fields null tonight")

    # Picks detected INTRADAY cannot be simulated from the session open. A
    # runner pick carries entry_type "open", which fills at bars[0] — the 09:30
    # print — against a stop derived from, say, a 10:20 EMA. The fill lands
    # below its own stop and the trade grades invalid_fill. Build the entry-time
    # map so those picks are simulated from when they could first be acted on.
    entry_after_map: dict[str, Any] = {}
    try:
        from .runner_scan import earliest_entry_time
        for c in ledger.runner_candidates_for_date(con, d):
            cutoff = earliest_entry_time(c.get("first_pass_et"))
            if cutoff is not None:
                entry_after_map[c["ticker"].upper()] = cutoff
    except Exception as e:
        log(tag, f"WARN could not build runner entry times ({e}) — "
                 f"runner picks would be graded from the session open")

    return {
        "adv20_map": adv20_map,
        "atr_map": atr_map,
        "atr_pct_map": atr_pct_map,
        "trend_map": trend_map,
        "fundamentals_map": fundamentals_map,
        "prev_close_map": prev_close_map,
        "entry_after_map": entry_after_map,
        "or_minutes": int(cfg.data_cfg.get("or_minutes", 15)),
    }
