"""
Alpaca.minute_bars must return the REGULAR session only (9:30 -> 15:59, or
12:59 on an early close). Alpaca stamps a bar at its START time and treats the
`end` bound as INCLUSIVE, so the old `end = 16:00` admitted the first
after-hours bar (the 16:00-16:01 print). Its close then became the "eod" exit
for every hold-to-close outcome, flattering (or deflating) the session stats.

Pinned here by capturing the window `end` the provider actually sends, with the
network replaced so nothing is fetched.
"""
from __future__ import annotations

from datetime import date, datetime

from agent import providers
from agent.config import Config

KEY = "PKABCDEFGHIJ12345678"
SECRET = "a" * 20 + "B" * 20


def _sent_end(monkeypatch, d: date) -> datetime:
    captured = {}

    def _fake_get(self, path, params=None):
        captured["end"] = params["end"]
        return {"bars": []}

    monkeypatch.setattr(providers.Alpaca, "_realtime_refused", False)
    monkeypatch.setattr(providers.Alpaca, "_get", _fake_get)
    cfg = Config(raw={"data": {}}, alpaca_key_id=KEY, alpaca_secret_key=SECRET)
    providers.Alpaca(cfg).minute_bars("AAPL", d)
    return datetime.fromisoformat(captured["end"])


def test_full_session_minute_bars_end_at_last_regular_bar(monkeypatch):
    end = _sent_end(monkeypatch, date(2026, 9, 16))  # Wednesday, full session
    assert end.replace(tzinfo=None) == datetime(2026, 9, 16, 15, 59)


def test_half_day_minute_bars_end_at_1259(monkeypatch):
    end = _sent_end(monkeypatch, date(2026, 11, 27))  # day after Thanksgiving
    assert end.replace(tzinfo=None) == datetime(2026, 11, 27, 12, 59)

