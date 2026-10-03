"""
Trade Agent — Web App (Vercel Python serverless entrypoint)

Single Flask app serving:
  - a password-gated dashboard (login/session)
  - three buttons that trigger the morning/evening/weekly loops synchronously
    and stream back the same log output the CLI would print
  - a JSON status API and a raw playbook endpoint

Deployed with vercel.json rewriting all paths to this one function. State
(ledger, playbook) lives in Neon Postgres via agent.ledger / agent.pgshim —
this process has no persistent local disk.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from typing import Any

from flask import Flask, Response, redirect, request, session, url_for

# The "agent" package lives at the repo root, one level above /api.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import ledger  # noqa: E402
from agent import dashboard as dashboard_lib  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.chartui import CHART_MARKER, chart_embed  # noqa: E402
from agent.mascot import MASCOT_MARKER, mascot_embed  # noqa: E402
from agent.memory import read_playbook  # noqa: E402
from agent.webui import PAGE as _PAGE  # noqa: E402
from agent.util import parse_date, today_et  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "dev-only-insecure-key-set-SECRET_KEY-env-var"

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def json_response(data: dict, status: int = 200, compact: bool = False) -> Response:
    """indent=2 for the endpoints people paste into a browser; compact for the
    ones only the dashboard reads (/api/status was ~255 KB indented, ~185 KB
    compact, and is polled every minute)."""
    body = (json.dumps(data, default=_json_default, separators=(",", ":")) if compact
            else json.dumps(data, default=_json_default, indent=2))
    return Response(body, status=status, mimetype="application/json")


_GZIP_MIN_BYTES = 1400
_GZIP_TYPES = ("application/json", "text/html", "text/plain")


@app.after_request
def _gzip(resp: Response) -> Response:
    """Compress large text responses when the client accepts gzip.

    The dashboard page (with the inlined Arby widget) and /api/status are each
    ~200 KB of highly repetitive text; gzip cuts both by roughly 85-90%.
    On Vercel (VERCEL=1) this steps aside: the platform edge already
    compresses function responses (with brotli where the client supports it),
    so app-level gzip there would only be redundant work. It applies when the
    app is served any other way (`python api/index.py`, another host).
    """
    try:
        if (os.environ.get("VERCEL")
                or resp.status_code < 200 or resp.status_code >= 300
                or resp.direct_passthrough
                or "Content-Encoding" in resp.headers
                or "gzip" not in (request.headers.get("Accept-Encoding") or "").lower()
                or (resp.mimetype or "") not in _GZIP_TYPES):
            return resp
        data = resp.get_data()
        if len(data) < _GZIP_MIN_BYTES:
            return resp
        import gzip
        resp.set_data(gzip.compress(data, compresslevel=6))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
        vary = resp.headers.get("Vary")
        resp.headers["Vary"] = f"{vary}, Accept-Encoding" if vary else "Accept-Encoding"
    except Exception:  # noqa: BLE001 -- compression must never fail a response
        pass
    return resp


def _is_cron_request() -> bool:
    return bool(CRON_SECRET) and request.headers.get("Authorization") == f"Bearer {CRON_SECRET}"


# A narrower secret for the every-5-minutes intraday trigger, which runs on a
# machine outside Vercel (Hobby crons cannot fire more than once a day).
# Unlike CRON_SECRET -- which passes login_required on EVERY endpoint -- this
# one authorizes /cron/intraday-check and nothing else, so the credential that
# has to live on another machine cannot read the ledger or start a paid loop.
INTRADAY_TRIGGER_SECRET = os.environ.get("INTRADAY_TRIGGER_SECRET", "")


def _is_intraday_trigger() -> bool:
    if _is_cron_request():
        return True
    if not INTRADAY_TRIGGER_SECRET:
        return False
    import hmac
    return hmac.compare_digest(request.headers.get("Authorization") or "",
                               f"Bearer {INTRADAY_TRIGGER_SECRET}")


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _is_cron_request():
            return f(*args, **kwargs)
        if not APP_PASSWORD:
            # No password configured — fail closed rather than deploy silently open.
            return json_response(
                {"error": "APP_PASSWORD is not configured on the server — set it in Vercel project env vars."},
                500,
            )
        if not session.get("authed"):
            if request.path.startswith("/api/") or request.method == "POST":
                return json_response({"error": "unauthorized"}, 401)
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------- login

_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Trade Agent — Login</title>
<style>
body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;background:#0d1117;color:#e6edf3;
     display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:32px;width:320px}}
h1{{color:#58a6ff;font-size:20px;margin-top:0}}
input{{width:100%;padding:10px;margin:10px 0;background:#0d1117;border:1px solid #30363d;
       border-radius:6px;color:#e6edf3;box-sizing:border-box}}
button{{width:100%;padding:10px;background:#238636;border:none;border-radius:6px;color:white;
        font-weight:600;cursor:pointer}}
.err{{color:#f85149;font-size:13px}}
</style></head><body>
<div class="card">
  <h1>Trade Agent</h1>
  <form method="post">
    <input type="password" name="password" placeholder="Password" autofocus required>
    <button type="submit">Sign in</button>
  </form>
  {error}
</div></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if APP_PASSWORD and request.form.get("password") == APP_PASSWORD:
            session["authed"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        return _LOGIN_HTML.format(error="<p class='err'>Wrong password.</p>"), 401
    return _LOGIN_HTML.format(error="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ----------------------------------------------------------------- dashboard
# The dashboard UI now lives in agent/webui.py (a multi-page SPA shell),
# imported at the top of this module as _PAGE. The Arby mascot is injected
# at the marker in the block just below.


# Arby the desk mascot: a self-contained widget injected at the marker. It only
# reads /api/status and never touches the run/loop logic above.
_PAGE = _PAGE.replace(MASCOT_MARKER, mascot_embed())
# The strategy-aware chart drawer (specs/CHART_DRAWER_SPEC_v1.md): a second
# self-contained widget injected at its own marker, same pattern as Arby. It
# only reads /api/chart and never touches the run/loop logic above.
_PAGE = _PAGE.replace(CHART_MARKER, chart_embed())


@app.route("/")
@login_required
def dashboard():
    return _PAGE


# ------------------------------------------------------------------------ API

_PICK_BARS_CACHE: dict[tuple[str, str], tuple[float, list]] = {}
_PICK_BARS_TTL_LIVE = 90          # seconds, while the session is trading
_PICK_BARS_TTL_FINAL = 6 * 3600   # a finished session's bars never change


def _pick_bars(picks: list, d) -> dict:
    """Best-effort real intraday bars for the desk monitor chart, keyed by ticker.

    Returns {} on any failure so the mascot falls back to its synthetic trace.
    Cached for a short TTL so the 60s status poll doesn't re-hit the network.

    Skips the fetch entirely before the 09:30 open (and on non-market days):
    there are no bars to get, and asking anyway was the single largest source
    of production errors -- yfinance's "possibly delisted; no price data found"
    for today's picks, dozens of times every morning between the plan landing
    and the open, each one a slow network miss on the status request path.
    """
    if not picks:
        return {}
    from agent.util import is_market_day, market_close_et, now_et
    from datetime import time as _dtime
    now_dt = now_et()
    if d == now_dt.date() and (not is_market_day(d) or now_dt.time() < _dtime(9, 31)):
        return {}
    final = d < now_dt.date() or now_dt.time() >= market_close_et(d)
    ttl = _PICK_BARS_TTL_FINAL if final else _PICK_BARS_TTL_LIVE
    out: dict = {}
    now = time.monotonic()
    for p in picks[:3]:
        sym = (p or {}).get("ticker")
        if not sym:
            continue
        key = (str(sym).upper(), d.isoformat())
        cached = _PICK_BARS_CACHE.get(key)
        # An empty result is only trusted for the live TTL: it may have been a
        # transient miss, and a finished session deserves another try.
        if cached and now - cached[0] < (ttl if cached[1] else _PICK_BARS_TTL_LIVE):
            out[sym] = cached[1]
            continue
        bars: list = []
        try:
            # MarketData: Alpaca first (window clamped to what the data plan
            # permits), yfinance only as the fallback -- yfinance alone was
            # returning "possibly delisted" for perfectly listed names.
            from agent import providers
            rows = providers.MarketData(load_config()).minute_bars(sym, d)
            # "v" rides along so the dashboard widget can overlay a real VWAP
            # line (agent/mascot.py computeVWAP) -- without it the chart would
            # only ever be able to draw candles, never the volume-weighted line.
            bars = [{"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v")} for b in rows]
        except Exception:
            bars = []
        _PICK_BARS_CACHE[key] = (now, bars)
        out[sym] = bars
    return out


def _runner_payload(con, d) -> dict:
    """Today's runner session for the dashboard, or a marker that it never ran.

    Never raises: a failure in this lane must not take down the whole status
    endpoint the rest of the desk depends on.
    """
    try:
        from agent import runner_view
        s = runner_view.session_summary(con, d)
        picks = con.execute(
            """SELECT pick_id, ticker, direction, conviction, size_r,
                      entry_price, stop_price, target_price, thesis
               FROM picks WHERE plan_date = ? AND setup_type = 'dto_runner'
               ORDER BY pick_id""", [d]).fetchall()
        return {
            "ran": bool(s["session"] or s["rows"]),
            "health": runner_view._health_line(s),
            "universe": (s["session"] or {}).get("universe_size") or 0,
            "n_candidates": s["n_candidates"],
            "n_selected": s["n_selected"],
            "n_orders": len(s["orders"]),
            "degraded": bool((s["session"] or {}).get("provider_degraded")),
            "skipped_reason": (s["session"] or {}).get("selection_skipped_reason"),
            # The runner's actual selections, with the thesis the selector wrote
            # — the "WHY THESE WERE PICKED" evidence that render_text shows but
            # the dashboard never did. Flat fields only (no nested entry_plan).
            "picks": [
                {"ticker": p[1], "direction": p[2], "conviction": p[3],
                 "size_r": p[4], "entry_price": p[5], "stop_price": p[6],
                 "target_price": p[7], "thesis": p[8] or ""}
                for p in picks
            ],
            # Rounded for transport only (display precision); the ledger keeps
            # full precision. Hundreds of rows ride every status poll.
            "rows": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}
                     for r in sorted(s["rows"],
                                     key=lambda x: (not x["selected"], x["rank"] or 99,
                                                    x["ticker"]))],
            # Same-day eyeball diagnostic, not a retuning signal -- one day's
            # top N out of hundreds of candidates is noise as training data.
            # Ranked by the move AFTER detection, (close - plan price) / ATR;
            # change_pct (the move already made at detection) rides along as
            # context only. The FDR-corrected, many-session comparison lives in
            # agent/runner_research.selection_edge() and feeds the weekly
            # miner, which is the only thing allowed to write a playbook rule.
            "moves": [
                {"ticker": r["ticker"], "post_move_atr": r["post_move_atr"],
                 "change_pct": r["change_pct"], "r": r["r"],
                 "exit_reason": r["exit_reason"], "invalid_reason": r["invalid_reason"],
                 "selected": r["selected"], "rank": r["rank"]}
                for r in runner_view.biggest_moves(s, 10)
            ],
            # Selected vs field in planned-risk R, with fills, industry units
            # and invalid fills counted -- one session, not evidence.
            "outcomes": runner_view.outcome_summary(s) if s["graded"] else None,
            # Plan vs the construction arms, and continuation after entry --
            # one session, descriptive (RUNNER_TRADE_CONSTRUCTION_v1).
            "construction": runner_view.construction_summary(con, s) if s["graded"] else None,
            "universe_note": runner_view.universe_line(s),
        }
    except Exception as e:
        return {"ran": False, "health": f"runner view unavailable ({e})",
                "rows": [], "n_candidates": 0, "n_selected": 0, "n_orders": 0}


@app.route("/api/status")
@login_required
def api_status():
    con = ledger.connect()
    try:
        cfg = load_config()
        d = today_et()
        plan_row = ledger.plan_for_date(con, d)
        overall = ledger.overall_stats(con)
        by_setup = ledger.stats_by(con, "setup_type")
        streak = ledger.current_streak(con)
        # Each lane priced at its own $/R: the runner lane sizes at
        # runner.dollars_per_r, and valuing its R at the morning rate
        # overstated its P&L several-fold.
        runner_dpr = (cfg.get("runner", {}) or {}).get("dollars_per_r")
        curve = ledger.equity_curve(con, cfg.get("account_size", 100000),
                                    cfg.get("dollars_per_r", 500),
                                    runner_dollars_per_r=runner_dpr)
        runs = ledger.recent_runs(con, 15)
        playbook_text, version = read_playbook(con)
        equity = curve[-1]["equity"] if curve else cfg.get("account_size", 100000)
        streak_str = f"{streak['length']} {streak['kind']}" if streak.get("kind") not in (None, "none") else "—"
        lanes = {}
        for lane in ledger.LANES:
            lane_streak = ledger.current_streak(con, lane=lane)
            lanes[lane] = {
                "overall": ledger.overall_stats(con, lane=lane),
                "streak": (f"{lane_streak['length']} {lane_streak['kind']}"
                           if lane_streak.get("kind") not in (None, "none") else "—"),
                "drawdown": ledger.trailing_weighted_r(
                    con, int(cfg.get("drawdown_window", 10)), lane=lane),
            }
        lock_cols = [c[0] for c in con.execute("DESCRIBE run_locks").fetchall()]
        from agent import health
        return json_response({
            "today": d.isoformat(),
            "plan": plan_row["plan"] if plan_row else None,
            # the DEFINITIVE, resolved pick rows (entry_type always a single flat
            # field here, unlike plan.picks[] which may only carry a nested
            # entry_plan.type) -- this is what the evening grader actually acts on.
            "picks_today": ledger.picks_for_date(con, d),
            "rejections_today": ledger.rejections_for_date(con, d),
            "pick_bars": _pick_bars(
                (plan_row["plan"] or {}).get("picks", []) if plan_row else [], d
            ),
            "overall": overall,
            # Per-lane record: the morning lane's streak/drawdown here are the
            # exact numbers its mechanical risk rules read.
            "lanes": lanes,
            "drawdown_pause_r": float(cfg.get("drawdown_pause_r", -4.0)),
            # Is every stage of the learning loop actually completing?
            "loop_health": health.loop_health(con, cfg),
            # Runner lane: the whole session, so the dashboard can show what was
            # scanned, what was picked, and - critically - whether a thin
            # candidate list was the market or a broken feed.
            "runner": _runner_payload(con, d),
            "by_setup": by_setup,
            "streak": streak_str,
            "streak_detail": streak,
            "equity": equity,
            "equity_curve": curve[-120:],
            # Conclusions & Changes: everything the weekly miner and evening
            # grader concluded, read back so a human can see the current best
            # edge, the active lessons, the shadow-ledger cost, the calibration
            # and management verdicts, and the FDR-corrected runner selection
            # edge — the same numbers that actually drive future playbooks.
            "conclusions": dashboard_lib.conclusions_payload(con),
            # Arby parity: the static dashboard inlines these as boot data, so
            # the hosted mascot gets the identical contract (playbook_bullets
            # is the only one its JS reads directly; the rest are room dressing
            # kept in sync so the two surfaces can't drift).
            "playbook_bullets": dashboard_lib.playbook_bullets(playbook_text),
            "playbook_html": dashboard_lib.playbook_html(playbook_text),
            "runs": runs,
            "run_locks": [dict(zip(lock_cols, row))
                         for row in con.execute(
                             "SELECT * FROM run_locks WHERE trading_date = ? ORDER BY run_type", [d]
                         ).fetchall()],
            "playbook": playbook_text,
            "playbook_version": version,
        }, compact=True)
    except Exception as e:  # noqa: BLE001
        return json_response({"error": str(e), "trace": traceback.format_exc()}, 500)
    finally:
        con.close()


_PAPER_CACHE: dict = {"at": 0.0, "data": None}
_PAPER_TTL = 20  # seconds


def _num(v):
    try:
        return round(float(v), 4) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@app.route("/api/paper")
@login_required
def api_paper():
    """The live Alpaca PAPER account as the broker sees it, next to the
    ledger's own live-order rows: is the desk actually trading, what is open
    right now, and is anything managing it intraday. Read-only; cached briefly
    because the dashboard polls."""
    cfg = load_config()
    live = cfg.get("live_paper", {}) or {}
    rcfg = cfg.get("runner", {}) or {}
    now = time.monotonic()
    cached = _PAPER_CACHE.get("data")
    if cached is not None and now - _PAPER_CACHE["at"] < _PAPER_TTL:
        return json_response(cached, compact=True)
    out: dict = {"enabled": bool(live.get("enabled")),
                 "runner_orders_enabled": bool(rcfg.get("live_orders_enabled")),
                 "breakout_two_step": bool(live.get("breakout_two_step"))}
    if not out["enabled"]:
        return json_response(out, compact=True)
    from agent import live_broker
    acct = live_broker.get_account(cfg=cfg)
    if acct is None:
        out["error"] = live_broker.last_error() or "account not readable"
        if live_broker.auth_rejected():
            # What the deployment is actually sending, described without the
            # secret: source, lengths, key type, what was cleaned off a paste.
            from agent.config import alpaca_credential_fingerprint, describe_alpaca_fingerprint
            out["credentials"] = describe_alpaca_fingerprint(alpaca_credential_fingerprint(cfg))
    else:
        out["account"] = {k: _num(acct.get(k)) for k in
                          ("equity", "last_equity", "cash", "buying_power")}
        out["account"]["status"] = acct.get("status")
    positions = live_broker.list_positions(cfg=cfg) or []
    out["positions"] = [{
        "symbol": p.get("symbol"), "qty": _num(p.get("qty")), "side": p.get("side"),
        "avg_entry_price": _num(p.get("avg_entry_price")),
        "current_price": _num(p.get("current_price")),
        "unrealized_pl": _num(p.get("unrealized_pl")),
        "unrealized_plpc": _num(p.get("unrealized_plpc"))} for p in positions]
    out["orders"] = [{
        "symbol": o.get("symbol"), "side": o.get("side"), "qty": _num(o.get("qty")),
        "type": o.get("type"), "order_class": o.get("order_class") or "simple",
        "status": o.get("status"), "submitted_at": o.get("submitted_at"),
        "filled_avg_price": _num(o.get("filled_avg_price")),
        "limit_price": _num(o.get("limit_price")), "stop_price": _num(o.get("stop_price")),
        "legs": [{"type": leg.get("type"), "status": leg.get("status"),
                  "filled_avg_price": _num(leg.get("filled_avg_price"))}
                 for leg in (o.get("legs") or [])],
    } for o in live_broker.recent_orders(25, cfg=cfg)]
    con = ledger.connect()
    try:
        d = today_et()
        out["ledger_today"] = ledger.live_orders_for_date(con, d)
        for row in out["ledger_today"]:
            row.pop("raw_order_json", None)
        sess = ledger.runner_session(con, d) or {}
        out["runner_note"] = sess.get("live_orders_note")
        hb = ledger.last_beat(con, "intraday_check")
        out["heartbeat"] = hb
        if hb and hb.get("last_at"):
            # Age computed here: last_at is naive ET, and a browser in another
            # timezone would parse it an hour or more off.
            from agent.util import now_et
            out["heartbeat_age_min"] = round(
                (now_et().replace(tzinfo=None) - hb["last_at"]).total_seconds() / 60, 1)
    finally:
        con.close()
    _PAPER_CACHE.update(at=now, data=out)
    return json_response(out, compact=True)


@app.route("/api/playbook")
@login_required
def api_playbook():
    con = ledger.connect()
    try:
        text, version = read_playbook(con)
        return Response(text, mimetype="text/plain",
                        headers={"X-Playbook-Version": version})
    finally:
        con.close()


@app.route("/api/history/dates")
@login_required
def api_history_dates():
    """Return every session date represented in the historical ledger tables."""
    con = ledger.connect()
    try:
        dates = set()
        date_sources = {
            "picks": "plan_date",
            "rejections": "plan_date",
            "daily_plans": "plan_date",
            "regime_history": "date",
            "lessons": "created_date",
            "calibration": "session_date",
            "rule_confirmations": "session_date",
            "open_question_progress": "session_date",
            "runs": "run_date",
            "run_locks": "trading_date",
            "plan_runs": "trading_date",
        }
        for table, column in date_sources.items():
            try:
                rows = con.execute(
                    f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"
                ).fetchall()
                dates.update(row[0].isoformat() for row in rows if row[0] is not None)
            except Exception:
                # Historical browsing should remain available if an older hosted
                # database is missing an optional v3 table.
                continue
        return json_response({"dates": sorted(dates, reverse=True)})
    finally:
        con.close()


# --------------------------------------------------------- raw table access
#
# A generic, read-only, whitelisted browser for every ledger table -- so any
# question like "what did today's MARA pick actually store for entry_type"
# can be answered straight from the app (GET request, or paste the URL into
# a browser) instead of needing a local DATABASE_URL + CLI. Table names are
# whitelisted (never interpolate a caller-controlled table name without one --
# see agent/ledger.py stats_by for the same pattern/lesson). Only SELECT is
# ever built here; there is no way to reach this with a write.

# table -> its natural date column (None if it has none / isn't date-filterable)
_QUERYABLE_TABLES = {
    "picks": "plan_date",
    "outcomes": "eval_date",
    "rejections": "plan_date",
    "daily_plans": "plan_date",
    "lessons": "created_date",
    "lesson_confirmations": "session_date",
    "regime_history": "date",
    "runs": "run_date",
    "calibration": "session_date",
    "rule_confirmations": "session_date",
    "open_question_progress": "session_date",
    "runner_candidates": "session_date",
    "runner_candidate_outcomes": "session_date",
    "runner_sessions": "session_date",
    # RUNNER_EXIT_SPEC_v1: each triggered candidate under every exit policy.
    "runner_exit_replays": "session_date",
    "live_orders": "session_date",
    # The integrity layer's own state (INTEGRITY_REPAIR_SPEC_v1 Phase 1).
    # Without these, whether a false survivor was recorded, or whether any
    # rule is registered at all, could not be checked from production.
    "research_rules": "discovered_at",
    "research_observations": "session_date",
    "research_looks": "session_date",
    "selection_edge_history": "week_ending",
    "population_universe": "plan_date",
    "population_outcomes": "plan_date",
    "paired_arm_picks": "plan_date",
    # WRITE_TIME_ENFORCEMENT_SPEC_v1: every weekly integrity finding and what
    # the system did about it, including the withheld text.
    "playbook_violations": "run_date",
    # DATA_CONTINUITY_SPEC_v1: per-field grading coverage, and the provider
    # alerts already emailed.
    "data_coverage": "session_date",
    "alerts": "alert_date",
    # GRADER_GROUNDING_SPEC_v1: what the grounding checks refused, and dated
    # lesson contradictions.
    "grader_rejections": "session_date",
    "lesson_contradictions": "session_date",
    # EVIDENCE_BACKLOG_SPEC_v1: the nightly evidence backlog (counts only) and
    # the daily diagnostics digest.
    "evidence_backlog": "session_date",
    "daily_diagnostics": "session_date",
    # TRADERS_LOG_SPEC_v1: the owner's daily log, browsable here. The agent
    # never reads it (tests/test_traders_log_firewall.py).
    "traders_log": "session_date",
    # Not date-filterable, but ordered newest-first (see _TABLE_ORDER) so
    # ?limit=N returns the latest: a lost weekly rewrite is recovered from
    # llm_archive, and every playbook the miner ever wrote is in
    # playbook_versions.
    "llm_archive": None,
    "playbook_versions": None,
}
_TABLE_ORDER = {"llm_archive": "saved_at", "playbook_versions": "version"}


@app.route("/api/table")
@login_required
def api_table_index():
    """Lists every queryable table + its columns, so you can discover what's
    available without already knowing the schema."""
    con = ledger.connect()
    try:
        out = {}
        for name in sorted(_QUERYABLE_TABLES):
            out[name] = [c[0] for c in con.execute(f"DESCRIBE {name}").fetchall()]
        return json_response({"tables": out,
                              "usage": "/api/table/<name>?date=YYYY-MM-DD&ticker=XXX&since=YYYY-MM-DD&limit=N"})
    finally:
        con.close()


@app.route("/api/table/<name>")
@login_required
def api_table(name: str):
    if name not in _QUERYABLE_TABLES:
        return json_response(
            {"error": f"unknown or non-whitelisted table {name!r}",
             "available": sorted(_QUERYABLE_TABLES)}, 404)
    con = ledger.connect()
    try:
        date_col = _QUERYABLE_TABLES[name]  # trusted constant, not user input
        cols = [c[0] for c in con.execute(f"DESCRIBE {name}").fetchall()]
        where: list[str] = []
        params: list = []
        if date_col and request.args.get("date"):
            where.append(f"{date_col} = ?")
            params.append(parse_date(request.args["date"]))
        if date_col and request.args.get("since"):
            where.append(f"{date_col} >= ?")
            params.append(parse_date(request.args["since"]))
        if "ticker" in cols and request.args.get("ticker"):
            where.append("ticker = ?")
            params.append(request.args["ticker"].strip().upper())
        try:
            limit = max(1, min(int(request.args.get("limit", 200)), 1000))
        except ValueError:
            limit = 200
        sql = f"SELECT * FROM {name}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        order_col = date_col or _TABLE_ORDER.get(name)  # trusted constants only
        if order_col:
            sql += f" ORDER BY {order_col} DESC"
        sql += f" LIMIT {limit}"
        rows = con.execute(sql, params).fetchall()
        return json_response({"table": name, "columns": cols, "n": len(rows),
                              "rows": [dict(zip(cols, r)) for r in rows]})
    except Exception as e:  # noqa: BLE001
        return json_response({"error": str(e), "trace": traceback.format_exc()}, 500)
    finally:
        con.close()


# ------------------------------------------------------------------ run loops

_VALID_LOOPS = {"morning", "evening", "weekly", "runner"}


def _weekly_run_date():
    """The session a weekly mining run is filed under: today on a market day,
    otherwise the most recent session. A Saturday run therefore records the
    same Friday week_ending the Friday-evening chained run used to."""
    from agent.util import is_market_day, prev_market_day
    today = today_et()
    return today if is_market_day(today) else prev_market_day(today)


def _execute_loop(loop_name: str, cfg, force: bool) -> dict:
    buf = io.StringIO()
    error = None
    try:
        with contextlib.redirect_stdout(buf):
            if loop_name == "morning":
                from agent.loops import morning
                morning.run(cfg, force=force)
            elif loop_name == "evening":
                from agent.loops import evening
                evening.run(cfg, force=force)
            elif loop_name == "runner":
                # Runs the scan SERVER-SIDE, so its evidence lands in the same
                # Postgres the dashboard reads. Running it locally writes to a
                # local DuckDB the hosted dashboard cannot see -- which is the
                # entire reason the Runner card kept showing "no scan recorded"
                # while the lane was in fact working fine on the local machine.
                from agent.loops import runner as runner_loop
                from agent.util import now_et
                result = runner_loop.tick(cfg, at=now_et())
                print(json.dumps(result, indent=2, default=str))
            else:
                from agent.loops import weekly
                weekly.run(cfg, run_date=_weekly_run_date())
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        buf.write(f"\n\n{traceback.format_exc()}")
    result = {"loop": loop_name, "log": buf.getvalue(), "error": error}
    if not error and "Duplicate trigger suppressed" in result["log"]:
        con = ledger.connect()
        try:
            existing = ledger.plan_for_date(con, today_et()) if loop_name in ("morning", "evening") else None
            result["duplicate_suppressed"] = True
            result["existing_result"] = existing["plan"] if existing else None
        finally:
            con.close()
    return result


@app.route("/run/<loop_name>", methods=["POST"])
@login_required
def run_loop(loop_name: str):
    """Manual button click from the dashboard — mock/offline/force are user-controlled."""
    if loop_name not in _VALID_LOOPS:
        return json_response({"error": f"unknown loop '{loop_name}'"}, 400)
    cfg = load_config()
    cfg.mock = request.args.get("mock") == "1"
    cfg.offline = request.args.get("offline") == "1"
    force = request.args.get("force") == "1"
    return json_response(_execute_loop(loop_name, cfg, force))


# Vercel Cron only sends GET requests, authenticated via the Authorization:
# Bearer <CRON_SECRET> header it attaches automatically. These always run for
# real (no mock/offline) and never force — they respect the market-day check.
#
# Vercel Hobby restricts cron FREQUENCY (at most once per day per job), not the
# NUMBER of jobs -- an earlier comment here claimed a hard max of two, which was
# wrong and cost this project a working intraday trigger for days. Three
# once-per-day crons deploy fine (morning / evening / runner, verified
# 2026-09-18). A "*/5 13-20 * * 1-5" expression IS rejected, with the explicit
# reason that it "would run more than once per day".
#
# The weekly miner has its OWN cron (Saturday). It used to run chained after
# Friday's evening grading inside the same request -- one 300s function budget
# for a full grading pass plus the single most expensive call this system
# makes (Opus + thinking over the whole ledger). A run killed at the budget
# never reaches its except-block: the week's playbook rewrite is lost and the
# lock is left 'in_progress'. A separate invocation gives the miner a whole
# budget of its own.
#
# Cron schedules are fixed UTC with no DST awareness, and on the Hobby plan a
# job fires at its scheduled minute PLUS a fixed offset of up to ~59 minutes
# (observed: "45 10" fires ~11:11 UTC, "30 14" fires ~15:18 UTC). So we
# sanity-check the current ET hour before spending API money. Windows are wide
# enough to accept the one-hour EST/EDT drift (morning fires ~7:11 ET in
# summer, ~6:11 in winter).
#
# runner: "30 14" lands ~11:18 ET in summer but ~10:18 ET in winter -- BEFORE
# scan_start_et (10:20) and the 10:30 decision time, which would stand the
# lane down every day from November to March. "runner-late" ("30 15") lands
# 11:30-12:29 ET in summer and 10:30-11:29 in winter, so every season has at
# least one tick inside the scan window and past the decision time. Two ticks
# are safe by construction: candidates dedupe per session and selection is
# claimed once per session in the database.
_CRON_WINDOWS = {"morning": (5, 10), "evening": (15, 19), "runner": (9, 12),
                 "runner-late": (9, 12), "weekly": (6, 14)}
_CRON_LOOP = {"morning": "morning", "evening": "evening", "runner": "runner",
              "runner-late": "runner", "weekly": "weekly"}


@app.route("/cron/<loop_name>", methods=["GET"])
@login_required
def cron_loop(loop_name: str):
    if loop_name not in _CRON_LOOP:
        return json_response({"error": f"unknown cron loop '{loop_name}'"}, 400)
    if not _is_cron_request():
        return json_response({"error": "this endpoint is for Vercel Cron only"}, 403)

    from agent.util import now_et
    now = now_et()
    lo, hi = _CRON_WINDOWS[loop_name]
    if not (lo <= now.hour <= hi):
        return json_response({
            "loop": loop_name, "skipped": True,
            "reason": f"current ET hour {now.hour} outside expected window {lo}-{hi} "
                      "(likely a DST schedule drift) — refusing to run automatically",
        })

    cfg = load_config()
    result = _execute_loop(_CRON_LOOP[loop_name], cfg, force=False)
    result["cron"] = loop_name
    return json_response(result)


# GET as well as POST: Vercel Cron issues GET and sends
# "Authorization: Bearer $CRON_SECRET" automatically. POST is retained so
# the GitHub Actions workflow (and any external caller) keeps working.
@app.route("/cron/intraday-check", methods=["GET", "POST"])
def intraday_check():
    if not _is_intraday_trigger():
        return json_response({"error": "unauthorized"}, 401)
    cfg = load_config()
    from agent.live_monitor import check
    # The live position monitor runs FIRST and unguarded. It is the job this
    # endpoint exists for; if it raises, the request should fail loudly.
    monitor = check(cfg)
    # Spec §1.4 nests the monitor result under "monitor". Its existing keys are
    # ALSO kept at the top level: this response is an established contract and
    # R-3 is additive-only, so re-shaping it would be a behavior change smuggled
    # in under a feature flag — and one that only shows up in production, since
    # the GitHub Actions cron discards the body.
    result = dict(monitor)
    result["monitor"] = monitor
    # The runner lane runs second, inside try/except. This is required, not
    # defensive habit: the runner lane must never be able to break the live
    # position monitor, and a monitor that stops running is a real position
    # left unwatched.
    from agent.util import is_market_day, log
    # Positions a missed end-of-day flatten left open overnight are closed on
    # the first tick of the next session. Guarded like the runner: it must
    # never break the monitor that already ran above.
    if ((cfg.get("live_paper", {}) or {}).get("enabled") and is_market_day(today_et())):
        try:
            from agent.live_monitor import carry_cleanup
            con = ledger.connect()
            try:
                result["carry"] = carry_cleanup(con, cfg, today_et())
            finally:
                con.close()
        except Exception as e:
            log("intraday", f"WARN carry cleanup failed; monitor unaffected: {e}")
            result["carry"] = {"error": str(e)}
        # Morning limit/breakout picks go live at their entry window (09:45 by
        # default), not on the opening print. Guarded like the runner.
        try:
            from agent.live_monitor import place_deferred_orders
            from agent.util import now_et
            con = ledger.connect()
            try:
                result["deferred"] = place_deferred_orders(con, cfg, today_et(), now_et())
            finally:
                con.close()
        except Exception as e:
            log("intraday", f"WARN deferred order placement failed; monitor unaffected: {e}")
            result["deferred"] = {"error": str(e)}
    if (cfg.get("runner", {}) or {}).get("enabled"):
        try:
            from agent.loops.runner import tick
            result["runner"] = tick(cfg)
        except Exception as e:
            log("runner", f"WARN runner tick failed; monitor unaffected: {e}")
            result["runner"] = {"error": str(e)}
    # A broker 401 is the credentials, not an order: every live position is
    # unmanaged until the key pair is fixed, so the owner hears about it once.
    try:
        from agent import live_broker
        if live_broker.auth_rejected():
            from agent.failures import alert_alpaca_auth
            con = ledger.connect()
            try:
                alert_alpaca_auth(con, cfg, "intraday", "paper broker (/v2 trading API)")
            finally:
                con.close()
    except Exception as e:  # noqa: BLE001 -- an alert never breaks the monitor
        log("intraday", f"WARN credential alert failed: {e}")
    # Heartbeat: the dashboard's only proof that something is actually
    # managing live positions intraday.
    try:
        con = ledger.connect()
        try:
            ledger.beat(con, "intraday_check", json.dumps(
                {"monitor": {k: monitor.get(k) for k in ("skipped", "reason", "updated")},
                 "runner": {k: (result.get("runner") or {}).get(k)
                            for k in ("skipped", "candidates", "universe_size", "error")}},
                default=str))
        finally:
            con.close()
    except Exception as e:
        log("intraday", f"WARN heartbeat not recorded: {e}")
    return json_response(result)


_CHART_CACHE: dict[tuple, tuple[float, dict]] = {}
_CHART_TTL_LIVE = 20     # seconds -- Phase 5 live polling cadence
_CHART_TTL_FINAL = 6 * 3600
_TICKER_RE = re.compile(r"^[A-Z.]{1,10}$")


@app.route("/api/chart")
@login_required
def api_chart():
    """Chart drawer payload (specs/CHART_DRAWER_SPEC_v1.md Phase 1.2).

    Any exception during payload construction is caught by
    chart_payload.build() itself and surfaced through `warnings` with a 200 --
    the drawer must never blank on a transient data problem. Only bad INPUT
    (this endpoint's job, not build()'s) is a 400.
    """
    from agent import chart_payload
    from agent.util import is_market_day, market_close_et, now_et

    ticker = (request.args.get("ticker") or "").strip().upper()
    if not _TICKER_RE.match(ticker):
        return json_response({"error": "ticker must match ^[A-Z.]{1,10}$"}, 400)
    try:
        d = parse_date(request.args["date"]) if request.args.get("date") else today_et()
    except Exception:
        return json_response({"error": "invalid date"}, 400)
    lane = request.args.get("lane") or None
    if lane not in (None, "morning", "shadow", "runner", "live"):
        return json_response({"error": f"unknown lane {lane!r}"}, 400)
    raw_id = request.args.get("id")
    ctx_id: Any = None
    if raw_id is not None:
        try:
            ctx_id = int(raw_id)
        except ValueError:
            ctx_id = raw_id  # runner id is "TICKER:DATE"; live id is a broker order id string
    try:
        tf = int(request.args.get("tf", 5))
    except ValueError:
        tf = 5
    if tf not in (1, 5, 15):
        return json_response({"error": "tf must be 1, 5, or 15"}, 400)
    asof = request.args.get("asof") == "1"

    now_dt = now_et()
    final = d < now_dt.date() or (is_market_day(d) and now_dt.time() >= market_close_et(d)) or not is_market_day(d)
    kind = "final" if final else "live"
    cache_key = (ticker, d.isoformat(), lane, str(ctx_id), tf, asof, kind)
    ttl = _CHART_TTL_FINAL if final else _CHART_TTL_LIVE
    now = time.monotonic()
    cached = _CHART_CACHE.get(cache_key)
    if cached and now - cached[0] < ttl and cached[1].get("bars"):
        return json_response(cached[1], compact=True)

    con = ledger.connect()
    try:
        payload = chart_payload.build(con, load_config(), ticker, d, lane, ctx_id, tf=tf, asof=asof)
    finally:
        con.close()
    _CHART_CACHE[cache_key] = (now, payload)
    return json_response(payload, compact=True)


@app.route("/api/runner/candidates")
@login_required
def api_runner_candidates():
    """Candidates for one session, alphabetical (spec R-1: detection must not
    rank). Defaults to today."""
    con = ledger.connect()
    try:
        d = parse_date(request.args["date"]) if request.args.get("date") else today_et()
        rows = ledger.runner_candidates_for_date(con, d)
        outcomes = {o["ticker"]: o for o in ledger.runner_outcomes_for_date(con, d)}
        for r in rows:
            r["outcome"] = outcomes.get(r["ticker"])
        return json_response({"date": d.isoformat(), "count": len(rows),
                              "session": ledger.runner_session(con, d),
                              "candidates": rows})
    finally:
        con.close()


@app.route("/api/runner/sessions")
@login_required
def api_runner_sessions():
    """Per-session scan telemetry — the stand-down-vs-outage discriminator.

    A zero-candidate day with universe_size 0 or provider_degraded true is a
    pipeline failure; a zero-candidate day with a full universe and a high
    rejected_conditions is a genuine stand-down. Vercel keeps logs about an
    hour, so this table is the durable record.
    """
    con = ledger.connect()
    try:
        try:
            limit = max(1, min(int(request.args.get("limit", 40)), 400))
        except ValueError:
            limit = 40
        return json_response({"sessions": ledger.recent_runner_sessions(con, limit)})
    finally:
        con.close()


# Local dev convenience: `python api/index.py` runs a normal Flask dev server.
if __name__ == "__main__":
    app.run(debug=True, port=5000)
