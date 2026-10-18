"""
Schema v38 - an entry order must express its setup (docs/FIX_PHASE0.md, D5).
Additive only.

Same rules as the earlier migrations: idempotent, bare token types, no
sequences, every column nullable.

  picks.entry_semantics
      'ok', or the specific way the entry order contradicts the setup it was
      proposed for (morning._entry_semantics): strength_buy_limit_below_ref,
      weakness_sell_limit_above_ref, breakdown_short_with_limit,
      breakout_long_with_limit, gap_fade_limit_without_expiry. Written by the
      morning validator for every pick that reaches the tape, TAG ONLY unless
      config `validator.enforce_entry_semantics` is on. NULL on a pick from
      before the tag existed.

  picks.entry_ref_price
      The premarket reference price the tag was judged against (the bundle's
      last price at plan time), so the tag can be recomputed and audited from
      the row alone.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 38

STATEMENTS: list[str] = [
    "ALTER TABLE picks ADD COLUMN IF NOT EXISTS entry_semantics VARCHAR",
    "ALTER TABLE picks ADD COLUMN IF NOT EXISTS entry_ref_price DOUBLE",
]
