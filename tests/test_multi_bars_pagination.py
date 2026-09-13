"""
Regression tests for Alpaca.multi_bars pagination.

The bug this pins: multi_bars issued one request per 100-symbol chunk and
ignored `next_page_token`. Alpaca caps a response by TOTAL bars across all
symbols and orders the payload alphabetically, so any request that spanned
pages returned a complete alphabetical PREFIX and silently dropped the rest.
Callers cannot tell that from "those symbols have no data" — and the runner
scan treats missing bars as a rejection, so the failure would have shown up as
a plausible-looking stand-down rather than an outage.

Measured live before the fix (2026-09-11, 95 symbols, 5Min, 09:30-15:00 ET):
40 of 95 symbols returned, 2289 bars, `next_page_token` set, 55 symbols
silently empty from LABU onward. After: 95 of 95, 5877 bars.

These tests stub _get so they run offline and assert the paging contract
itself rather than any particular day's data.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime

from agent.providers import Alpaca
from agent.util import ET


class _StubAlpaca(Alpaca):
    """Alpaca with the network replaced by a scripted page sequence."""

    def __init__(self, pages):
        self.feed = "sip"
        self.h = {}
        self._pages = list(pages)
        self.calls = []

    def _get(self, path, params=None):  # type: ignore[override]
        self.calls.append(dict(params or {}))
        return self._pages.pop(0) if self._pages else {"bars": {}}


def _bar(minute, price=10.0, vol=100):
    return {"t": f"2026-09-11T{13 + minute // 60:02d}:{minute % 60:02d}:00Z",
            "o": price, "h": price, "l": price, "c": price, "v": vol}


START = datetime.combine(date(2026, 9, 11), dtime(9, 30), tzinfo=ET)
END = datetime.combine(date(2026, 9, 11), dtime(15, 0), tzinfo=ET)


def test_follows_next_page_token_and_merges_symbols():
    """The exact shape of the live failure: page 1 holds an alphabetical
    prefix, page 2 holds the tail. Without paging, the tail is lost."""
    alp = _StubAlpaca([
        {"bars": {"AAA": [_bar(0)], "BBB": [_bar(0)]}, "next_page_token": "tok1"},
        {"bars": {"YYY": [_bar(0)], "ZZZ": [_bar(0)]}, "next_page_token": None},
    ])
    out = alp.multi_bars(["AAA", "BBB", "YYY", "ZZZ"], "5Min", START, END)

    assert sorted(k for k, v in out.items() if v) == ["AAA", "BBB", "YYY", "ZZZ"]
    assert len(alp.calls) == 2
    assert "page_token" not in alp.calls[0]
    assert alp.calls[1]["page_token"] == "tok1"


def test_merges_bars_for_a_symbol_split_across_pages():
    """A symbol straddling a page boundary must end up with BOTH halves, in
    chronological order — not just whichever page landed last."""
    alp = _StubAlpaca([
        {"bars": {"AAA": [_bar(0), _bar(5)]}, "next_page_token": "tok1"},
        {"bars": {"AAA": [_bar(10), _bar(15)]}, "next_page_token": None},
    ])
    out = alp.multi_bars(["AAA"], "5Min", START, END)

    assert len(out["AAA"]) == 4
    times = [b["t"] for b in out["AAA"]]
    assert times == sorted(times)
    assert len({b["t"] for b in out["AAA"]}) == 4  # no duplicates


def test_symbol_with_no_bars_gets_an_empty_list_not_a_missing_key():
    """"Asked and got nothing" must be distinguishable from "never asked."
    The scan's no_bars counter depends on this."""
    alp = _StubAlpaca([{"bars": {"AAA": [_bar(0)]}, "next_page_token": None}])
    out = alp.multi_bars(["AAA", "MISSING"], "5Min", START, END)

    assert "MISSING" in out
    assert out["MISSING"] == []


def test_single_page_response_issues_exactly_one_request():
    """No wasted round trip when everything fits in one page."""
    alp = _StubAlpaca([{"bars": {"AAA": [_bar(0)]}}])
    alp.multi_bars(["AAA"], "5Min", START, END)
    assert len(alp.calls) == 1


def test_page_cap_bounds_a_cyclic_token():
    """A malformed or cyclic token must not spin forever."""
    alp = _StubAlpaca([{"bars": {"AAA": [_bar(0)]}, "next_page_token": "same"}] * 200)
    out = alp.multi_bars(["AAA"], "5Min", START, END)
    assert len(alp.calls) == 50  # the bound, not 200
    assert out["AAA"]


def test_chunks_of_100_each_page_independently():
    """Paging state must reset per 100-symbol chunk, not leak across chunks."""
    alp = _StubAlpaca([
        {"bars": {f"S{i:03d}": [_bar(0)] for i in range(100)}, "next_page_token": None},
        {"bars": {f"S{i:03d}": [_bar(0)] for i in range(100, 150)}, "next_page_token": None},
    ])
    out = alp.multi_bars([f"S{i:03d}" for i in range(150)], "5Min", START, END)

    assert len([k for k, v in out.items() if v]) == 150
    assert len(alp.calls) == 2
    assert "page_token" not in alp.calls[1]
