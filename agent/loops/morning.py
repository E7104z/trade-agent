"""
MORNING LOOP (6:45 AM ET)

  pull market data -> compute regime -> load memory -> two-stage Claude call
  (analyst generates candidates, risk manager kills the weak ones) ->
  validate plan -> log picks to ledger -> deliver (email + dashboard).
"""
from __future__ import annotations

import itertools
import json
import threading
from collections import Counter
from datetime import date

from .. import ledger
from ..config import REPORTS_DIR, Config
from ..delivery import morning_email_html, render_dashboard, send_email
from ..failures import classify_failure, format_failure
from ..indicators import beta_corr
from ..llm import LLMClient, load_prompt, merge_usage, prompt_version
from ..memory import build_morning_memory
from ..providers import (EXCLUDED_PRODUCTS, MarketData, is_common_stock_symbol,
                         render_bundle_md, yf_history_closes)
from ..regime import compute_regime, regime_md
from ..util import is_market_day, log, now_et, today_et


def _reference_prices(bundle: dict) -> dict[str, float]:
    """Best-known last/current price per ticker from the morning data bundle.

    Used only by _validate_plan's price-grounding gates — NOT for grading or
    sizing. Sources: premarket gap scan, movers, and catalyst_quotes (live
    snapshots for every news-headline ticker — added after the 2026-08-24
    INSM incident, where a news-sourced pick outside the scan universe had no
    reference at all and sailed through with a hallucinated 72.5 entry on a
    ~125 stock). This is only the bundle-sourced baseline: _grounded_ref_prices
    tops it up with a live fetch for any proposed ticker missing here.
    """
    out: dict[str, float] = {}
    for g in (bundle.get("premarket_gappers") or []):
        sym, last = g.get("symbol"), g.get("last")
        if sym and last:
            out[str(sym).upper()] = float(last)
    movers = bundle.get("movers") or {}
    for m in (movers.get("gainers") or []) + (movers.get("losers") or []):
        sym, px = m.get("symbol"), m.get("price")
        if sym and px and str(sym).upper() not in out:
            out[str(sym).upper()] = float(px)
    for q in (bundle.get("catalyst_quotes") or []):
        sym, last = q.get("symbol"), q.get("last")
        if sym and last and str(sym).upper() not in out:
            out[str(sym).upper()] = float(last)
    return out


def _grounded_ref_prices(md: MarketData, plan: dict, bundle: dict, d: date,
                         seed: dict[str, float] | None = None) -> dict[str, float]:
    """Reference prices for _validate_plan, keyed off what the analyst PROPOSED.

    The bundle's quotes only cover tickers some feed happened to surface —
    which is why the 2026-08-25 DKS incident got through: an earnings short
    sourced purely by the analyst's own web search (no Alpaca news symbol
    tag, not in the gap scan/movers) was anchored on a fictional '$179.64'
    while DKS traded ~$124-139, and the grounding gate had nothing to check
    it against. So: one batched live-snapshot fetch for every proposed
    ticker the bundle didn't quote, merged in before validation. Tickers
    whose fetch returns nothing stay absent — _validate_plan drops those
    (fail closed) rather than trusting an unverifiable price.
    """
    refs = _reference_prices(bundle)
    # `seed` carries quotes already fetched this run by
    # _grounded_candidate_prices (same md, same session, minutes old at most).
    # Merging them means a ticker grounded before the risk manager is not
    # re-fetched after it -- one snapshot call per ticker per run, not two, on
    # a 300s serverless budget with arm B running concurrently. It does not
    # weaken the gate: these are real fetched quotes, and the magnitude/
    # entry-type checks below run against them exactly as they would against a
    # fresh one (the gate's band is 15%, far outside any staleness of this
    # size). A ticker in neither map is still absent, and still dropped.
    refs.update(seed or {})
    proposed = [str(p.get("ticker", "")).upper().strip() for p in (plan.get("picks") or [])]
    missing = [t for t in dict.fromkeys(proposed) if t and t not in refs]
    if not missing:
        return refs
    log("morning", f"Fetching live quotes for unreferenced pick ticker(s): {', '.join(missing)}")
    try:
        refs.update(md.pick_quotes(missing, d))
    except Exception as e:
        log("morning", f"WARN pick-quote fetch failed ({e}) — affected picks will be dropped as unverifiable")
    return refs


def _candidate_tickers(candidates: dict) -> list[str]:
    """Deduped, normalized ticker list from an analyst candidate block."""
    proposed = [str(c.get("ticker", "")).upper().strip()
                for c in (candidates.get("candidates") or [])]
    return [t for t in dict.fromkeys(proposed) if t]


def _grounded_candidate_prices(md: MarketData, candidates: dict,
                               bundle: dict, d: date) -> dict[str, float]:
    """Live last price for every ANALYST candidate the bundle never quoted.

    Runs BEFORE the risk manager -- unlike _grounded_ref_prices, which runs
    after it -- so a web-search-sourced catalyst is priced while it can still
    be traded, rather than after kill-check 2c has already dropped it for
    having no price.

    That ordering was the whole bug (2026-09-08): pick_quotes existed, was
    built precisely to rescue "tickers the bundle never quoted," and only ever
    ran on picks that had ALREADY survived the risk manager -- so it never
    fired for the tickers it was written for. Two catalysts (conviction 5 and
    4) died that morning annotated "excellent catalyst, untradeable structure",
    which is this bug's signature: the market offered something, the plumbing
    did not deliver it.

    Same fail-closed contract as every other grounding fetch: a ticker absent
    here is still absent downstream and is still dropped by _validate_plan.
    This only moves the FETCH earlier -- it does not weaken any gate.
    """
    refs = _reference_prices(bundle)
    missing = [t for t in _candidate_tickers(candidates) if t not in refs]
    if not missing:
        return {}
    log("morning", f"Grounding candidate ticker(s) the bundle missed: {', '.join(missing)}")
    try:
        return md.pick_quotes(missing, d)
    except Exception as e:  # noqa: BLE001
        log("morning", f"WARN candidate-quote fetch failed ({e}) — "
                       f"risk manager will see no live price for: {', '.join(missing)}")
        return {}


def _live_quotes_md(cand_quotes: dict[str, float]) -> str:
    """Render the post-analyst quote top-up for the risk manager's user block.

    Deliberately grounds ONLY the tickers actually fetched, BY NAME. This must
    never read as a blanket pass on kill-check 2c: a candidate not listed here
    and not in MARKET DATA is still ungrounded, and the mechanical gates in
    _validate_plan run afterward on the merged map regardless of what the risk
    manager concludes from this section.
    """
    if not cand_quotes:
        return ""
    lines = "\n".join(f"- {t}: ${p}" for t, p in sorted(cand_quotes.items()))
    return ("## LIVE QUOTES — fetched after the analyst call\n\n"
            "Verified last prices for candidate tickers that were absent from\n"
            "MARKET DATA above. A ticker listed here IS price-grounded; do not\n"
            "kill it under check 2c for absence of a quote. A candidate ticker\n"
            "NOT listed here and not in MARKET DATA remains ungrounded.\n\n"
            f"{lines}\n")


