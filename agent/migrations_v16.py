"""
Schema v16 - live-trading observability + earnings lookup cache. Additive only.

Same rules as the earlier migrations: idempotent, bare token DOUBLE, no
sequences, every column nullable.

WHY

  runner_sessions.live_orders_note
      What happened when the selected picks were sent to the broker: how many
      were placed, and each refusal with the broker's own reason. Every runner
      order was refused for weeks (sub-penny prices -> 422) and the only trace
      was a list in a response body nothing kept.

  heartbeats
      The last time each unattended job actually ran. The intraday monitor is
      what enforces time stops and the end-of-day flatten on live paper
      positions; when nothing calls it, positions are silently held overnight
      (UBER, 2026-09-10 -> 09-15). A heartbeat row is the only way the
      dashboard can tell "monitor ran 4 minutes ago" from "monitor never ran".

  earnings_cache
      Next earnings date per symbol, checked once per day. The runner's
      earnings gate looked every survivor up in yfinance on EVERY scan tick
      (8 workers, 25s deadline) and abandoned dozens of lookups per tick,
      letting those candidates through with the gate unmeasured. An earnings
      date does not move intraday.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 16

STATEMENTS: list[str] = [
    "ALTER TABLE runner_sessions ADD COLUMN IF NOT EXISTS live_orders_note VARCHAR",
    """CREATE TABLE IF NOT EXISTS heartbeats (
        name      VARCHAR PRIMARY KEY,
        last_at   TIMESTAMP,
        detail    VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS earnings_cache (
        symbol        VARCHAR PRIMARY KEY,
        next_date     DATE,
        checked_date  DATE
    )""",
]
