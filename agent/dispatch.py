"""
Hand the rest of a job to a fresh invocation of this same deployment
(specs/MORNING_BUDGET_SPEC_v1.md).

Vercel caps every invocation at 300s (vercel.json maxDuration) and does NOT
cancel a function when its caller disconnects unless cancellation is opted
into (`supportsCancellation` in vercel.json, which this project does not set).
So a request to one of our own /cron/... routes, sent and then abandoned after
a moment, starts a new invocation with a whole budget of its own. That is how
the morning's risk-manager half, and the paired arm's, get their own 300s.

Only on the hosted deployment: locally there is no budget to escape, and a
caller runs the next half in-process instead (`available()` is False).
"""
from __future__ import annotations

import os

__all__ = ["available", "base_url", "fire"]

# Long enough for the request to be delivered; far shorter than any half of
# the morning, which answers only when its work is done.
WAIT_S = 3.0


def base_url(cfg=None) -> str | None:
    """This deployment's public origin. The production domain first (Vercel's
    system variable, else config `public_base_url`): a per-deployment URL can
    sit behind Vercel's deployment protection, so it is the last resort."""
    configured = ""
    try:
        configured = str((cfg.get("public_base_url") if cfg is not None else "") or "")
    except Exception:  # noqa: BLE001
        configured = ""
    host = (os.environ.get("TRADE_AGENT_BASE_URL")
            or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
            or configured
            or os.environ.get("VERCEL_URL") or "").strip().rstrip("/")
    if not host:
        return None
    return host if host.startswith("http") else f"https://{host}"


def available(cfg=None) -> bool:
    """True on the hosted deployment, where a hand-off can reach a route that
    accepts the cron secret."""
    return bool(os.environ.get("VERCEL") and os.environ.get("CRON_SECRET") and base_url(cfg))


def fire(path: str, params: dict | None = None, wait_s: float = WAIT_S, cfg=None) -> dict:
    """GET `path` on this deployment with the cron secret and stop waiting
    after `wait_s`. Returns {"ok": bool, "how": str}.

    A read timeout IS the success case: the target answers only when its work
    is done, minutes later. A quick answer means it had nothing to do (200) or
    refused (anything else). Never raises."""
    base = base_url(cfg)
    secret = os.environ.get("CRON_SECRET", "")
    if not base or not secret:
        return {"ok": False, "how": "no deployment URL or CRON_SECRET in this environment"}
    try:
        import requests
        r = requests.get(base + path, params=params or {}, timeout=(5.0, wait_s),
                         headers={"Authorization": f"Bearer {secret}"})
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "ReadTimeout":
            return {"ok": True, "how": "dispatched"}
        return {"ok": False, "how": f"{type(e).__name__}: {e}"[:300]}
    if r.status_code == 200:
        return {"ok": True, "how": f"answered within {wait_s:.0f}s: {r.text[:300]}"}
    return {"ok": False, "how": f"HTTP {r.status_code}: {r.text[:300]}"}
