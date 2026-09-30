"""
Schema v25 - playbook audit (2026-09-29). Additive only.

Same rules as the earlier migrations: idempotent, bare token types, no
sequences, every column nullable.

  rejections.kill_checks / kill_checks_n
      Every kill check the risk manager said was binding on a rejection, not
      only the primary one. A rejection that failed two checks (UAL: R:R 1.80
      AND the same airline thesis as AAL) was charged entirely to the R:R
      filter, so every per-category forgone-R figure was misattributed. The
      shadow summary groups a multi-check rejection as `multiple`.

  selection_edge_history.method_version
      The ablation behind each weekly Selection Edge row. Method 1 (every row
      written before this migration, NULL here) drew the desk's arm from its
      TRIGGERED picks only while the random arm drew from a pool padded with
      untriggered and scratch rejections worth 0R, and it pooled runner-lane
      picks into the morning desk's arm. A row computed that way is not
      comparable to one computed by method 2 and is rendered as withdrawn.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION"]

SCHEMA_VERSION = 25

STATEMENTS: list[str] = [
    "ALTER TABLE rejections ADD COLUMN IF NOT EXISTS kill_checks VARCHAR",
    "ALTER TABLE rejections ADD COLUMN IF NOT EXISTS kill_checks_n INTEGER",
    "ALTER TABLE selection_edge_history ADD COLUMN IF NOT EXISTS method_version INTEGER",
]
