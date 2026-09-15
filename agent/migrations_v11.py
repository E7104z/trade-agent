"""
Schema v11 - daily cache for the runner's liquid universe. Additive only.

Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY A CACHE TABLE

Building the liquid universe means fetching /v2/assets (~12,890 symbols),
filtering to ~7,182 tradable common stocks, then pulling previous-session daily
bars for all of them to apply a $5M/day dollar-volume floor. That last step
takes ~77 seconds.

It is the same answer for every tick of the session, and the intraday cron
fires every five minutes, so it is computed once per session and reused. The
row also carries prev_close and prev_volume for the whole universe - the same
daily-bar fetch produces them, and RVOL needs prev_volume for every ticker - so
caching it turns each subsequent tick into a single 5-minute bar fetch instead
of a bar fetch plus a 77-second daily fetch.

`payload` is JSON: {"liquid": [...], "prev": {"SYM": [close, volume], ...}}.
It is one row per session date, rewritten if the session is rebuilt.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 11

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS runner_universe_cache (
        session_date  DATE NOT NULL,
        kind          VARCHAR NOT NULL,
        built_at      TIMESTAMP,
        n             INTEGER,
        source        VARCHAR,
        payload       VARCHAR,
        PRIMARY KEY (session_date, kind)
    )""",
]