def _candidate_grounding_health(candidates: dict, bundle: dict,
                                cand_quotes: dict[str, float]) -> dict:
    """How much of the analyst's OWN candidate list could be priced at all.

    This is the honest denominator for the data-health verdict: it is measured
    over the population the analyst produced, before the risk manager removes
    anything, so "half the candidates had no obtainable price" means what it
    says. Measuring it over surviving picks instead would report a clean
    session on exactly the mornings that went worst -- if everything was killed
    for having no price, there are no picks left to count.
    """
    priced = {**_reference_prices(bundle), **cand_quotes}
    tickers = _candidate_tickers(candidates)
    return {"candidates_seen": len(tickers),
            "candidates_ungrounded": sum(1 for t in tickers if t not in priced)}


def _bundle_field(bundle: dict, field: str) -> dict:
    """Generic extractor for a per-ticker numeric/scalar field carried on
    premarket_gappers/catalyst_quotes rows (mirrors _reference_prices' merge
    order: gap-scan/movers before catalyst quotes, first value wins)."""
    out: dict = {}
    for g in (bundle.get("premarket_gappers") or []) + (bundle.get("catalyst_quotes") or []):
        sym, val = g.get("symbol"), g.get(field)
        if sym and val is not None and str(sym).upper() not in out:
            out[str(sym).upper()] = val
    return out


def _bundle_health(md: MarketData, bundle: dict) -> dict:
    """Session-level data facts _validate_plan cannot see for itself: how much
    the bundle actually covered, and whether the quote feed had to be
    downgraded to get there. Feeds _data_health; never gates anything."""
    missing = bundle.get("missing") or []
    return {
        "bundle_gappers": len(bundle.get("premarket_gappers") or []),
        "bundle_catalyst_quotes": len(bundle.get("catalyst_quotes") or []),
        "catalyst_quotes_failed": any(str(m).startswith("catalyst_quotes:") for m in missing),
        "feed_downgraded": bool(getattr(md, "feed_downgraded", False)),
    }


def _grounded_atr(md: MarketData, plan: dict, bundle: dict) -> dict[str, float]:
    """ATR14 per proposed ticker for the noise-stop gate — bundle-sourced
    first (already computed during enrichment, zero extra cost), then one
    batched daily_context fetch for whatever the bundle didn't cover (mirrors
    _grounded_ref_prices' top-up pattern). A fetch failure leaves affected
    tickers absent, which _validate_plan treats as fail-OPEN (skip the gate,
    warn) — never fail-closed like price grounding; see spec §3."""
    atr_map = {k: float(v) for k, v in _bundle_field(bundle, "atr14").items()}
    proposed = [str(p.get("ticker", "")).upper().strip() for p in (plan.get("picks") or [])]
    missing = [t for t in dict.fromkeys(proposed) if t and t not in atr_map]
    if not missing:
        return atr_map
    try:
        ctx = md.daily_context(missing)
        for t, c in ctx.items():
            if c.get("atr14"):
                atr_map[t] = float(c["atr14"])
    except Exception as e:
        log("morning", f"WARN ATR fetch failed for noise-stop gate ({e}) — gate skipped (fails open) for affected tickers")
    return atr_map


def _grounded_earnings(md: MarketData, plan: dict, bundle: dict) -> dict[str, int]:
    """earnings_days_away per proposed ticker for the binary-event gate —
    same bundle-first, top-up-fetch, fail-open pattern as _grounded_atr."""
    earn_map = {k: int(v) for k, v in _bundle_field(bundle, "earnings_days_away").items()}
    proposed = [str(p.get("ticker", "")).upper().strip() for p in (plan.get("picks") or [])]
    missing = [t for t in dict.fromkeys(proposed) if t and t not in earn_map]
    if not missing:
        return earn_map
    try:
        for t, v in md.next_earnings(missing).items():
            if v is not None:
                earn_map[t] = int(v)
    except Exception as e:
        log("morning", f"WARN earnings fetch failed for binary-event gate ({e}) — gate skipped (fails open) for affected tickers")
    return earn_map


def _data_health(gate_counts: dict[str, int], n_proposed: int,
                 session_health: dict | None) -> dict:
    """The session's DATA-HEALTH verdict: could this pipeline evaluate the
    market at all, as opposed to evaluating it and declining?

    Today those two states are indistinguishable downstream — a plan reading
    "systemic data gaps / CANNOT VERIFY ATR/VWAP" is stored beside a run row
    saying status "ok", error "". Nothing can then tell a plumbing failure from
    a judgment call, and the miner ends up pooling two entirely different
    classes under one "no_trade" label. This promotes the existing Phase 1f
    fail-open counters to a first-class verdict; it gates nothing and changes
    no number.

    `session_health` carries the facts _validate_plan cannot see for itself
    (bundle coverage, feed state, the analyst's candidate population) and is
    supplied by run(); callers that omit it still get a valid verdict computed
    from the gate counters alone, with the checks whose inputs are absent
    simply not firing — never fabricated as passing.
    """
    sh = dict(session_health or {})
    out: dict = {
        "candidates_seen": sh.get("candidates_seen"),
        "candidates_ungrounded": sh.get("candidates_ungrounded"),
        "picks_proposed": n_proposed,
        "picks_ungrounded": gate_counts["grounding"],
        "atr_unmeasured": gate_counts["atr"],
        "earnings_unmeasured": gate_counts["earnings"],
        "bundle_gappers": sh.get("bundle_gappers"),
        "bundle_catalyst_quotes": sh.get("bundle_catalyst_quotes"),
        "catalyst_quotes_failed": sh.get("catalyst_quotes_failed"),
        "feed_downgraded": sh.get("feed_downgraded"),
    }

    # Ungrounded ratio: measured over the ANALYST's candidates when the
    # candidate-grounding pass ran (that population is the honest denominator —
    # it is measured before the risk manager removes anything), and otherwise
    # over the picks the risk manager proposed. Never mixed: a ratio whose
    # numerator and denominator come from different populations is worse than
    # no ratio at all.
    by_candidate = out["candidates_ungrounded"] is not None and bool(out["candidates_seen"])
    ungrounded, seen = ((out["candidates_ungrounded"], out["candidates_seen"])
                        if by_candidate else (out["picks_ungrounded"], n_proposed))
    out["ungrounded_basis"] = "candidates" if by_candidate else "picks"

    reasons: list[str] = []
    # A gap scan that returned NOTHING is a feed failure, not a flat tape: the
    # scan folds in INDEX_ETFS unconditionally, so a healthy call cannot come
    # back empty.
    if out["bundle_gappers"] == 0:
        reasons.append("premarket gap scan returned no rows at all")
    if out["catalyst_quotes_failed"]:
        reasons.append("catalyst-quote fetch raised")
    if seen and ungrounded >= max(2, 0.5 * seen):
        reasons.append(f"{ungrounded}/{seen} {out['ungrounded_basis']} had no obtainable price")
    out["degraded"] = bool(reasons)
    out["degraded_reasons"] = reasons
    return out


