"""
Postgres compatibility shim (agent/pgshim.py) — the hosted (Vercel + Neon)
execution path where ledger.connect() swaps DuckDB for Postgres. Local/CI runs
exercise DuckDB only, so the shim is the one ledger layer that could rot
unseen. Pinned here without a live database:

  - '?' positional placeholders translate to psycopg2's '%s', but a '?' inside
    a quoted string literal (including an escaped '' quote) is left alone
  - DESCRIBE <table> is rewritten to an information_schema.columns lookup that
    still yields column names as row[0]
  - PGConnection.execute mirrors duckdb's chaining surface (.fetchone/.fetchall)
  - the Postgres branch of ledger.connect() rewrites the DuckDB-only alias
    DOUBLE -> DOUBLE PRECISION (DuckDB accepts it; Postgres does not)
"""
from __future__ import annotations

import pytest

from agent import pgshim


# --------------------------------------------------------------- placeholders

def test_placeholders_translate_and_leave_quoted_question_marks_alone():
    sql = "SELECT * FROM t WHERE a = ? AND b = '?' AND c = 'it''s ?' AND d = ?"
    assert pgshim._translate_placeholders(sql) == (
        "SELECT * FROM t WHERE a = %s AND b = '?' AND c = 'it''s ?' AND d = %s"
    )


def test_placeholders_never_rewrite_an_unterminated_literal():
    # A stray opening quote is taken literally: everything after it is copied
    # verbatim, which is the safe direction (no placeholder is fabricated).
    assert pgshim._translate_placeholders("SELECT '? FROM t WHERE x = ?") == (
        "SELECT '? FROM t WHERE x = ?"
    )


# --------------------------------------------------------------- DESCRIBE

def test_describe_regex_accepts_only_a_bare_table_name():
    assert pgshim._DESCRIBE_RE.match("DESCRIBE picks")
    assert pgshim._DESCRIBE_RE.match("  describe outcomes;  ")
    assert pgshim._DESCRIBE_RE.match("DESCRIBE   picks ;")
    assert not pgshim._DESCRIBE_RE.match("SELECT * FROM picks")
    assert not pgshim._DESCRIBE_RE.match("DESCRIBE picks WHERE x = 1")


# ----------------------------------------------------------- PGConnection

class _FakeCursor:
    def __init__(self, rows=None):
        self.sql = None
        self.params = None
        self.rows = rows

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows or []


class _FakePG:
    def __init__(self, rows=None):
        self.autocommit = None
        self.rows = rows
        self.cursors: list[_FakeCursor] = []

    def cursor(self):
        c = _FakeCursor(self.rows)
        self.cursors.append(c)
        return c

    def close(self):
        pass


def _pg(monkeypatch, rows=None) -> _FakePG:
    fake = _FakePG(rows)
    monkeypatch.setattr(pgshim.psycopg2, "connect", lambda dsn: fake)
    return fake


def test_describe_is_rewritten_to_information_schema(monkeypatch):
    fake = _pg(monkeypatch)
    con = pgshim.PGConnection("postgres://unit-test")
    con.execute("DESCRIBE picks")
    assert "information_schema.columns" in fake.cursors[0].sql
    assert fake.cursors[0].params == ("picks",)


def test_execute_translates_placeholders(monkeypatch):
    fake = _pg(monkeypatch)
    con = pgshim.PGConnection("postgres://unit-test")
    con.execute("UPDATE t SET x = ? WHERE y = ? AND z = '?'", [1, 2])
    assert fake.cursors[0].sql == "UPDATE t SET x = %s WHERE y = %s AND z = '?'"
    assert fake.cursors[0].params == [1, 2]


def test_result_supports_duckdb_style_chaining(monkeypatch):
    _pg(monkeypatch, rows=[("a",), ("b",)])
    con = pgshim.PGConnection("postgres://unit-test")
    assert con.execute("SELECT * FROM t").fetchone() == ("a",)
    assert con.execute("SELECT * FROM t").fetchall() == [("a",), ("b",)]



# ------------------------------------------------- DOUBLE -> DOUBLE PRECISION

def test_postgres_connect_rewrites_double_type_alias(monkeypatch):
    from agent import ledger

    captured = {}

    class _FakePG:
        def execute(self, sql, params=None):
            return _Res((ledger._SCHEMA_FINGERPRINT,))

        def close(self):
            pass

    class _Res:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    def _capture_apply_schema(con, translate):
        captured["translate"] = translate

    monkeypatch.setattr(pgshim, "connect_pg", lambda dsn: _FakePG())
    monkeypatch.setenv("DATABASE_URL", "postgres://unit-test")
    monkeypatch.setattr(ledger, "_SCHEMA_READY", set())
    monkeypatch.setattr(ledger, "_schema_is_current", lambda con: False)
    monkeypatch.setattr(ledger, "_apply_schema", _capture_apply_schema)

    ledger.connect()

    translate = captured["translate"]
    # The DuckDB alias becomes a real Postgres type...
    assert translate("CREATE TABLE x (r DOUBLE, note VARCHAR)") == (
        "CREATE TABLE x (r DOUBLE PRECISION, note VARCHAR)"
    )
    # ...and a word that merely CONTAINS the alias is left alone.
    assert translate("CREATE TABLE x (d DOUBLED VARCHAR)") == (
        "CREATE TABLE x (d DOUBLED VARCHAR)"
    )
