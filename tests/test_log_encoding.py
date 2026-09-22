"""
Logging must never be able to fail a loop.

Every loop logs strings the model wrote, and models write en-dashes, arrows
and curly quotes freely. On Windows the console defaults to cp1252, which has
none of them, so `print` raises UnicodeEncodeError -- from the logging call,
not from any work.

That is not hypothetical. A weekly run made its mining call, saved the mined
lesson to the ledger, and then died on `log("weekly", ...)` because the lesson
text contained a U+2192. The expensive work had succeeded and been persisted;
the playbook rewrite that was the entire point of the run never happened. The
run that was about to publish the finding was killed by printing it.
"""
from __future__ import annotations

import io

import pytest

from agent.util import log


class _Cp1252Stdout(io.TextIOBase):
    """A stdout that behaves like a Windows console: strict cp1252."""

    encoding = "cp1252"

    def __init__(self):
        self.written: list[str] = []

    def write(self, s: str) -> int:
        s.encode("cp1252")          # raises exactly as the real console does
        self.written.append(s)
        return len(s)


def test_a_model_written_arrow_does_not_kill_the_run(monkeypatch, capsys):
    """The exact character that took down the weekly loop."""
    monkeypatch.setattr("sys.stdout", _Cp1252Stdout())
    log("weekly", "selection → no edge")          # must not raise


@pytest.mark.parametrize("ch", ["→", "—", "‘", "“", "≥"])
def test_the_usual_model_punctuation_survives(monkeypatch, ch):
    """Arrow, em-dash, curly quotes, >=. All routine model output, none of
    them in cp1252."""
    monkeypatch.setattr("sys.stdout", _Cp1252Stdout())
    log("evening", f"lesson {ch} recorded")


def test_the_line_still_gets_printed_not_swallowed(monkeypatch):
    """Degrade the characters, never the line. A silently dropped log entry
    would hide the very run this protects."""
    out = _Cp1252Stdout()
    monkeypatch.setattr("sys.stdout", out)
    log("weekly", "a → b")
    joined = "".join(out.written)
    assert "[weekly]" in joined
    assert "a " in joined and " b" in joined


def test_ordinary_ascii_is_untouched(monkeypatch):
    out = _Cp1252Stdout()
    monkeypatch.setattr("sys.stdout", out)
    log("morning", "plan saved: 3 picks")
    assert "plan saved: 3 picks" in "".join(out.written)


def test_a_stream_with_no_encoding_attribute_is_handled(monkeypatch):
    """Some captured/piped streams report no encoding at all; the fallback
    must not itself raise."""
    class _NoEncoding(_Cp1252Stdout):
        encoding = None

    monkeypatch.setattr("sys.stdout", _NoEncoding())
    log("runner", "candidate → selected")