def _validate_plan(plan: dict, cfg: Config, streak: dict | None = None,
                   drawdown: dict | None = None,
                   ref_prices: dict[str, float] | None = None,
                   atr_map: dict[str, float] | None = None,
                   earnings_map: dict[str, int] | None = None,
                   session_health: dict | None = None) -> tuple[dict, list[str]]:
    """Hard-validate and normalize the risk manager's plan. Returns (plan, warnings).

    The playbook's losing-streak rule (>=3 losers: cap 0.5R, demand conviction >= 4)
    is enforced MECHANICALLY here — the prompt asks the risk manager to apply it,
    but discipline rules must not depend on an LLM remembering them.

    Same for the DRAWDOWN CIRCUIT BREAKER: if the trailing drawdown_window
    triggered trades sum to <= drawdown_pause_r weighted R, the desk enters
    capital-preservation mode — max 1 pick, minimum size, conviction >= 4 —
    regardless of what the LLMs proposed. Losing streaks catch consecutive
    losers; this catches the slow bleed that alternating wins/losses hides.

    PRICE-GROUNDING GATE (magnitude): an entry more than
    max_entry_ref_distance_pct away from the last known price — in EITHER
    direction — is dropped as ungrounded. A limit 40% below market isn't a
    patient pullback order, it's evidence the level was invented (stale
    article price, hallucinated history) and would either never fill or fill
    only in a crash the thesis never contemplated. Direction-only checking
    misses this shape entirely: real incident 2026-08-24, INSM long limit
    72.5 vs actual ~125 — "below market" looked like normal pullback buying
    to the entry-type gate. Magnitude is checked FIRST so a wildly-off level
    is diagnosed as ungrounded rather than confusingly as a mistyped order.

    ENTRY-TYPE SANITY GATE (side): "limit" means buy at this price or better
    (so a long limit only makes sense at/below the current price — you're
    buying weakness); "breakout" means buy as price reaches this level from
    below (only makes sense at/above current — you're buying strength as
    it's earned). A long limit order priced ABOVE the market is not a limit
    order at all — it fills immediately at whatever the market is, i.e. a
    disguised market order, with the "entry price" purely decorative. Left
    unchecked, that mistype can fill on a gap-through at a price already
    past the trade's own stop (see grading.py's invalid_fill gate, which
    catches the resulting fill but can't fix the mistyped plan). Where a
    reference price is available, mistyped picks are dropped rather than
    silently executed as market orders.

    COVERAGE: every proposed ticker is checked — no exceptions. ref_prices
    starts from the bundle (scan universe, movers, most actives, premarket
    gappers, news-headline catalyst_quotes) and _grounded_ref_prices tops it
    up with one batched live snapshot for any proposed ticker the bundle
    missed, so coverage tracks what the analyst proposed, not which feed
    happened to tag it. That closed the last hole — tickers surfaced purely
    by the LLM's web search or by company-name-keyed 8-K filings (real
    incident 2026-08-25: DKS earnings short anchored on a fictional $179.64
    while the stock traded ~$124-139; no feed had quoted DKS, so the gate
    never fired). A ticker with NO obtainable quote at all is DROPPED, not
    waved through — fail closed: an unverifiable price is exactly as
    dangerous as a verifiably wrong one. grading.py's invalid_fill gate
    remains the final backstop for anything that still slips through.
    """
    warnings: list[str] = []
    # Phase 1f fail-open telemetry: count proposed tickers for which each fail-
    # OPEN gate could not run (data provider outage), and how many were dropped
    # for having no obtainable price reference at all (a data-quality failure,
    # not a magnitude rejection — see _validate_plan's PRICE-GROUNDING docstring).
    # Attached to `plan` (not returned separately) so every existing caller of
    # this function keeps working unchanged; ledger.save_daily_plan reads it
    # off `plan` the same way it already reads regime_tag/prompt_version.
    gate_counts = {"atr": 0, "earnings": 0, "grounding": 0}
    n_proposed = len(plan.get("picks") or [])

    def _stamp_gate_telemetry() -> None:
        plan["gates_failed_open"] = json.dumps(gate_counts)
        plan["provider_degraded"] = bool(
            n_proposed > 0 and any(v > n_proposed / 2 for v in gate_counts.values()))
        plan["data_health"] = _data_health(gate_counts, n_proposed, session_health)

    max_picks = int(cfg.get("max_picks", 4))
    min_r, max_r = float(cfg.get("min_size_r", 0.25)), float(cfg.get("max_size_r", 1.5))
    max_total = float(cfg.get("max_total_open_risk_r", 3.0))
    losing_streak = bool(streak and streak.get("kind") == "losing" and streak.get("length", 0) >= 3)
    atr_gate_mode = str(cfg.get("atr_gate", "warn")).lower()
    earnings_gate_mode = str(cfg.get("earnings_gate", "warn")).lower()
    leveraged_gate_mode = str(cfg.get("leveraged_gate", "warn")).lower()
    min_stop_atr = float(cfg.get("min_stop_atr", 0.35))
    warn_stop_atr = float(cfg.get("warn_stop_atr", 2.5))
    warn_target_atr = float(cfg.get("warn_target_atr", 3.5))

    dd_pause = float(cfg.get("drawdown_pause_r", -4.0))
    dd_mode = bool(drawdown and drawdown.get("n", 0) >= 5
                   and drawdown.get("sum_weighted_r", 0.0) <= dd_pause)
    if dd_mode:
        warnings.append(
            f"CAPITAL PRESERVATION MODE: trailing {drawdown['n']}-trade weighted R "
            f"{drawdown['sum_weighted_r']:+.2f} <= {dd_pause} — max 1 pick at {min_r}R, conviction >= 4")
        max_picks = 1

    picks = plan.get("picks") or []
    if plan.get("no_trade"):
        plan["picks"] = []
        _stamp_gate_telemetry()
        return plan, warnings

    valid: list[dict] = []
    seen: set[str] = set()
    for p in picks:
        t = str(p.get("ticker", "")).upper().strip()
        if not t or t in seen:
            warnings.append(f"dropped duplicate/empty ticker entry: {t or '(blank)'}")
            continue
        seen.add(t)
        p["ticker"] = t
        d = str(p.get("direction", "long")).lower()
        p["direction"] = d if d in ("long", "short") else "long"

        # LEVERAGED-PRODUCT / STRUCTURAL-INSTRUMENT GATE (Phase 1d — warn mode
        # by default, config `leveraged_gate`, same idiom as atr_gate/
        # earnings_gate). Flip to "enforce" only after shadow evidence shows
        # what it would have cost.
        if t in EXCLUDED_PRODUCTS or not is_common_stock_symbol(t):
            kind = ("leveraged/inverse/volatility product" if t in EXCLUDED_PRODUCTS
                    else "warrant/unit/rights-shaped symbol")
            msg = (f"{t}: {kind} — moves on mechanical leverage/decay or conversion "
                   f"supply, not a company catalyst; a thesis on it is a thesis about "
                   f"its underlying wearing a costume")
            if leveraged_gate_mode == "enforce":
                warnings.append(msg + " — dropped")
                continue
            warnings.append(f"[LEVERAGED GATE warn-only, would drop if enforced] {msg}")

        entry, stop, target = p.get("entry_price"), p.get("stop_price"), p.get("target_price")
        if not all(isinstance(x, (int, float)) for x in (entry, stop, target)):
            warnings.append(f"{t}: missing numeric entry/stop/target — dropped")
            continue
        if p["direction"] == "long" and not (stop < entry < target):
            warnings.append(f"{t}: long levels inconsistent (need stop<entry<target) — dropped")
            continue
        if p["direction"] == "short" and not (target < entry < stop):
            warnings.append(f"{t}: short levels inconsistent (need target<entry<stop) — dropped")
            continue

        ref = (ref_prices or {}).get(t)
        if not ref:
            gate_counts["grounding"] += 1
            warnings.append(
                f"{t}: entry {entry} could not be verified against any live quote (no bundle "
                f"reference and the pre-validation snapshot fetch returned nothing) — dropped; "
                f"an unverifiable price fails closed, not open")
            continue
        max_dist = float(cfg.get("max_entry_ref_distance_pct", 15.0))
        dist_pct = abs(entry - ref) / ref * 100
        if dist_pct > max_dist:
            warnings.append(
                f"{t}: entry {entry} is {dist_pct:.0f}% away from last price {ref} "
                f"(max {max_dist:.0f}%) — dropped as ungrounded; the level was priced off "
                f"something other than the actual market")
            continue
        entry_type = str((p.get("entry_plan") or {}).get("type") or p.get("entry_type") or "open").lower()
        tol = 1.01  # 1% buffer for reference-price staleness, not a chase threshold
        mistyped_as = None
        if entry_type == "limit":
            if p["direction"] == "long" and entry > ref * tol:
                mistyped_as = "breakout"
            elif p["direction"] == "short" and entry < ref / tol:
                mistyped_as = "breakout"
        elif entry_type == "breakout":
            if p["direction"] == "long" and entry < ref / tol:
                mistyped_as = "limit"
            elif p["direction"] == "short" and entry > ref * tol:
                mistyped_as = "limit"
        if mistyped_as:
            warnings.append(
                f"{t}: entry_type={entry_type} but entry {entry} is on the wrong side of "
                f"last price {ref} for a {p['direction']} (reads as {mistyped_as}) — dropped "
                f"rather than let it fill as a disguised market order")
            continue

        # NOISE-STOP / WIDE-STOP / FANTASY-TARGET GATE (fails OPEN on missing ATR —
        # see module docstring's "null asymmetry" note: ATR is not always
        # fetchable for a real tradable name, and dropping a real trade on
        # missing metadata is the worse error than skipping the check).
        atr14 = (atr_map or {}).get(t)
        if atr14:
            stop_dist_atr = abs(entry - stop) / atr14
            target_dist_atr = abs(target - entry) / atr14
            if stop_dist_atr < min_stop_atr:
                msg = (f"{t}: stop is {stop_dist_atr:.2f} ATR from entry (min {min_stop_atr}) — "
                       f"inside daily noise, the thesis cannot be tested at this distance")
                if atr_gate_mode == "enforce":
                    warnings.append(msg + " — dropped")
                    continue
                warnings.append(f"[ATR GATE warn-only, would drop if enforced] {msg}")
            elif stop_dist_atr > warn_stop_atr:
                warnings.append(f"{t}: stop is {stop_dist_atr:.2f} ATR from entry "
                                f"(> {warn_stop_atr}) — unusual, not necessarily wrong")
            if target_dist_atr > warn_target_atr and p.get("time_stop"):
                warnings.append(f"{t}: target is {target_dist_atr:.2f} ATR from entry "
                                f"(> {warn_target_atr}) with a same-day time_stop — aggressive")
        else:
            gate_counts["atr"] += 1
            warnings.append(f"{t}: ATR unavailable — noise-stop/wide-stop checks skipped (unmeasured)")

        # BINARY-EVENT GATE (fails OPEN on missing earnings data, same reasoning).
        # earnings_days_away == 0 means "reports today" (spec) -- this desk is
        # intraday-only (every position exits by the 16:00 close), so "today"
        # IS the entire holding window.
        earn_days = (earnings_map or {}).get(t)
        if earn_days is not None:
            is_earnings_play = "earnings" in str(p.get("setup_type", "")).lower() or \
                               "earnings" in str(p.get("thesis") or p.get("thesis_final") or "").lower()
            if earn_days == 0 and not is_earnings_play:
                msg = (f"{t}: earnings today (not a declared earnings play) — a report can gap "
                       f"through the stop; the stop is fiction against a binary event")
                if earnings_gate_mode == "enforce":
                    warnings.append(msg + " — dropped")
                    continue
                warnings.append(f"[EARNINGS GATE warn-only, would drop if enforced] {msg}")
        else:
            gate_counts["earnings"] += 1
            warnings.append(f"{t}: earnings date unavailable — binary-event check skipped (unmeasured)")

        size = p.get("size_r")
        if not isinstance(size, (int, float)):
            size = min_r
            warnings.append(f"{t}: size_r missing, defaulted to {min_r}")
        p["size_r"] = round(min(max(float(size), min_r), max_r), 2)

        conv = p.get("conviction")
        p["conviction"] = int(min(max(int(conv), 1), 5)) if isinstance(conv, (int, float)) else 3

        if losing_streak:
            if p["conviction"] < 4:
                warnings.append(f"{t}: dropped — losing streak >= 3 demands conviction >= 4 "
                                f"(had {p['conviction']})")
                continue
            if p["size_r"] > 0.5:
                warnings.append(f"{t}: size clamped {p['size_r']} -> 0.5R (losing-streak rule)")
                p["size_r"] = 0.5
        if dd_mode:
            if p["conviction"] < 4:
                warnings.append(f"{t}: dropped — capital preservation mode demands conviction >= 4 "
                                f"(had {p['conviction']})")
                continue
            if p["size_r"] > min_r:
                warnings.append(f"{t}: size clamped {p['size_r']} -> {min_r}R (capital preservation mode)")
                p["size_r"] = min_r
        valid.append(p)

    if len(valid) > max_picks:
        valid.sort(key=lambda x: -x["conviction"])
        warnings.append(f"trimmed picks {len(valid)} -> {max_picks} (kept highest conviction)")
        valid = valid[:max_picks]

    total = sum(p["size_r"] for p in valid)
    if total > max_total and total > 0:
        scale = max_total / total
        for p in valid:
            p["size_r"] = round(max(p["size_r"] * scale, min_r), 2)
        warnings.append(f"scaled sizes down by {scale:.2f} to respect total risk cap {max_total}R")

    plan["picks"] = valid
    plan["total_risk_r"] = round(sum(p["size_r"] for p in valid), 2)
    if not valid and not plan.get("no_trade"):
        plan["no_trade"] = True
        plan["no_trade_reason"] = plan.get("no_trade_reason") or "All picks failed hard validation."
        warnings.append("no valid picks survived validation — converted to no_trade day")
    _stamp_gate_telemetry()
    return plan, warnings


