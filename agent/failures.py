"""
Failure classification: is this the desk breaking, or the provider being down?

WHY THIS EXISTS

On 2026-09-22 the evening loop failed three times (runs 64/65/66) with:

    Anthropic API failed after 3 attempts: Error code: 400 -
    {'type': 'invalid_request_error',
     'message': 'Your credit balance is too low to access the Anthropic API...'}

Every one of those rows was logged as `status='error'` with a blank model and
$0.000 cost -- byte-for-byte indistinguishable from a NoneType crash in the
indicator block. The account had simply run out of money. The same error had
already hit `morning` on 09-02 and 09-04 and `weekly` on 09-04, and each time
it was re-diagnosed from scratch.

The project principle at stake: an outage must never be recorded in a way that
reads like a legitimate stand-down, and a billing condition must never read
like a code defect. Those are three different things and they need three
different repairs -- top up the account, fix the bug, or accept the stand-down.
A single `error` status collapses all three.

RETRYABILITY IS THE OTHER HALF

`LLMClient` retries any exception three times with 15/30/45s backoff. For a
rate limit or an overloaded shard that is correct. For "your credit balance is
too low" it is not: the condition cannot resolve within the retry window, and
the 90 seconds of backoff is pure waste -- on Vercel it is 90 seconds out of a
300s function budget, spent guaranteeing the same 400.

So each classification carries BOTH a status and whether retrying could ever
have helped.
"""
from __future__ import annotations

__all__ = ["STATUS_ERROR", "STATUS_API_ERROR", "classify_failure",
           "is_permanent_provider_error", "format_failure"]

import traceback as _traceback

# runs.status is a bare VARCHAR -- the comment in agent/ledger.py's _SCHEMA
# lists the known values but no CHECK constraint enforces them, so adding one
# is schema-safe on both DuckDB and Postgres. (The CHECK the run-lock code
# relies on is on run_locks.status, a different table, and is untouched.)
STATUS_ERROR = "error"          # the desk's own code failed
STATUS_API_ERROR = "api_error"  # the LLM provider was unusable

# Substrings that identify a provider-side condition rather than a defect in
# this codebase. Matched case-insensitively against the full exception text,
# which for LLM failures is LLMClient's
# "Anthropic API failed after N attempts: <underlying error>" wrapper.
_PROVIDER_MARKERS = (
    "anthropic api failed after",
    "rate_limit_error",
    "overloaded_error",
    "authentication_error",
    "permission_error",
    "credit balance is too low",
    "api_error",
    "529",
)

# A provider condition that CANNOT clear inside a retry window. Retrying these
# only burns wall clock -- and on a 300s serverless budget that matters.
# Everything else provider-side (rate limits, overload, transient network) is
# exactly what backoff is for.
_PERMANENT_MARKERS = (
    "credit balance is too low",
    "authentication_error",
    "permission_error",
    "invalid_x_api_key",
    "invalid api key",
)


def _text(exc: BaseException | str) -> str:
    return (exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}").lower()


def is_permanent_provider_error(exc: BaseException | str) -> bool:
    """True when retrying cannot possibly help.

    Deliberately narrow. A false positive here turns a recoverable blip into a
    hard failure, which is strictly worse than wasting a retry -- so anything
    not explicitly listed stays retryable.
    """
    return any(m in _text(exc) for m in _PERMANENT_MARKERS)


def classify_failure(exc: BaseException | str) -> str:
    """Map an exception to the status that should be written to runs.status.

    Returns STATUS_API_ERROR when the provider was the thing that failed, and
    STATUS_ERROR otherwise. When in doubt this returns STATUS_ERROR: mislabeling
    a real bug as an outage would hide it, while mislabeling an outage as a bug
    only costs the investigation this module exists to shorten.
    """
    return STATUS_API_ERROR if any(m in _text(exc) for m in _PROVIDER_MARKERS) else STATUS_ERROR


def format_failure(exc: BaseException) -> str:
    """Exception text plus its traceback, for runs.error.

    `str(e)` alone loses every frame, so a KeyError logged that way says only
    which key -- never which of the several dicts in the data-setup block it
    came from. ledger.log_run truncates to error[:2000], so the type and
    message go FIRST and the traceback follows; a truncated tail still leaves
    the identifying line intact.
    """
    detail = f"{type(exc).__name__}: {exc}"
    tb = _traceback.format_exc()
    if tb and "NoneType: None" not in tb:
        detail = f"{detail}\n{tb}"
    return detail
