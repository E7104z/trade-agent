"""Repair runner_candidates rows whose change_pct was fabricated by a split.

Root cause
----------
`Alpaca.multi_daily_bars` used to request daily bars with ``adjustment="raw"``.
When a stock had a reverse split between the previous session's close and the
current session's detection, ``prev_close`` stayed on the pre-split share basis
while ``detection_price`` was post-split, so ``change_pct`` showed a phantom
gap.  Measured live: NBIZ 2026-09-22 printed +284% from a 1:4 reverse split,
not a move.

The fix in agent/providers.py now fetches those bars split-adjusted, which is
exactly right for live scans (session date == "now", so the adjustment basis
equals the detection basis).  This script is for backfilling rows already
written under the old code.

The one subtlety: Alpaca's ``adjustment="split"`` normalises a bar to *today's*
basis, so for a PAST session where a later split has occurred, the split-adjusted
prev close would be over-adjusted.  We undo that by dividing out any split that
happened *after* the session:

    f_after        = split_close(session) / raw_close(session)   # 1 if none
    prev_close     = split_close(prev_day) / f_after
    prev_volume    = split_volume(prev_day) * f_after
    change_pct     = (detection_price - prev_close) / prev_close * 100

For the live case f_after == 1 and this reduces to plain split adjustment.

Usage
-----
    .venv/Scripts/python -m scripts.backfill_change_pct --threshold 40 [--write]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb  # noqa: E402

from agent.config import load_config  # noqa: E402
from agent.providers import Alpaca  # noqa: E402
from agent.util import prev_market_day  # noqa: E402


DB = "data/ledger.duckdb"


def _daily(alp: Alpaca, ticker: str, start, end, adjustment: str) -> dict:
    """Daily bars for [start, end] keyed by bar date string."""
    raw = alp._get(
        "/v2/stocks/{sym}/bars".format(sym=ticker),
        {
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adjustment": adjustment,
            "feed": alp.feed,
        },
    )
    out: dict = {}
    for b in raw.get("bars") or []:
        out[b["t"][:10]] = {"close": float(b["c"]), "volume": float(b["v"])}
    return out


def recompute(alp: Alpaca, session_date, ticker: str, detection_price: float):
    """Return (prev_close, prev_volume, change_pct) on the session-date basis."""
    pd_ = prev_market_day(session_date)
    raw = _daily(alp, ticker, pd_, session_date, "raw")
    split = _daily(alp, ticker, pd_, session_date, "split")

    pd_key, d_key = pd_.isoformat(), session_date.isoformat()
    if pd_key not in split:
        return None

    # f_after = any split that occurred AFTER this session (between session and
    # today).  For the live case there is none and this is 1.0.
    f_after = 1.0
    if pd_key in raw and d_key in raw and d_key in split:
        r_d, s_d = raw[d_key]["close"], split[d_key]["close"]
        if r_d:
            f_after = s_d / r_d

    prev_close = split[pd_key]["close"] / f_after
    prev_volume = split[pd_key]["volume"] * f_after
    change_pct = ((detection_price - prev_close) / prev_close) * 100 if prev_close else None
    return prev_close, prev_volume, change_pct


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=40.0,
                    help="reconsider rows with |change_pct| above this (default 40)")
    ap.add_argument("--write", action="store_true", help="apply updates (default: dry run)")
    args = ap.parse_args()

    cfg = load_config()
    alp = Alpaca(cfg)
    alp.feed = "iex"  # this account is SIP-refused for realtime; IEX has history

    con = duckdb.connect(DB)
    cols = [c[0] for c in con.execute("DESCRIBE runner_candidates").fetchall()]
    rows = con.execute(
        "SELECT session_date, ticker, detection_price, prev_close, prev_volume, change_pct "
        "FROM runner_candidates WHERE ABS(change_pct) > ? ORDER BY session_date, ticker",
        [args.threshold],
    ).fetchall()

    changed = 0
    for session_date, ticker, det, old_close, old_vol, old_pct in rows:
        res = recompute(alp, session_date, ticker, float(det))
        if res is None:
            print(f"SKIP {ticker} {session_date}: no split-adjusted prev bar")
            continue
        new_close, new_vol, new_pct = res
        if new_pct is not None and abs(new_pct - old_pct) < 0.5:
            print(f"KEEP {ticker} {session_date}: {old_pct:+.2f}% -> {new_pct:+.2f}% "
                  f"(no split; genuine gap)")
            continue
        print(f"FIX  {ticker} {session_date}: change_pct {old_pct:+.2f}% -> {new_pct:+.2f}%  "
              f"prev_close {old_close} -> {new_close:.3f}  prev_volume {old_vol} -> {new_vol:.0f}")
        if args.write:
            con.execute(
                "UPDATE runner_candidates SET prev_close=?, prev_volume=?, change_pct=? "
                "WHERE session_date=? AND ticker=?",
                [new_close, new_vol, new_pct, session_date, ticker],
            )
        changed += 1

    if args.write:
        print(f"\n{changed} row(s) updated.")
    else:
        print(f"\nDry run: {changed} row(s) would change. Re-run with --write to apply.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
