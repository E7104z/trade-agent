"""
Runner selection and live paper placement (RUNNER_PORT_SPEC_v1 2.1-2.5).

One LLM call per session, at most. Everything else in this module is
deterministic: the candidate table, the validation, the pick rows, the sizing,
and the order placement.

The three rules that shape this file:

  R-0  Deterministic code computes every number. The selector receives computed
       features and chooses among them; it never derives an indicator, a ratio,
       or a threshold.

  R-1  Detection must not rank. The table is sorted ALPHABETICALLY. Selection
       quality is the quantity being measured, so handing the selector a
       pre-ranked list would contaminate the measurement with the ranker.

  R-2  DTO's conclusions do not port. No win rate, no Golden Basket, no
       _scoreSignal weighting, and no "RVOL 3-5x is the sweet spot" reaches the
       prompt. The selector must not be handed an uncorrected prior.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from . import ledger, live_broker
from .config import Config
from .llm import LLMClient, load_prompt, prompt_version
from .util import log, now_et

__all__ = ["run_selection", "build_candidate_table", "parse_selection"]

# Columns sent to the selector, in order (spec 2.2). Deliberately excludes
# anything that would rank the list for it.
TABLE_COLUMNS = [
    "ticker", "detection_time_et", "detection_minutes", "price", "price_tier",
    "vwap_pct_above", "ema9_slope", "rvol", "trend_consistency", "change_pct",
    "float_m", "stop_dist", "stop_dist_pct", "target_price",
]


def _fmt(v, digits: int = 2) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def build_candidate_table(candidates: list[dict]) -> str:
    """Pipe-delimited table, one row per candidate, alphabetical by ticker.

    A compact table rather than prose per candidate: prose multiplies token
    cost for no analytical gain, and it invites the model to treat the longest
    description as the most important name.
    """
    lines = [" | ".join(TABLE_COLUMNS)]
    for c in sorted(candidates, key=lambda r: r["ticker"]):
        price = c.get("detection_price")
        stop_dist = c.get("stop_dist")
        stop_dist_pct = (stop_dist / price * 100) if (stop_dist and price) else None
        fp = c.get("first_pass_et")
        det_time = fp.strftime("%H:%M") if isinstance(fp, datetime) else ""
        lines.append(" | ".join([
            c["ticker"],
            det_time,
            _fmt(c.get("detection_minutes"), 0),
            _fmt(price),
            c.get("price_tier") or "",
            _fmt(c.get("vwap_pct_above")),
            _fmt(c.get("ema9_slope"), 4),
            _fmt(c.get("rvol")),
            _fmt(c.get("trend_consistency"), 1),
            _fmt(c.get("change_pct")),
            _fmt(c.get("float_shares"), 1),
            _fmt(stop_dist),
            _fmt(stop_dist_pct),
            _fmt(c.get("target_price")),
        ]))
    return "\n".join(lines)


def parse_selection(text: str, valid_tickers: set[str]) -> tuple[list[dict], list[str]]:
    """Parse the selector's JSON defensively. Returns (picks, hallucinated).

    Strips markdown fences first, per the established pattern. Any ticker not
    in the candidate list is a hallucination: dropped and counted, never
    traded. Raises ValueError when nothing parseable comes back, so the caller
    can record a no-trade session rather than guess.
    """
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            raise ValueError("no JSON object in selector response")
        data = json.loads(m.group(0))

    raw = data.get("picks")
    if raw is None or not isinstance(raw, list):
        raise ValueError("selector response has no 'picks' list")

    picks: list[dict] = []
    hallucinated: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        if ticker not in valid_tickers:
            hallucinated.append(ticker)
            continue
        if ticker in seen:
            continue
        seen.add(ticker)
        try:
            conviction = int(item.get("conviction") or 0)
        except (TypeError, ValueError):
            conviction = 0
        try:
            rank = int(item.get("rank") or 0)
        except (TypeError, ValueError):
            rank = 0
        picks.append({
            "ticker": ticker,
            "rank": rank,
            "conviction": max(1, min(5, conviction)) if conviction else None,
            "rationale": str(item.get("rationale") or "")[:500],
        })

    # Re-rank densely on the model's stated order, so a skipped or duplicated
    # rank cannot leave holes in selection_rank.
    picks.sort(key=lambda p: (p["rank"] if p["rank"] else 999))
    for i, p in enumerate(picks, start=1):
        p["rank"] = i
    return picks, hallucinated


NO_RUNNER_RULES = "(no validated runner rules yet — do not assume any)"


def runner_playbook_section(text: str) -> str:
    """The runner section of a playbook document, exactly as the selector sees it.

    Pure text-in/text-out so the weekly loop's write-time check can call the
    very function the selector calls. A checker that re-implemented this
    extraction could happily pass a section the selector never finds — which is
    the failure that kept this lane reading the placeholder while the research
    layer computed a full runner block every week and nothing consumed it.

    If no runner section exists, say so plainly rather than returning the whole
    playbook: an unrelated section is noise the selector may try to apply to a
    setup it was never about.
    """
    text = text or ""
    for line in text.splitlines():
        if re.match(r"^#{1,3}\s", line) and "runner" in line.lower():
            idx = text.index(line)
            rest = text[idx:]
            nxt = re.search(r"\n#{1,3}\s", rest[len(line):])
            return rest[: len(line) + nxt.start()] if nxt else rest
    return NO_RUNNER_RULES


def _playbook_section(con=None) -> str:
    """The runner section of the live playbook. Read-only — only the weekly
    miner writes playbook.md, and that writer isolation is unchanged."""
    try:
        from .memory import read_playbook
        text, _version = read_playbook(con)
    except Exception:
        return "(no playbook available)"
    return runner_playbook_section(text)


def save_runner_pick(con, session_date: date, c: dict, sel: dict,
                     cfg: Config) -> int | None:
    """Insert one runner pick into `picks`.

    Deliberately NOT via save_daily_plan: that function deletes every pick for
    the date before inserting, which would destroy the morning plan. Runner
    picks are appended alongside it.

    setup_type is "dto_runner" and that is load-bearing — it keeps runner
    evidence in its own bucket for the integrity layer so runner statistics can
    never silently merge into the existing setups' numbers.
    """
    rcfg = cfg.get("runner", {}) or {}
    row = con.execute(
        """INSERT INTO picks
           (plan_date, ticker, direction, setup_type, catalyst, thesis, conviction,
            size_r, entry_type, entry_price, stop_price, target_price,
            regime_tag, prompt_version, playbook_version, in_scan_universe)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           RETURNING pick_id""",
        [
            session_date, c["ticker"].upper(), "long", "dto_runner",
            "intraday runner detection",
            sel.get("rationale") or "",
            sel.get("conviction"),
            # Sizing discipline (spec 2.5): every runner pick is sized at the
            # configured minimum until this setup clears n>=8 under the
            # integrity layer. Conviction is RECORDED as a research dimension
            # and does NOT scale size — size scales only on validated forward
            # evidence from this desk's own ledger.
            float(rcfg.get("size_r", cfg.get("min_size_r", 0.25))),
            "open",
            c.get("detection_price"), c.get("stop_price"), c.get("target_price"),
            _regime_tag(con, session_date),
            prompt_version("runner_selector"),
            _playbook_version(con),
            False,
        ],
    ).fetchone()
    return row[0] if row else None


def _regime_tag(con, d: date) -> str:
    try:
        row = con.execute(
            "SELECT regime_tag FROM daily_plans WHERE plan_date = ?", [d]).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return ""


def _playbook_version(con=None) -> str:
    """read_playbook returns (markdown, version); only the version is wanted
    for pick provenance."""
    try:
        from .memory import read_playbook
        _text, version = read_playbook(con)
        return version or ""
    except Exception:
        return ""


def _place_live_orders(cfg: Config, con, session_date: date,
                       placements: list[tuple[dict, int]]) -> dict:
    """Place paper bracket orders for selected runner picks.

    Gated on BOTH live_paper.enabled and runner.live_orders_enabled. Uses a
    SEPARATE daily cap from live_paper.max_picks_per_day: sharing that budget
    would let an active runner morning starve the premarket plan the
    analyst/risk-manager pair already validated.
    """
    rcfg = cfg.get("runner", {}) or {}
    out = {"placed": 0, "skipped": [], "buying_power": None}
    if not (cfg.get("live_paper", {}) or {}).get("enabled"):
        out["skipped"].append("live_paper.enabled is false")
        return out
    if not rcfg.get("live_orders_enabled"):
        out["skipped"].append("runner.live_orders_enabled is false")
        return out

    # Log buying power BEFORE the loop. A pick rejected for insufficient buying
    # power is a hole in the data, not a trade, so it must be visible as such
    # rather than inferred from a missing order.
    try:
        acct = live_broker._request("GET", "/v2/account", cfg=cfg)
        bp = float((acct or {}).get("buying_power") or 0)
        out["buying_power"] = bp
        log("runner", f"live paper buying power before placement: ${bp:,.2f}")
    except Exception as e:
        log("runner", f"WARN could not read buying power: {e}")

    cap = int(rcfg.get("max_live_orders_per_day", 10))
    dollars_per_r = float(rcfg.get("dollars_per_r", 100))
    size_r = float(rcfg.get("size_r", cfg.get("min_size_r", 0.25)))

    for c, pick_id in placements[:cap]:
        entry = c.get("detection_price")
        stop = c.get("stop_price")
        target = c.get("target_price")
        if not (entry and stop and target):
            out["skipped"].append(f"{c['ticker']}: incomplete plan")
            continue
        qty = live_broker.quantity_for_r(size_r, dollars_per_r, entry, stop)
        if qty < 1:
            out["skipped"].append(f"{c['ticker']}: qty<1 at size_r={size_r}")
            continue
        try:
            # entry_type is "open", so this is a market order and the ordinary
            # bracket path applies — the breakout_two_step complexity is not
            # involved.
            order = live_broker.submit_bracket_order(
                c["ticker"], qty, "buy", "open", entry, stop, target, cfg=cfg)
        except Exception as e:
            out["skipped"].append(f"{c['ticker']}: {e}")
            continue
        if not order:
            # Keep the broker's own reason: a bare "no order" hid a 422 on
            # every runner order for weeks (sub-penny prices).
            reason = live_broker.last_error() or "no detail"
            out["skipped"].append(f"{c['ticker']}: broker refused ({reason})"[:400])
            log("runner", f"  live paper order {c['ticker']} REFUSED: {reason}")
            continue
        try:
            ledger.save_live_order(con, pick_id, order, session_date,
                                   c["ticker"], "buy", qty, "market")
        except Exception as e:
            log("runner", f"WARN could not persist live order for {c['ticker']}: {e}")
        out["placed"] += 1
        log("runner", f"  live paper order {c['ticker']} qty={qty}")

    if len(placements) > cap:
        out["skipped"].append(f"{len(placements) - cap} beyond max_live_orders_per_day={cap}")
    return out


def run_selection(cfg: Config, *, con, session_date: date,
                  md=None, at: datetime | None = None) -> dict:
    """The decision tick (spec 2.1). At most one LLM call per session."""
    rcfg = cfg.get("runner", {}) or {}
    max_picks = int(rcfg.get("max_picks", 10))
    candidates = ledger.runner_candidates_for_date(con, session_date)

    # -- 0. idempotency -------------------------------------------------
    # Claimed BEFORE any branch that costs money or writes picks. The intraday
    # cron fires every five minutes and can retry or overlap; without this a
    # decision tick could pay for two calls and write two competing pick sets.
    if not ledger.claim_runner_selection(con, session_date, at=at):
        return {"skipped": "selection already made for this session"}

    def _finish(reason: str, **extra) -> dict:
        ledger.upsert_runner_session(con, {
            "session_date": session_date, "selection_skipped_reason": reason})
        return {"selection_skipped_reason": reason, **extra}

    # -- 1. nothing to choose from --------------------------------------
    if not candidates:
        return _finish("no candidates")

    # -- 2. no choice to make -------------------------------------------
    # Fewer candidates than slots means there is no selection problem. Do not
    # pay for one.
    if len(candidates) <= max_picks:
        selected = [{"ticker": c["ticker"], "rank": i, "conviction": None,
                     "rationale": "auto-selected: candidates <= max_picks"}
                    for i, c in enumerate(sorted(candidates, key=lambda r: r["ticker"]), 1)]
        result = _write_picks(cfg, con, session_date, candidates, selected)
        result.update(_finish("candidates <= max_picks"))
        return result

    # -- 3. the one LLM call --------------------------------------------
    started = now_et().replace(tzinfo=None)
    model = rcfg.get("selection_model", "claude-sonnet-4-6")
    system = load_prompt("runner_selector", {
        "MAX_PICKS": str(max_picks),
        "REGIME_TAG": _regime_tag(con, session_date) or "(unknown)",
        "PLAYBOOK_SECTION": _playbook_section(con),
        "CANDIDATE_TABLE": build_candidate_table(candidates),
    })
    client = LLMClient(cfg)
    try:
        res = client.call(system=system,
                          user="Select today's runner picks. Reply with JSON only.",
                          model=model, mock_name="runner_selector",
                          expect_json=False)
    except Exception as e:
        ledger.log_run(con, "runner", session_date, started, "error",
                       model=model, error=str(e))
        # A failed selection is a NO-TRADE session. Never fall back to a
        # heuristic: a silent heuristic would contaminate the baseline-vs-
        # selection comparison this whole design exists to produce.
        return _finish(f"selection_failed: {e}"[:500])

    ledger.log_run(con, "runner", session_date, started, "ok",
                   model=model, usage=res.usage)

    valid = {c["ticker"] for c in candidates}
    try:
        selected, hallucinated = parse_selection(res.text, valid)
    except ValueError as e:
        return _finish(f"selection_failed: {e}"[:500], hallucinated=0)

    if hallucinated:
        log("runner", f"selector returned {len(hallucinated)} ticker(s) not in the "
                      f"candidate list; dropped: {hallucinated}")
    selected = selected[:max_picks]
    if not selected:
        return _finish("selector returned no picks",
                       hallucinated=len(hallucinated))

    result = _write_picks(cfg, con, session_date, candidates, selected)
    result["hallucinated"] = len(hallucinated)
    result["usage"] = res.usage
    return result


def _write_picks(cfg: Config, con, session_date: date,
                 candidates: list[dict], selected: list[dict]) -> dict:
    """Persist picks + selection stamps, then place live paper orders."""
    by_ticker = {c["ticker"]: c for c in candidates}
    placements: list[tuple[dict, int]] = []
    for sel in selected:
        c = by_ticker.get(sel["ticker"])
        if not c:
            continue
        pick_id = None
        try:
            pick_id = save_runner_pick(con, session_date, c, sel, cfg)
        except Exception as e:
            log("runner", f"WARN could not write pick for {sel['ticker']}: {e}")
        ledger.mark_runner_selected(con, session_date, sel["ticker"],
                                    sel["rank"], pick_id)
        if pick_id:
            placements.append((c, pick_id))

    log("runner", f"selected {len(selected)} of {len(candidates)} candidates: "
                  + ", ".join(s["ticker"] for s in selected))
    orders = _place_live_orders(cfg, con, session_date, placements)
    # Durable record of what reached the broker (and why the rest did not).
    note = f"placed {orders.get('placed', 0)}/{len(placements)}"
    if orders.get("skipped"):
        note += " | " + " ; ".join(orders["skipped"])
    try:
        ledger.upsert_runner_session(con, {"session_date": session_date,
                                           "live_orders_note": note[:2000]})
    except Exception as e:
        log("runner", f"WARN could not record live order note: {e}")
    return {"selected": [s["ticker"] for s in selected],
            "candidates": len(candidates), "orders": orders}
