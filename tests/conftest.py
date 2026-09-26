"""
Suite-wide guard: no test may reach the production ledger.

ledger.connect() goes to Postgres whenever DATABASE_URL is set, and several
fixtures isolate only by pointing ledger.DB_PATH at a tmp file -- which
connect() ignores in that mode. VS Code's Python test runner loads
${workspaceFolder}/.env into the test process by default, so a .env holding
the production DSN turns each of those fixtures into writes against Neon
(test_morning_pipeline_repair alone marks EVERY rejections row
would_have_won +1.0R). Clearing it here covers every test, guarded or not,
including anything that connects at import/collection time.

A test that needs the Postgres branch sets a fake DSN itself
(monkeypatch.setenv in the test body runs after this fixture).
"""
from __future__ import annotations

import os

import pytest

os.environ.pop("DATABASE_URL", None)


@pytest.fixture(autouse=True)
def _no_production_ledger(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