def _correlation_matrix_md(tickers: list[str]) -> str:
    """Pairwise 20d-return correlation + each name's beta to SPY, for the
    risk manager's kill-check 17 (thematic exposure -> arithmetic, not just
    narrative). Rendered only when >= 2 distinct candidates. Best-effort: any
    fetch/compute failure just omits the section — this never blocks the run,
    and its absence reads to the risk manager as "not computed," not as a
    certified absence of correlation (it still owns kill-check 12 either way).
    """
    uniq = list(dict.fromkeys(t.upper() for t in tickers if t))
    if len(uniq) < 2:
        return ""
    try:
        closes = yf_history_closes(uniq + ["SPY"], days=40)
        spy = closes.get("SPY") or []
        betas = {}
        for t in uniq:
            bc = beta_corr(spy, closes.get(t) or [], n=20)
            if bc:
                betas[t] = bc[0]
        pairs = []
        for i, a in enumerate(uniq):
            for b in uniq[i + 1:]:
                bc = beta_corr(closes.get(a) or [], closes.get(b) or [], n=20)
                if bc and abs(bc[1]) > 0.7:
                    pairs.append(f"{a}/{b} corr {bc[1]:+.2f}")
        if not betas and not pairs:
            return ""
        lines = ["## CORRELATION / BETA (candidates, 20d daily returns — computed, not a vibe)\n"]
        if betas:
            lines.append("- Beta to SPY: " + ", ".join(f"{t} {b:+.2f}" for t, b in betas.items()))
        lines.append("- Pairs with |corr| > 0.7 (share ONE risk unit, kill-check 17): "
                     + (", ".join(pairs) if pairs else "none"))
        return "\n".join(lines)
    except Exception as e:
        log("morning", f"WARN correlation matrix failed ({e}) — kill-check 17 section omitted")
        return ""


