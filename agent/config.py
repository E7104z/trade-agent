"""
Configuration for Trade Agent.

Layered: built-in defaults <- config.json <- credentials.json <- environment vars.
Secrets live in credentials.json (gitignored); tunables live in config.json (tracked).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "ledger.duckdb"
REPORTS_DIR = BASE_DIR / "reports"
PROMPTS_DIR = BASE_DIR / "prompts"
MOCKS_DIR = BASE_DIR / "mocks"
PLAYBOOK_PATH = BASE_DIR / "playbook.md"
PLAYBOOK_HISTORY_DIR = BASE_DIR / "playbook_history"
CONFIG_PATH = BASE_DIR / "config.json"
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
ECON_OVERRIDES_PATH = DATA_DIR / "econ_overrides.json"

_DEFAULTS: dict[str, Any] = {
    "models": {
        "morning": "claude-opus-4-5",
        "evening": "claude-sonnet-4-5",
        "weekly": "claude-opus-4-5",
    },
    "extended_thinking": True,
    "thinking_budget_tokens": 8000,
    "max_output_tokens": 16000,
    "enable_web_search": True,
    "web_search_max_uses": 6,
    "account_size": 100_000,
    "dollars_per_r": 500,
    "max_picks": 4,
    "max_size_r": 1.5,
    "min_size_r": 0.25,
    "max_total_open_risk_r": 3.0,
    "slippage_bps": 5,
    "flatten_at": None,        # e.g. "15:55" -- session-end flatten cutoff, default OFF
    "min_bar_volume": 0,       # thin-bar filter threshold, default OFF (0 = disabled)
    "lesson_holdout_sessions": 0,  # trailing-window lesson-confirmation holdout, default OFF
    "timezone": "America/New_York",
    "email_enabled": False,
    "data": {
        "alpaca_feed": "iex",
        "movers_top": 18,
        "news_limit": 40,
        "gap_scan_min_pct": 3.0,
        "edgar_user_agent": "TradeAgent research bot (contact: user@example.com)",
    },
    "memory": {
        "recent_trades_in_context": 20,
        "max_active_lessons_in_context": 25,
    },
}

# Approximate USD per million tokens (input, output) — for run-cost logging only.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif v not in (None, ""):
            out[k] = v
    return out


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"[config] Could not parse {path.name}: {e}")
    return {}


@dataclass
class Config:
    raw: dict = field(default_factory=dict)
    # secrets
    anthropic_api_key: str = ""
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    smtp: dict = field(default_factory=dict)
    # runtime flags (set by CLI)
    mock: bool = False
    offline: bool = False
    dry_run: bool = False
    no_email: bool = False

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def models(self) -> dict:
        return self.raw["models"]

    @property
    def data_cfg(self) -> dict:
        return self.raw["data"]

    @property
    def memory_cfg(self) -> dict:
        return self.raw["memory"]

    @property
    def has_alpaca(self) -> bool:
        return bool(self.alpaca_key_id and self.alpaca_secret_key)

    @property
    def email_configured(self) -> bool:
        s = self.smtp or {}
        return bool(
            self.raw.get("email_enabled")
            and not self.no_email
            and s.get("host")
            and s.get("username")
            and s.get("password")
            and s.get("to_addrs")
        )


def load_config() -> Config:
    merged = _deep_merge(_DEFAULTS, _load_json(CONFIG_PATH))
    creds = _load_json(CREDENTIALS_PATH)

    cfg = Config(raw=merged)
    cfg.anthropic_api_key = (
        os.getenv("ANTHROPIC_API_KEY") or creds.get("anthropic_api_key", "") or ""
    )
    cfg.alpaca_key_id = (
        os.getenv("ALPACA_API_KEY")
        or os.getenv("ALPACA_KEY_ID")
        or creds.get("alpaca_key_id", "")
        or ""
    )
    cfg.alpaca_secret_key = (
        os.getenv("ALPACA_SECRET_KEY") or creds.get("alpaca_secret_key", "") or ""
    )
    smtp = dict(creds.get("smtp", {}) or {})
    if os.getenv("SMTP_HOST"):
        smtp["host"] = os.environ["SMTP_HOST"]
    if os.getenv("SMTP_PORT"):
        smtp["port"] = int(os.environ["SMTP_PORT"])
    if os.getenv("SMTP_USERNAME"):
        smtp["username"] = os.environ["SMTP_USERNAME"]
    if os.getenv("SMTP_PASSWORD"):
        smtp["password"] = os.environ["SMTP_PASSWORD"]
    if os.getenv("SMTP_FROM"):
        smtp["from_addr"] = os.environ["SMTP_FROM"]
    if os.getenv("SMTP_TO"):
        smtp["to_addrs"] = [a.strip() for a in os.environ["SMTP_TO"].split(",") if a.strip()]
    cfg.smtp = smtp
    return cfg


def ensure_dirs() -> None:
    for d in (DATA_DIR, REPORTS_DIR, PLAYBOOK_HISTORY_DIR):
        d.mkdir(parents=True, exist_ok=True)
