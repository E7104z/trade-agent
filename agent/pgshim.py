"""
Postgres compatibility shim so agent/ledger.py (written against DuckDB's
con.execute(sql, params).fetchall()-style API) works UNCHANGED against a
hosted Neon Postgres database on Vercel, where there is no persistent local
disk for a DuckDB file.

Translates:
  - '?' positional placeholders -> psycopg2's '%s'
  - DuckDB's `DESCRIBE <table>` -> information_schema.columns lookup
    (returns rows shaped so `[c[0] for c in ...]` still yields column names)

Connection mode is chosen by agent/ledger.py: if DATABASE_URL is set, use
this Postgres-backed connection; otherwise fall back to local DuckDB (so the
original CLI/local-dev workflow keeps working unchanged).
"""
from __future__ import annotations

import re

import psycopg2
import psycopg2.extras

_DESCRIBE_RE = re.compile(r"^\s*DESCRIBE\s+([A-Za-z_][A-Za-z0-9_]*)\s*;?\s*$", re.IGNORECASE)


def _translate_placeholders(sql: str) -> str:
    """
    Replace '?' positional placeholders with psycopg2's '%s', WITHOUT touching
    '?' characters that appear inside single-quoted SQL string literals (e.g.
    COALESCE(x, '?') as a default label) — a blind global .replace() would
    silently corrupt those literal values.
    """
    out = []
    in_str = False
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if in_str:
            out.append(c)
            if c == "'":
                # SQL escapes a literal quote as '' — a doubled quote stays inside the string
                if i + 1 < n and sql[i + 1] == "'":
                    out.append(sql[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            out.append(c)
        elif c == "?":
            out.append("%s")
        else:
            out.append(c)
        i += 1
    return "".join(out)


class _Result:
    """Chainable result wrapper: con.execute(...).fetchone() / .fetchall()."""

    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class PGConnection:
    """Drop-in replacement for duckdb.DuckDBPyConnection used by ledger.py."""

    def __init__(self, dsn: str):
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = True  # mirror duckdb's implicit-commit usage

    def execute(self, sql: str, params=None) -> _Result:
        m = _DESCRIBE_RE.match(sql)
        cur = self._conn.cursor()
        if m:
            table = m.group(1)
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position",
                (table,),
            )
            return _Result(cur)
        translated = _translate_placeholders(sql)
        cur.execute(translated, params)
        return _Result(cur)

    def close(self) -> None:
        self._conn.close()


def connect_pg(dsn: str) -> PGConnection:
    return PGConnection(dsn)
