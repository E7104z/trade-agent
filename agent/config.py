"""
Configuration for Trade Agent.

Layered: built-in defaults <- config.json <- credentials.json <- environment vars.
Secrets live in credentials.json (gitignored); tunables live in config.json (tracked).
"""
from __future__ import annotations

import json
import os
import re
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
    "exploration_lane": {
        "enabled": False,
        "min_rr": 1.8,
        "max_rr": 2.0,
        "max_picks": 1,
        "size_r": 0.25,
    },
    # The desk trades every session: when review and validation leave the plan
    # empty, the best valid rejected candidate goes on at minimum size, labeled
    # `daily_floor` so it is measured apart from full approvals.
    "daily_floor": {
        "enabled": False,
        "size_r": 0.25,
        "min_rr": 1.0,
    },
    # Every setup the analyst finds gets traded: each candidate the risk manager
    # rejected on judgment goes on at minimum size, one per setup, labeled
    # `coverage`. Only a broken order (blocking_kills) stays off the tape.
    "coverage_lane": {
        "enabled": False,
        "size_r": 0.25,
        "min_rr": 1.0,
        "max_picks": 12,
        "blocking_kills": ["grounding", "entry_type", "event_risk"],
    },
    "max_entry_ref_distance_pct": 15.0,  # entry further than this from last price = ungrounded, dropped
    "atr_gate": "warn",         # "warn" | "enforce" -- noise-stop gate rollout switch, default warn-only
    "earnings_gate": "warn",    # "warn" | "enforce" -- binary-event gate rollout switch, default warn-only
    "min_stop_atr": 0.35,       # stop closer than this many ATRs from entry = inside daily noise
    "warn_stop_atr": 2.5,       # stop farther than this many ATRs = unusual, warn but pass
    "warn_target_atr": 3.5,     # target farther than this (with a same-day time_stop) = warn but pass
    "slippage_bps": 5,
    "flatten_at": None,        # e.g. "15:55" -- session-end flatten cutoff, default OFF
    "live_paper": {
        "enabled": False,
        "alpaca_paper": False,
        "breakout_two_step": False,
        "trading_base": "https://paper-api.alpaca.markets",
        "max_picks_per_day": 4,
        "intraday_check_secret_env": "CRON_SECRET",
    },
    # --- DTO Runner lane (specs/RUNNER_PORT_SPEC_v1.md §1.6) ---------------
    # Three independent switches so each phase can be turned on alone:
    #   enabled             gates the intraday scan (Phase 1)
    #   selection_enabled   gates the one LLM call per session (Phase 2)
    #   live_orders_enabled gates paper order placement (Phase 2)
    # All default OFF. With every switch off this block changes nothing.
    "runner": {
        "enabled": False,
        "phase": 1,
        # Conditions need 10 five-minute bars from 09:30, so nothing can
        # qualify before 10:20. Scanning earlier finds nothing and spends
        # bar-fetch budget on ~1,190 symbols for no reason.
        "scan_start_et": "10:20",
        "scan_end_et": "15:00",
        # Liquid tradable universe (~4,068 at a $5M/day dollar floor)
        # + DEFAULT_UNIVERSE + most-actives(100 x2) + movers(50),
        # which is ~4,224 after filtering. The cap exists only to bound
        # a runaway screener - set it ABOVE the real population, or it
        # silently truncates the scan (measured: a 1,500 cap cut 4,224
        # to 1,500 and took candidates from 11 to 3).
        "universe_size": 6000,
        "price_min": 5.0,
        "price_max": 50.0,
        "min_vwap_pct": 0.5,
        # 0 = RVOL is computed, recorded and shown to the selector, but does
        # not gate. DTO nominally requires 1.5x, but its RVOL runs ~17x hot
        # (a real scan list of 46 names had a MEDIAN of 7.0x and included
        # AGNC at 11.2x and EWZ at 10.1x), so that gate passed almost
        # everything. On a correctly-scaled RVOL, 1.5 cuts a 57-candidate
        # list to 7 - a filter 17x stricter than the one that actually ran.
        "min_rvol": 0.0,
        # RVOL is a core runner-screen input. When true (default in the live
        # lane), a name whose RVOL cannot be computed (missing previous-day
        # volume, or an IEX-downgraded session) is hard-skipped rather than
        # traded on an unreadable participation signal. Keep true in
        # production; set false only for a DTO-parity comparison.
        "require_rvol": True,
        # Minimum stop distance, max(abs, pct * price). When price sits on the
        # 9 EMA at detection the stop collapses toward zero and every R against
        # it is meaningless (measured: a $0.01 stop grading -5.15R). DTO applies
        # the same floor; it is a sanity guard on geometry, not an edge claim.
        "min_stop_abs": 0.10,
        "min_stop_pct": 0.005,
        "float_min_m": 5.0,
        "float_max_m": 200.0,
        # DTO hard-gates trend consistency at 45. The THRESHOLD is corpus-
        # derived and so is a "conclusion" under spec R-2, but it is also a
        # live condition that changes the passing set. Kept at DTO's value so
        # detection matches what was actually traded; set to null to run
        # detection without it and re-derive the threshold from our own
        # forward evidence.
        "min_trend_consistency": 45.0,
        # Walk first_pass_et back to the bar where conditions actually turned
        # true, instead of stamping the scan cycle that happened to notice.
        "walkback": True,
        "earnings_exclude_days": 2,
        # "dto"    - a missing float/earnings PASSES and is recorded
        #            (matches DTO; keeps the candidate pool intact)
        # "reject" - a gate that cannot run has not passed (strict)
        "null_gates": "dto",
        "decision_time_et": "10:30",
        "max_picks": 10,
        "selection_enabled": False,
        "selection_model": "claude-sonnet-4-6",
        "live_orders_enabled": False,
        "max_live_orders_per_day": 10,
        "dollars_per_r": 100,
        "size_r": 0.25,
    },
    "min_bar_volume": 0,       # thin-bar filter threshold, default OFF (0 = disabled)
    "lesson_holdout_sessions": 0,  # trailing-window lesson-confirmation holdout, default OFF
    "timezone": "America/New_York",
    "email_enabled": False,
    "data": {
        "alpaca_feed": "sip",
        "movers_top": 18,
        "news_limit": 40,
        "gap_scan_min_pct": 3.0,
        "edgar_user_agent": "TradeAgent research bot (contact: user@example.com)",
        "indicator_enrich": True,          # premarket VWAP/RVOL on gap-scan + catalyst quotes
        "context_enrich": True,            # ATR/levels/trend/fundamentals/earnings ticker cards (v2)
        "or_minutes": 15,                  # opening-range window, minutes
        "gap_scan_price_band": [None, None],  # optional [min, max] last-price hard filter, default OFF
        # Concurrent batched Alpaca data requests. Algo Trader Plus allows
        # 10,000 calls/min; set to 1 on the free plan (~200/min).
        "alpaca_concurrency": 8,
    },
    "memory": {
        "recent_trades_in_context": 20,
        "max_active_lessons_in_context": 25,
    },
    # --- v4 research integrity layer (specs/RESEARCH_INTEGRITY_SPEC_v4.md) ---
    # Every default here reproduces current behavior exactly; nothing here
    # changes a number, a fill, or a decision until deliberately flipped.
    "research": {
        "fdr_q": 0.10,      # Benjamini-Hochberg FDR level across the whole weekly run
        "min_n": 8,         # matches the playbook's existing reporting bar
    },
    "population": {"enabled": True},   # observation only: adds rows, changes no decision
    "friction": {
        "model": "flat",               # "flat" = today's exact behavior
        "base_bps": 2.0,
        "spread_k": 1.6,
        "impact_k": 220.0,
        "max_bps": 120.0,
        "stress_multipliers": [1.0, 2.0, 3.0],
    },
    "paired": {
        "enabled": True,               # always-on per decision 2026-08-27
        "every_n_sessions": 1,          # daily -- throttling costs months of statistical power
        "arm_b_context_mode": "aggregate_only",  # ablate the ungated last-20-trades channel first
        "arm_b_playbook_version": None,
    },
    "ablation_sim_version": None,       # restrict the selection ablation to one sim_version
    # --- persona (specs/PERSONA_CHARACTER_SHEET_SPEC_v1.md) -----------------
    # One character sheet (prompts/persona.md) shared by the five system
    # prompts. A treatment: off by default, and with it off every rendered
    # prompt is byte-identical to the pre-persona text. `version` pins the
    # sheet that was switched on; a persona.md carrying another version is
    # not used. Rows it influenced carry persona_version (migration v29).
    "persona": {"enabled": False, "version": 1},
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
    # Where the Alpaca key came from, and anything load_config had to clean off
    # a pasted value. Never the values themselves.
    alpaca_key_source: str = ""
    credential_notes: dict = field(default_factory=dict)
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
    def persona_cfg(self) -> dict:
        return self.raw.get("persona") or {}

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


