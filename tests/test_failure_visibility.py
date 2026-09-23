"""
Pins for the 2026-09-22 evening-loop investigation.

WHAT HAPPENED

Three evening runs (64/65/66) failed with:

    Anthropic API failed after 3 attempts: Error code: 400 -
    {'type': 'invalid_request_error',
     'message': 'Your credit balance is too low to access the Anthropic API...'}

The account had run out of credit. Finding that out took hours, for four
separable reasons, each pinned below:

  1. runs.error was persisted AND served in /api/status, and simply never
     rendered by either dashboard. The answer was one column away the whole
     time.
  2. The loops logged str(e), discarding the traceback -- fine for this error,
     useless for the 'NoneType' object is not subscriptable failure that hit
     the same loop days earlier.
  3. A billing outage was written as status='error', byte-identical to a code
     crash. Same error had already hit morning on 09-02 and 09-04.
  4. The runner reported "Healthy: 4289 symbols scanned, 78 candidates" for a
     session whose selector had died on that same outage.

Conventions follow tests/test_runner_view.py, which pins bugs the same way.
"""
from __future__ import annotations

from datetime import date

import pytest

from agent import failures
from agent.delivery import _error_cell
from agent.runner_view import _health_line


_CREDIT_ERROR = (
    "Anthropic API failed after 3 attempts: Error code: 400 - "
    "{'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'Your credit balance is too low to access the Anthropic API. "
    "Please go to Plans & Billing to upgrade or purchase credits.'}, "
    "'request_id': 'req_011CfKNZA6TzWgHqTBEJyo7S'}"
)


# ------------------------------------------------- 1. the error must be shown

def test_error_cell_renders_the_error_text():
    """The whole investigation existed because this cell was never rendered."""
    out = _error_cell(_CREDIT_ERROR)
    assert "credit balance is too low" in out


def test_error_cell_is_empty_for_a_clean_run():
    """Never the literal strings None/undefined/null -- an unguarded f-string
    would print "None" on every successful row and make the column useless."""
    for empty in (None, "", 0):
        assert _error_cell(empty) == ""


def test_error_cell_escapes_markup():
    """runs.error is raw exception text. Tracebacks carry <module> and generic
    types; unescaped they break the table or swallow the message."""
    out = _error_cell('KeyError in <module> with "x" & y')
    assert "<module>" not in out
    assert "&lt;module&gt;" in out
    assert "&amp;" in out


def test_error_cell_keeps_the_full_text_in_the_title_attribute():
    long_err = "E: " + ("x" * 500)
    out = _error_cell(long_err)
    assert 'title="' in out
    assert "x" * 500 in out          # full text preserved for hover
    assert out.count("x" * 500) == 1  # but only once -- the body is truncated
    assert "&hellip;" in out


def test_render_dashboard_surfaces_a_run_error(monkeypatch):
    """End-to-end pin on the static renderer: given a runs row carrying an
    error, that text must appear in the generated HTML."""
    from agent import delivery, ledger

    monkeypatch.setattr(ledger, "recent_runs", lambda con, n=12: [{
        "loop": "evening", "run_date": date(2026, 9, 22), "status": "api_error",
        "model": "", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
        "error": _CREDIT_ERROR,
    }])
    rows = "".join(
        f"<td>{delivery._error_cell(r['error'])}</td>"
        for r in ledger.recent_runs(None)
    )
    assert "credit balance is too low" in rows


# --------------------------------------------- 2/3. classification + capture

def test_credit_exhaustion_is_classified_as_an_api_error_not_a_code_error():
    assert failures.classify_failure(_CREDIT_ERROR) == failures.STATUS_API_ERROR


def test_credit_exhaustion_is_permanent_and_must_not_be_retried():
    """15+30+45s of backoff was 90 of the 128 seconds each failed run spent,
    re-earning an identical 400 out of a 300s serverless budget."""
    assert failures.is_permanent_provider_error(_CREDIT_ERROR) is True


def test_rate_limits_and_overload_stay_retryable():
    """Narrowness matters: turning a recoverable blip into a hard failure is
    worse than wasting one retry."""
    for retryable in ("rate_limit_error: too many requests",
                      "overloaded_error: server busy",
                      "Connection reset by peer"):
        assert failures.is_permanent_provider_error(retryable) is False


def test_a_real_code_bug_is_not_labelled_an_outage():
    """The 'NoneType' failure that hit this same loop must stay status=error,
    or a genuine defect gets filed as somebody else's outage."""
    err = "'NoneType' object is not subscriptable"
    assert failures.classify_failure(err) == failures.STATUS_ERROR
    assert failures.is_permanent_provider_error(err) is False


def test_format_failure_includes_type_and_message_first():
    """log_run truncates to error[:2000]; the identifying line must survive."""
    try:
        {}["missing"]
    except KeyError as e:
        detail = failures.format_failure(e)
    assert detail.startswith("KeyError:")
    assert "Traceback" in detail


def test_format_failure_without_an_active_exception_is_still_useful():
    detail = failures.format_failure(ValueError("standalone"))
    assert "ValueError: standalone" in detail


# ------------------------------------------- 4. outage must not read healthy

def _session(**over):
    sess = {"universe_size": 4289, "provider_degraded": False,
            "rejected_no_bars": 10, "rejected_too_few_bars": 5,
            "selection_skipped_reason": None}
    sess.update(over)
    return {"session": sess, "gates": {"feed": "iex", "feed_downgraded": True},
            "n_candidates": 78}


def test_selection_failure_is_reported_as_degraded_not_healthy():
    """The exact 2026-09-22 shape: a healthy scan whose selector died on the
    API outage was reported as 'Healthy: 4289 symbols scanned, 78 candidates'."""
    line = _health_line(_session(
        selection_skipped_reason=f"selection_failed: {_CREDIT_ERROR}"))
    assert not line.startswith("Healthy")
    assert "DEGRADED" in line
    assert "SELECTION FAILED" in line
    assert "credit balance is too low" in line


def test_a_legitimate_selection_skip_still_reads_healthy():
    """'candidates <= max_picks' and 'no candidates' are real stand-downs, not
    outages -- over-flagging them would destroy the distinction from the other
    side."""
    for benign in ("candidates <= max_picks", "no candidates",
                   "selector returned no picks"):
        line = _health_line(_session(selection_skipped_reason=benign))
        assert line.startswith("Healthy"), benign


def test_healthy_session_with_no_skip_reason_still_reads_healthy():
    assert _health_line(_session()).startswith("Healthy")