def _attach_overlay_context(plan: dict, bundle: dict) -> None:
    """Attach bundle-computed levels/context to each surviving pick, in-place,
    so the dashboard's Arby chart can overlay them (PDH/PDL/PMH/PML dashed
    lines, plus earnings-soon / short%-high / extension quips) — spec v2 §7.

    Additive and best-effort: every field is already computed on the morning
    bundle (zero extra network), rides in the plan JSON so it reaches both
    /api/status and the static boot payload with no schema change, and is
    simply omitted when the bundle never enriched that ticker (e.g. a
    web-search-sourced name) — never fabricated. Any failure leaves the plan
    untouched; overlays are decoration and must never fail a real loop.
    """
    fields = ("pdh", "pdl", "pm_high", "pm_low", "hi_52w", "atr_pct",
              "gap_atr", "ext_atr", "earnings_days_away", "short_pct_float")
    try:
        maps = {f: _bundle_field(bundle, f) for f in fields}
    except Exception as e:
        log("morning", f"WARN overlay-context attach failed ({e}) — dashboard overlays skipped")
        return
    for p in plan.get("picks", []):
        t = str(p.get("ticker", "")).upper()
        ctx = {f: maps[f][t] for f in fields if maps[f].get(t) is not None}
        if ctx:
            p["overlay"] = ctx


_KILL_CHECK_ENUM = {"staleness", "chase", "entry_type", "grounding", "crowding",
                    "stop_quality", "rr", "regime_fit", "correlation", "event_risk",
                    "conviction"}


def _build_rejections(candidates: dict, plan: dict) -> list[dict]:
    """Shadow ledger input: every analyst candidate that did NOT survive to the final
    plan, carrying its full plan levels so the evening loop can grade the counterfactual."""
    taken = {p["ticker"].upper() for p in plan.get("picks", [])}
    rejected_rows = {str(r.get("ticker", "")).upper(): r for r in plan.get("rejected", []) or []}
    # Stamped per row rather than filtered out: a rejection from a degraded
    # session is still graded (a counterfactual is still a counterfactual), it
    # just must not be counted as a REASON — see ledger.shadow_summary.
    degraded = bool((plan.get("data_health") or {}).get("degraded"))
    out = []
    for c in candidates.get("candidates", []) or []:
        t = str(c.get("ticker", "")).upper()
        if not t or t in taken:
            continue
        entry_plan = c.get("entry_plan") or {}
        entry = entry_plan.get("price", c.get("entry_price"))
        stop = c.get("stop_price")
        target = c.get("target_price")
        planned_rr = None
        if all(isinstance(x, (int, float)) for x in (entry, stop, target)):
            risk = abs(float(entry) - float(stop))
            if risk > 0:
                planned_rr = round(abs(float(target) - float(entry)) / risk, 4)
        rejected_row = rejected_rows.get(t) or {}
        reason = rejected_row.get("reason")
        kill_check = str(rejected_row.get("primary_kill_check") or "").strip().lower() or None
        if kill_check not in _KILL_CHECK_ENUM:
            kill_check = None
        # Prefer the risk manager's own structured verdict over the locally
        # computed R:R -- a candidate can compute to <2.0 by coincidence (a
        # price-grounding failure, say) while the actual binding reason was
        # something else entirely; see PARTICIPATION_UNBLOCK_SPEC_v2 Phase 1.1.
        # Falls back to the old inferred heuristic for rows with no enum
        # (pre-migration data, or malformed LLM output).
        if kill_check is not None:
            rejection_category = "rr_below_control" if kill_check == "rr" else "other"
        else:
            rejection_category = ("rr_below_control" if planned_rr is not None and planned_rr < 2.0
                                  else "other")
        out.append({
            "ticker": t,
            "direction": c.get("direction"),
            "setup_type": c.get("setup_type"),
            "reason": reason or "not selected by risk manager (no explicit reason)",
            "entry_type": entry_plan.get("type", "open"),
            "entry_price": entry,
            "stop_price": stop,
            "target_price": target,
            "conviction": c.get("conviction"),
            "thesis": c.get("thesis"),
            "source_stage": "risk_rejected" if reason else "validator_or_unselected",
            "rejection_category": rejection_category,
            "primary_kill_check": kill_check,
            "planned_rr": planned_rr,
            "session_degraded": degraded,
        })
    return out


def _no_trade_category(plan: dict, rejections: list[dict]) -> str | None:
    """Closed-enum classification of WHY a no-trade day happened
    (PARTICIPATION_UNBLOCK_SPEC_v2 Phase 3), separate from the risk manager's
    own free-text `plan["no_trade_reason"]` prose (kept as-is).

    `data_incomplete` always wins when data_health.degraded is true — a
    stand-down declared on degraded data is not a stand-down, regardless of
    what the rejection mix looks like (the mix itself is suspect on a
    degraded session). Otherwise the category is read off the
    `primary_kill_check` distribution across the day's rejections.
    """
    if not plan.get("no_trade"):
        return None
    if (plan.get("data_health") or {}).get("degraded"):
        return "data_incomplete"
    if not plan.get("analyst_candidate_count"):
        return "empty_universe"
    checks = [r.get("primary_kill_check") for r in rejections if r.get("primary_kill_check")]
    if checks:
        top_check, top_n = Counter(checks).most_common(1)[0]
        if top_n >= len(checks) / 2:
            if top_check == "rr":
                return "all_failed_rr"
            if top_check == "regime_fit":
                return "all_failed_regime"
    # Mixed kill-check mix, no primary_kill_check data at all (pre-migration
    # rows), or picks that the risk manager approved but the mechanical
    # validator gates then dropped — none of those are a single clean story,
    # so they're grouped under the validator catch-all rather than guessed at.
    return "all_failed_validator"


