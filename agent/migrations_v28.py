"""
Schema v28 - runner trade construction (specs/RUNNER_TRADE_CONSTRUCTION_v1.md).
Additive only.

Same rules as the earlier migrations: idempotent, bare token types, no
sequences, every column nullable.

  runner_candidate_outcomes -- what was knowable at the first moment the
  candidate could be bought, and what price did afterwards, for EVERY
  candidate with bars (traded, untraded, invalid fill alike). Computed by the
  evening from the session's minute bars, reading only bars strictly before
  the entry bar for every at-entry field.

      entry_px / entry_et
          The first enterable price: the open of the first minute bar at or
          after the honest entry time (runner_scan.honest_entry_time), before
          friction -- the price simulate_pick fills an `open` entry at.
      staleness_min
          Minutes from Held (first_pass_et, when the conditions first held)
          to entry_et. A plan taken at detection and bought an hour later is a
          different trade from one bought at once.
      vwap_at_entry / ext_vwap_atr
          Session VWAP through the last bar before entry, and
          (entry_px - vwap_at_entry) / ATR14: how extended above VWAP the
          name was when it could be bought (the 2026-09-23 review found the
          losers extended).
      spy_vwap_pct / qqq_vwap_pct
          (last close - session VWAP) / VWAP * 100 for SPY and QQQ through the
          last bar before entry: the index tape the long was bought into.
      drift_atr / run_up_atr / draw_down_atr
          Exit-independent continuation: (session close - entry_px) / ATR14,
          and the highest high / lowest low from entry to the close against
          entry_px, in ATR. No stop or target rule can win where price does
          not keep going up after entry; these say where it does.

  runner_exit_replays.stop_used / target_used / cost_r
      The stop and target each replayed policy actually carried (arm A and C
      widen the stop and move the target with it), and the friction charged
      in that policy's own R -- a tight stop pays the same cents in more R.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 28

_OUTCOME_COLUMNS = (
    ("entry_px", "DOUBLE"),
    ("entry_et", "TIMESTAMP"),
    ("staleness_min", "DOUBLE"),
    ("vwap_at_entry", "DOUBLE"),
    ("ext_vwap_atr", "DOUBLE"),
    ("spy_vwap_pct", "DOUBLE"),
    ("qqq_vwap_pct", "DOUBLE"),
    ("drift_atr", "DOUBLE"),
    ("run_up_atr", "DOUBLE"),
    ("draw_down_atr", "DOUBLE"),
)

STATEMENTS: list[str] = [
    *(f"ALTER TABLE runner_candidate_outcomes ADD COLUMN IF NOT EXISTS {c} {t}"
      for c, t in _OUTCOME_COLUMNS),
    "ALTER TABLE runner_exit_replays ADD COLUMN IF NOT EXISTS stop_used DOUBLE",
    "ALTER TABLE runner_exit_replays ADD COLUMN IF NOT EXISTS target_used DOUBLE",
    "ALTER TABLE runner_exit_replays ADD COLUMN IF NOT EXISTS cost_r DOUBLE",
]
