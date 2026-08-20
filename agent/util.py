"""
Shared utilities: Eastern-time helpers, market calendar, robust JSON extraction, retry.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# NYSE full-day holidays (observed dates).
MARKET_HOLIDAYS: set[str] = {
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def now_et() -> datetime:
    return datetime.now(tz=ET)


def today_et() -> date:
    return now_et().date()


def is_market_day(d: date | None = None) -> bool:
    d = d or today_et()
    return d.weekday() < 5 and d.isoformat() not in MARKET_HOLIDAYS


def prev_market_day(d: date | None = None) -> date:
    d = d or today_et()
    cur = d - timedelta(days=1)
    while not is_market_day(cur):
        cur -= timedelta(days=1)
    return cur


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def log(loop: str, msg: str) -> None:
    print(f"[{now_et().strftime('%H:%M:%S')}] [{loop}] {msg}", flush=True)


def retry(fn: Callable, attempts: int = 3, delay: float = 2.0, label: str = "op") -> Any:
    """Run fn() with simple exponential backoff. Raises the last error."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — provider errors vary widely
            last = e
            if i < attempts - 1:
                time.sleep(delay * (2**i))
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last}")


# ---------------------------------------------------------------- JSON parsing

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _balanced_json_slice(text: str) -> str | None:
    """Return the first balanced {...} block in text, respecting strings."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _repair(s: str) -> str:
    # strip trailing commas before } or ]
    return re.sub(r",\s*([}\]])", r"\1", s)


def extract_json(text: str) -> dict:
    """
    Robustly pull a JSON object out of an LLM response.
    Tries fenced blocks first, then the first balanced brace block.
    """
    candidates: list[str] = []
    for m in _FENCE_RE.finditer(text):
        candidates.append(m.group(1).strip())
    sliced = _balanced_json_slice(text)
    if sliced:
        candidates.append(sliced)
    for cand in candidates:
        for attempt in (cand, _repair(cand)):
            try:
                obj = json.loads(attempt)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    raise ValueError("No parseable JSON object found in model response")


def fmt_pct(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.{digits}f}%"


def fmt_num(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "n/a"
    return f"{x:,.{digits}f}"