def _add_exploration_pick(candidates: dict, plan: dict, cfg: Config) -> bool:
    """Add one explicitly labeled R:R exploration pick, when enabled.

    This lane only reconsiders a candidate the risk manager rejected for R:R;
    other rejection reasons remain binding. It is capped and minimum-sized so
    its results remain a separate experiment rather than a silent policy change.
    """
    lane = cfg.get("exploration_lane", {}) or {}
    if not lane.get("enabled") or plan.get("picks"):
        return False
    max_picks = max(0, int(lane.get("max_picks", 1)))
    if max_picks < 1:
        return False
    rejected_rows = {str(r.get("ticker", "")).upper(): r for r in plan.get("rejected", []) or []}
    # PARTICIPATION_UNBLOCK_SPEC_v2 Phase 1.1/1.2: the risk manager can now
    # name the ONE kill-check that actually ended a candidate
    # (`primary_kill_check`). When it does, that enum is authoritative — admit
    # only when R:R was the SOLE binding reason (kill_check == "rr"), and
    # every other primary reason blocks regardless of what the prose says.
    # When it's absent (pre-migration rows, or malformed LLM output), fall
    # back to the old prose blocklist: these words are matched as substrings
    # against a whole sentence, so a candidate killed purely on "R:R 1.86 <
    # 2.0" is rescuable, but the same candidate killed with "R:R 1.86 < 2.0,
    # and regime is chop" is blocked by the word `regime` alone. The blocklist
    # is NOT widened here -- widening it is gate-loosening by another name.
    blocked = ("stale", "event", "binary", "invalid", "ungrounded", "mismatch",
               "chase", "vague", "unverified", "correlation", "regime")
    low_rr = float(lane.get("min_rr", 1.8))
    high_rr = float(lane.get("max_rr", 2.0))
    for candidate in candidates.get("candidates", []) or []:
        ticker = str(candidate.get("ticker", "")).upper().strip()
        rejected_row = rejected_rows.get(ticker) or {}
        reason = str(rejected_row.get("reason", ""))
        kill_check = str(rejected_row.get("primary_kill_check") or "").strip().lower() or None
        if kill_check not in _KILL_CHECK_ENUM:
            kill_check = None
        if not ticker:
            continue
        if kill_check is None and "r:r" not in reason.lower():
            continue
        entry_plan = candidate.get("entry_plan") or {}
        entry = entry_plan.get("price", candidate.get("entry_price"))
        stop = candidate.get("stop_price")
        target = candidate.get("target_price")
        if not all(isinstance(x, (int, float)) for x in (entry, stop, target)):
            continue
        risk = abs(float(entry) - float(stop))
        if risk <= 0:
            continue
        rr = abs(float(target) - float(entry)) / risk
        if not low_rr <= rr < high_rr:
            continue
        # Checked AFTER the R:R band so the log line can report the rr that was
        # actually in range. Silent on a clean admit either way -- which path
        # decided is recorded durably on the persisted rejection row itself
        # (primary_kill_check), a stdout log line is not; logging is reserved
        # for the diagnostic case (an in-band candidate that got blocked).
        if kill_check is not None:
            if kill_check != "rr":
                log("morning", f"EXPLORATION: {ticker} in-band (R:R {rr:.2f}) but blocked "
                               f"by primary_kill_check={kill_check!r} - reason: {reason!r}")
                continue
        else:
            blocking = [w for w in blocked if w in reason.lower()]
            if blocking:
                log("morning", f"EXPLORATION: {ticker} in-band (R:R {rr:.2f}) but blocked "
                               f"by legacy blocklist word(s) {blocking} - reason: {reason!r}")
                continue
        plan["picks"] = [{
            **candidate,
            "ticker": ticker,
            "entry_plan": {"type": entry_plan.get("type", "open"), "price": entry},
            "entry_price": entry,
            "size_r": float(lane.get("size_r", cfg.get("min_size_r", 0.25))),
            "conviction": min(int(candidate.get("conviction", 3) or 3), 3),
            "exploration_lane": "rr_below_control",
            "exploration_rr": round(rr, 4),
        }]
        plan["no_trade"] = False
        plan.pop("no_trade_reason", None)
        plan["exploration_pick_count"] = 1
        return True
    return False


def _run_paired_arm_b(cfg: Config, d: date, analyst_system: str, risk_system: str,
                      bundle_md: str, bundle: dict, reg_md: str, mem: dict,
                      regime_tag: str, model: str, llm_call) -> dict:
    """Run the paired challenger arm on its own DB connection and MarketData
    instance so it can execute concurrently with arm A on a background thread
    (see run()) instead of stacking two full analyst+risk-manager LLM round
    trips back to back. Sharing arm A's `con`/`md` across threads would race
    on the DB connection and the Alpaca/yfinance client state underneath it;
    a fresh connection/instance sidesteps that entirely. `bundle` itself is
    read-only after morning_bundle() returns, so it's safe to share as-is.

    Never raises — mirrors run_challenger_arm's own never-fail contract, since
    a challenger-arm crash must not take down arm A (which owns this thread).
    """
    from . import paired as paired_mod
    con2 = ledger.connect()
    md2 = MarketData(cfg)
    try:
        # Arm B gets candidate grounding on exactly the same terms as arm A.
        # An arm denied a pipeline fix the other arm has is not a comparison of
        # context modes any more -- it is context mode confounded with whether
        # prices reached the risk manager, which is the larger effect of the
        # two. Arm B runs its own analyst, so it grounds its own candidates
        # against its own MarketData.
        armb_quotes: dict[str, float] = {}
        armb_health: dict = {}

        def _quotes_md_arm_b(cands: dict) -> str:
            q = _grounded_candidate_prices(md2, cands, bundle, d)
            armb_quotes.update(q)
            armb_health.update(_candidate_grounding_health(cands, bundle, q))
            return _live_quotes_md(q)

        def _validate_arm_b(p: dict) -> tuple[dict, list[str]]:
            # data_health from the shared bundle but ITS OWN MarketData (md2
            # probes the feed independently) and its own candidate population.
            return _validate_plan(
                p, cfg, ledger.current_streak(con2, lane="morning"),
                ledger.trailing_weighted_r(con2, int(cfg.get("drawdown_window", 10)),
                                           lane="morning"),
                ref_prices=_grounded_ref_prices(md2, p, bundle, d, seed=armb_quotes),
                atr_map=_grounded_atr(md2, p, bundle),
                earnings_map=_grounded_earnings(md2, p, bundle),
                session_health={**_bundle_health(md2, bundle), **armb_health})

        return paired_mod.run_challenger_arm(
            con2, cfg, d,
            analyst_system=analyst_system, risk_system=risk_system,
            bundle_md=bundle_md, regime_md_text=reg_md, memory_text=mem["text"],
            playbook_version=mem["playbook_version"], llm_call=llm_call,
            validate=_validate_arm_b, quotes_md_for=_quotes_md_arm_b,
            regime_tag=regime_tag, model=model)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    finally:
        con2.close()


