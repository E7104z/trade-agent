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



# ------------------------------------------- the prefetch path (what grading reads)

def _prefetch_end(monkeypatch, d: date) -> datetime:
    """The `end` prefetch_session sends to the batched endpoint. The evening
    loop and `run.py regrade-runner` both prefetch, and minute_bars() serves
    the memo first -- so THIS window, not Alpaca.minute_bars', is the one
    every graded session actually used."""
    captured = {}

    def _fake_multi(self, symbols, timeframe, start, end, **kw):
        captured["end"] = end
        return {}

    monkeypatch.setattr(providers.Alpaca, "_realtime_refused", False)
    monkeypatch.setattr(providers.Alpaca, "multi_bars", _fake_multi)
    monkeypatch.setattr(providers.MarketData, "_session_final", staticmethod(lambda d: True))
    monkeypatch.setattr(providers.MarketData, "minute_bars", lambda self, s, d: [])
    cfg = Config(raw={"data": {}}, alpaca_key_id=KEY, alpaca_secret_key=SECRET)
    providers.MarketData(cfg).prefetch_session(["AAPL"], d, daily=False)
    return captured["end"]


def test_prefetch_ends_at_last_regular_bar(monkeypatch):
    end = _prefetch_end(monkeypatch, date(2026, 9, 16))
    assert end.replace(tzinfo=None) == datetime(2026, 9, 16, 15, 59)


def test_prefetch_half_day_excludes_the_afternoon(monkeypatch):
    # The old 16:00 bound admitted every after-hours bar from 13:00 on.
    end = _prefetch_end(monkeypatch, date(2026, 11, 27))
    assert end.replace(tzinfo=None) == datetime(2026, 11, 27, 12, 59)


def test_both_fetch_paths_share_one_window(monkeypatch):
    for d in (date(2026, 9, 16), date(2026, 11, 27)):
        assert _sent_end(monkeypatch, d) == _prefetch_end(monkeypatch, d) \
            == providers.regular_session_window(d)[1]