# An env-style NAME (upper case) followed by = or : and a value that does not
# itself start with '='. The value rule keeps base64-style secrets that END in
# "==" from being misread as NAME=VALUE.
_NAME_EQUALS_VALUE = re.compile(r"^[A-Z][A-Z0-9_]*\s*[:=]\s*([^\s=]\S*)$")


def clean_credential(value: str | None) -> tuple[str, list[str]]:
    """Undo what pasting a credential into a settings form typically adds.

    Returns (cleaned value, notes describing what was removed). The notes
    never contain the value. A real API key contains no whitespace, quotes,
    '=' or ':', so removing these can only turn a corrupted paste back into
    the key; it cannot damage a correct one. Handled: surrounding whitespace
    and newlines, surrounding quotes, and a whole `NAME = value` line pasted
    into the value field (the shape these keys were shared in on 2026-09-24).
    """
    raw = value or ""
    notes: list[str] = []
    v = raw.strip()
    if v != raw:
        notes.append("surrounding whitespace/newline removed")
    m = _NAME_EQUALS_VALUE.match(v)
    if m:
        v = m.group(1)
        notes.append("a 'NAME = value' line was pasted; the value was extracted")
    elif v[:1] in ("=", ":"):
        v = v[1:].strip()
        notes.append("leading '=' removed")
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
        notes.append("surrounding quotes removed")
    if any(ch.isspace() for ch in v):
        notes.append("still contains internal whitespace (not removed)")
    return v, notes