def run(cfg: Config, run_date: date | None = None, force: bool = False) -> None:
    d = run_date or today_et()
    started = now_et().replace(tzinfo=None)
    con = ledger.connect()
    lock = None

    if not is_market_day(d) and not force:
        log("morning", f"{d} is not a market day — skipping (use --force to override)")
        ledger.log_run(con, "morning", d, started, "skipped")
        con.close()
        return

    # A dry run builds the prompt and stops. It must not claim the day's lock:
    # it used to mark it 'complete', after which the REAL morning run that day
    # was suppressed as a duplicate trigger.
    if not cfg.mock and not cfg.dry_run:
        lock = ledger.acquire_run_lock(con, d, "morning")
        if not lock["acquired"]:
            log("morning", f"Duplicate trigger suppressed for {d} (lock status={lock['status']})")
            con.close()
            return

    paired_thread: threading.Thread | None = None
    try:
        # 1. data
        log("morning", "Pulling market data bundle...")
        md = MarketData(cfg)
        bundle = md.morning_bundle(d)
        bundle_md = render_bundle_md(bundle)

        # 2. regime
        log("morning", "Computing market regime...")
        reg = compute_regime(cfg, d)
        ledger.save_regime(con, reg)
        reg_md = regime_md(reg)

        # 3. memory
        mem = build_morning_memory(con, cfg)

        # 4. build prompts
        analyst_system = load_prompt("morning_analyst", {})
        user_block = (
            f"# TODAY: {d.isoformat()} ({d.strftime('%A')})\n\n"
            f"## MARKET DATA\n\n{bundle_md}\n\n"
            f"## REGIME READ (computed)\n\n{reg_md}\n\n"
            f"{mem['text']}\n\n"
            "Produce your candidate list now, following the output contract exactly."
        )

        if cfg.dry_run:
            path = REPORTS_DIR / f"{d.isoformat()}_morning_prompt.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(analyst_system + "\n\n---\n\n" + user_block, encoding="utf-8")
            log("morning", f"DRY RUN — prompt written to {path}, no API call made")
            ledger.log_run(con, "morning", d, started, "dry_run")
            return

        # 5. stage 1: analyst
        llm = LLMClient(cfg)
        model = cfg.models["morning"]

        # risk_system doesn't depend on the analyst's output, so it's built
        # here (rather than after stage 1, where it used to live) so arm B
        # -- a full independent analyst+risk round trip -- can be kicked off
        # on a background thread right now and run concurrently with arm A's
        # two LLM calls below, instead of stacking after them. That's the
        # difference between ~1x and ~2x the per-call wall time in a
        # synchronous serverless request with a hard 300s budget.
        risk_system = load_prompt("morning_risk", {
            "MAX_PICKS": str(cfg.get("max_picks", 4)),
            "MIN_SIZE_R": str(cfg.get("min_size_r", 0.25)),
            "MAX_SIZE_R": str(cfg.get("max_size_r", 1.5)),
            "MAX_TOTAL_RISK_R": str(cfg.get("max_total_open_risk_r", 3.0)),
        })

        from . import paired as paired_mod
        paired_box: dict = {}
        if not cfg.mock and paired_mod.should_run_paired(cfg, d):
            def _bg() -> None:
                paired_box["arm_b"] = _run_paired_arm_b(
                    cfg, d, analyst_system, risk_system, bundle_md, bundle, reg_md,
                    mem, reg.get("tag", ""), model, llm.call)
            paired_thread = threading.Thread(target=_bg, daemon=True)
            paired_thread.start()
            log("morning", "Paired challenger arm B started in background...")

        log("morning", f"Stage 1/2 — analyst call ({model})...")
        analyst = llm.call(
            system=analyst_system, user=user_block, model=model,
            mock_name="morning_analyst", thinking=True, web_search=True,
        )
        candidates = analyst.parsed or {}
        n_cand = len(candidates.get("candidates", []))
        log("morning", f"Analyst produced {n_cand} candidates")

        # Between stage 1 and stage 2: price every candidate the bundle never
        # quoted, while the risk manager can still act on it. See
        # _grounded_candidate_prices -- this is the ordering fix, and it is the
        # difference between "the market offered nothing" and "our plumbing
        # offered nothing."
        cand_quotes = _grounded_candidate_prices(md, candidates, bundle, d)
        quotes_md = _live_quotes_md(cand_quotes)

        corr_md = "" if cfg.offline else _correlation_matrix_md(
            [c.get("ticker", "") for c in candidates.get("candidates", [])])

        # 6. stage 2: risk manager
        risk_user = (
            f"# TODAY: {d.isoformat()}\n\n"
            f"## THE ANALYST'S CANDIDATES\n\n```json\n{json.dumps(candidates, indent=2)}\n```\n\n"
            f"## MARKET DATA\n\n{bundle_md}\n\n"
            + (f"{quotes_md}\n" if quotes_md else "")
            + (f"{corr_md}\n\n" if corr_md else "")
            + f"## REGIME READ\n\n{reg_md}\n\n"
            f"{mem['text']}\n\n"
            "Run every candidate through the kill checklist and produce the final plan now."
        )
        log("morning", f"Stage 2/2 — risk manager call ({model})...")
        risk = llm.call(
            system=risk_system, user=risk_user, model=model,
            mock_name="morning_risk", thinking=True, web_search=False,
        )
        plan = risk.parsed or {}
        plan["analyst_candidate_count"] = n_cand
        plan["risk_rejection_count"] = len(plan.get("rejected", []) or [])
        _add_exploration_pick(candidates, plan, cfg)
        plan["validator_proposed_count"] = len(plan.get("picks", []) or [])

        # 7. validate + persist (ref prices cover every proposed ticker — live-fetched if needed)
        session_health = {**_bundle_health(md, bundle),
                          **_candidate_grounding_health(candidates, bundle, cand_quotes)}
        plan, warnings = _validate_plan(
            # Lane-scoped: the streak clamp and drawdown breaker are this
            # lane's discipline and read this lane's record only (see
            # ledger.lane_clause -- runner picks would otherwise fill the window).
            plan, cfg, ledger.current_streak(con, lane="morning"),
            ledger.trailing_weighted_r(con, int(cfg.get("drawdown_window", 10)),
                                       lane="morning"),
            ref_prices=_grounded_ref_prices(md, plan, bundle, d, seed=cand_quotes),
            atr_map=_grounded_atr(md, plan, bundle),
            earnings_map=_grounded_earnings(md, plan, bundle),
            session_health=session_health)
        plan["validator_drop_count"] = max(
            0, plan["validator_proposed_count"] - len(plan.get("picks", []) or []))
        for w in warnings:
            log("morning", f"VALIDATE: {w}")
        plan["regime_tag"] = reg.get("tag", "")
        plan["prompt_version"] = f"analyst_v{prompt_version('morning_analyst')}/risk_v{prompt_version('morning_risk')}"
        plan["playbook_version"] = mem["playbook_version"]
        usage = merge_usage(analyst.usage, risk.usage)

        if cfg.mock:
            # Mock mode exists to exercise prompts/parsing/validation for free --
            # it must never write a fake plan into the same ledger real trading
            # days use, or email one out as if it were today's picks. This is
            # exactly how a canned mocks/morning_risk.json fixture ended up as
            # real-looking NVDA/CVNA/AMD picks (and, via a matching mock evening
            # run, fabricated grades and lessons) sitting in production.
            log("morning", f"MOCK RUN — not persisted, no email sent. Would-be plan: "
                           f"no_trade={plan.get('no_trade')}, {len(plan.get('picks', []))} pick(s)")
            for p in plan.get("picks", []):
                log("morning", f"  [mock] {p['ticker']} {p['direction']} {p['setup_type']} | "
                               f"entry {p['entry_price']} stop {p['stop_price']} target {p['target_price']}")
            ledger.log_run(con, "morning", d, started, "mock", model, usage)
            return

        # 8. persist
        _attach_overlay_context(plan, bundle)
        rejections = _build_rejections(candidates, plan)
        plan["persisted_rejection_count"] = len(rejections)
        plan["no_trade_category"] = _no_trade_category(plan, rejections)
        pick_ids = ledger.save_daily_plan(con, d, plan, candidates, reg.get("tag", ""))
        ledger.append_plan_run(con, d, "morning", lock["request_id"], plan)
        n_rej = ledger.save_rejections(con, d, rejections)
        # A degraded session is one the pipeline could not EVALUATE, as opposed
        # to one it evaluated and declined. Both are stored — the degraded rate
        # is itself operational evidence — but only the second is a decision,
        # and runs.status is where that distinction has to live for anything
        # downstream to honor it. `runs.status` is already free text, so this
        # needs no migration.
        health = plan.get("data_health") or {}
        if health.get("degraded"):
            log("morning", "SESSION DEGRADED — the pipeline could not evaluate the market: "
                           + "; ".join(health.get("degraded_reasons") or ["(unspecified)"])
                           + ". This is logged as an outage, NOT as a stand-down decision.")
        ledger.log_run(con, "morning", d, started,
                       "degraded" if health.get("degraded") else "ok", model, usage)

        # Live paper orders are a separate ledger concern. The simulator's
        # picks are already locked above; a broker failure cannot change them.
        if cfg.get("live_paper", {}).get("enabled") and plan.get("picks"):
            try:
                from .. import live_broker
                # Explicit slice, not membership-testing the id list: the old
                # `pick_id not in pick_ids[:max]` form only emulated an index
                # check while pick_ids happened to be unique and ordered, and
                # would mis-slice (silently, and in O(n^2)) if an id ever
                # repeated. Behavior-identical in the normal case.
                max_live = int(cfg["live_paper"].get("max_picks_per_day", 4))
                for p, pick_id in itertools.islice(zip(plan["picks"], pick_ids), max_live):
                    entry = float(p["entry_price"])
                    stop = float(p["stop_price"])
                    qty = live_broker.quantity_for_r(
                        p.get("size_r", 0), cfg.get("dollars_per_r", 500), entry, stop)
                    order = live_broker.submit_bracket_order(
                        p["ticker"], qty, "buy" if p["direction"] == "long" else "sell",
                        p.get("entry_type") or (p.get("entry_plan") or {}).get("type", "open"),
                        entry, stop, float(p["target_price"]), cfg=cfg)
                    if order:
                        if ((p.get("entry_type") or (p.get("entry_plan") or {}).get("type", "open")).lower()
                                == "breakout" and cfg["live_paper"].get("breakout_two_step")):
                            order["order_class"] = "stop_then_oco"
                        ledger.save_live_order(
                            con, pick_id, order, d, p["ticker"],
                            "buy" if p["direction"] == "long" else "sell", qty,
                            p.get("entry_type") or (p.get("entry_plan") or {}).get("type", "open"))
            except Exception as e:
                log("morning", f"WARN live paper placement failed; simulation unchanged: {e}")

        # 9. deliver
        if plan.get("no_trade"):
            log("morning", f"PLAN: NO TRADE — {plan.get('no_trade_reason','')}")
        else:
            log("morning", f"PLAN: {len(plan['picks'])} picks, total risk {plan['total_risk_r']}R")
            for p in plan["picks"]:
                log("morning", f"  {p['ticker']} {p['direction']} {p['setup_type']} | "
                               f"entry {p['entry_price']} stop {p['stop_price']} target {p['target_price']} "
                               f"| {p['size_r']}R conv {p['conviction']}")
        log("morning", f"Tokens: {usage['input_tokens']} in / {usage['output_tokens']} out "
                       f"(~${usage['cost_usd']:.3f}, {usage['web_searches']} web searches)")

        send_email(cfg, f"Trade Agent — Morning Plan {d.isoformat()}",
                   morning_email_html(d, plan, reg_md))
        render_dashboard(con, cfg, d)
        log("morning", f"Done. {len(pick_ids)} picks logged, {n_rej} rejections logged to shadow ledger.")

        # v4: snapshot the pre-selection gap-scan population (the unconditional
        # null). Runs last and is internally guarded — it can never fail the
        # morning run. was_picked reflects the final validated plan since this
        # runs after save_daily_plan.
        from .. import integrity
        integrity.on_morning(con, cfg, d, bundle, plan)

        # v4: paired challenger arm B — same bundle/regime/memory arm A saw,
        # context_mode ablated per config (default: aggregate_only, the ungated
        # last-20-trades channel). Arm B never trades and is never written to
        # picks; graded later by the SAME simulator in evening.py. Never runs
        # under mock (no real plan A was persisted to pair against) or dry-run.
        # It was started on a background thread back in step 5 (concurrently
        # with arm A's own analyst+risk calls) — join it here now that arm A
        # is fully done.
        if paired_thread is not None:
            log("morning", "Waiting for paired challenger arm B (background)...")
            paired_thread.join()
            arm_b = paired_box.get("arm_b") or {"ok": False, "reason": "background thread produced no result"}
            if arm_b.get("ok"):
                log("morning", f"Arm B ({arm_b['context_mode']}): {arm_b['picks']} pick(s), "
                               f"no_trade={arm_b['no_trade']}, ~${arm_b['cost_usd']:.3f}")
            else:
                log("morning", f"Arm B skipped/failed: {arm_b.get('reason')}")
        ledger.finish_run_lock(con, d, "morning", lock["request_id"], "complete")

    except Exception as e:
        # str(e) alone loses the stack, and a bare "error" status cannot tell a
        # provider outage from a defect in this code -- both were true of the
        # 2026-09-22 failures. See agent/failures.py.
        status = classify_failure(e)
        detail = format_failure(e)
        if lock:
            ledger.finish_run_lock(con, d, "morning", lock["request_id"], "failed")
        ledger.log_run(con, "morning", d, started, status, error=detail)
        log("morning", f"ERROR: {e}")
        # arm A failed before reaching its own join point above — still wait
        # (briefly) for the background arm B thread so it isn't left running
        # against a connection this function is about to close out from under it.
        if paired_thread is not None:
            paired_thread.join(timeout=120)
        raise
    finally:
        con.close()
