"""Schema v7 - additive live paper execution ledger."""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 7

STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS live_orders (
        live_order_id TEXT PRIMARY KEY,
        pick_id INTEGER,
        session_date DATE,
        ticker TEXT,
        side TEXT,
        qty INTEGER,
        order_class TEXT,
        entry_order_type TEXT,
        submitted_at TIMESTAMP,
        status TEXT,
        entry_fill_price DOUBLE,
        entry_fill_at TIMESTAMP,
        exit_reason TEXT,
        exit_fill_price DOUBLE,
        exit_fill_at TIMESTAMP,
        live_r DOUBLE,
        reconciled BOOLEAN DEFAULT FALSE,
        raw_order_json TEXT
    )""",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS live_r DOUBLE",
    "ALTER TABLE live_orders ADD COLUMN IF NOT EXISTS oco_order_id TEXT",
    "ALTER TABLE live_orders ADD COLUMN IF NOT EXISTS exit_legs_armed BOOLEAN DEFAULT FALSE",
]