def load_config() -> Config:
    merged = _deep_merge(_DEFAULTS, _load_json(CONFIG_PATH))
    creds = _load_json(CREDENTIALS_PATH)

    cfg = Config(raw=merged)
    cfg.anthropic_api_key, _ = clean_credential(
        os.getenv("ANTHROPIC_API_KEY") or creds.get("anthropic_api_key", "") or ""
    )
    # Key id precedence: ALPACA_API_KEY, then ALPACA_KEY_ID, then
    # credentials.json. The source is recorded so a rejected key can be traced
    # to the setting that supplied it.
    for source, raw in (("env:ALPACA_API_KEY", os.getenv("ALPACA_API_KEY")),
                        ("env:ALPACA_KEY_ID", os.getenv("ALPACA_KEY_ID")),
                        ("credentials.json", creds.get("alpaca_key_id", ""))):
        if raw:
            cfg.alpaca_key_id, notes = clean_credential(raw)
            cfg.alpaca_key_source = source
            if notes:
                cfg.credential_notes["alpaca_key_id"] = notes
            break
    raw_secret = os.getenv("ALPACA_SECRET_KEY") or creds.get("alpaca_secret_key", "") or ""
    cfg.alpaca_secret_key, notes = clean_credential(raw_secret)
    if notes:
        cfg.credential_notes["alpaca_secret_key"] = notes
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


# (key id length, secret length) pairs Alpaca has issued.
_ALPACA_KEY_SHAPES = {(20, 40), (26, 44)}


def alpaca_credential_fingerprint(cfg: Config) -> dict:
    """A description of the Alpaca credentials in use that reveals nothing
    usable: where the key came from, lengths, the two-letter key-type prefix,
    character classes, and what was cleaned off. Lets a 401 be diagnosed from
    the dashboard ("the key in Vercel is 27 chars and has a space in it")
    without anyone reading a secret.

    `usual_format` compares against the shapes Alpaca has issued keys in: an
    upper-case alphanumeric key id with an alphanumeric secret, either 20/40
    characters (older keys) or 26/44 (keys issued from 2026-09). It is
    informational, never a gate: if Alpaca changes its format, keys that fail
    this check still get used.
    """
    key, secret = cfg.alpaca_key_id or "", cfg.alpaca_secret_key or ""
    prefix = key[:2].upper()
    shape_ok = (len(key), len(secret)) in _ALPACA_KEY_SHAPES
    return {
        "key_source": cfg.alpaca_key_source or "(none)",
        "key_present": bool(key),
        "key_length": len(key),
        "key_type": {"PK": "paper", "AK": "live"}.get(prefix, "unrecognized"),
        "key_upper_alnum": bool(key) and key.isalnum() and key == key.upper(),
        "secret_present": bool(secret),
        "secret_length": len(secret),
        "secret_alnum": bool(secret) and secret.isalnum(),
        "usual_format": (shape_ok and key.isalnum() and key == key.upper()
                         and secret.isalnum()),
        "cleaned": dict(cfg.credential_notes or {}),
    }


def describe_alpaca_fingerprint(fp: dict) -> str:
    """One human line for the dashboard and alerts. Contains no secret."""
    if not fp.get("key_present") or not fp.get("secret_present"):
        missing = [n for n, ok in (("key id", fp.get("key_present")),
                                   ("secret", fp.get("secret_present"))) if not ok]
        return f"Alpaca {' and '.join(missing)} not set (key source: {fp.get('key_source')})"
    bits = [f"key from {fp['key_source']}: {fp['key_length']} chars, {fp['key_type']} "
            f"({'upper-case alphanumeric' if fp['key_upper_alnum'] else 'NOT upper-case alphanumeric'})",
            f"secret: {fp['secret_length']} chars"
            + ("" if fp["secret_alnum"] else ", contains non-alphanumeric characters")]
    if not fp.get("usual_format"):
        bits.append("does not match the usual Alpaca shape "
                    "(key id / secret of 20/40 or 26/44 chars, alphanumeric)")
    for name, notes in (fp.get("cleaned") or {}).items():
        bits.append(f"{name}: {'; '.join(notes)}")
    return " · ".join(bits)


def ensure_dirs() -> None:
    for d in (DATA_DIR, REPORTS_DIR, PLAYBOOK_HISTORY_DIR):
        d.mkdir(parents=True, exist_ok=True)
