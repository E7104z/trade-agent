"""
MORNING LOOP (6:45 AM ET)

  pull market data -> compute regime -> load memory -> two-stage Claude call
  (analyst generates candidates, risk manager kills the weak ones) ->
  validate plan -> log picks to ledger -> deliver (email + dashboard).
"""
from __future__ import annotations

import json
from datetime import date

from .. import ledger
from ..config import REPORTS_DIR, Config
from ..delivery import morning_email_html, render_dashboard, send_email
from ..indicators import beta_corr
from ..llm import LLMClient, load_prompt, merge_usage, prompt_version
from ..memory import build_morning_memory
from ..providers import MarketData, render_bundle_md, yf_history_closes
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


def _grounded_ref_prices(md: MarketData, plan: dict, bundle: dict, d: date) -> dict[str, float]:
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


def _validate_plan(plan: dict, cfg: Config, streak: dict | None = None,
                   drawdown: dict | None = None,
                   ref_prices: dict[str, float] | None = None,
                   atr_map: dict[str, float] | None = None,
                   earnings_map: dict[str, int] | None = None) -> tuple[dict, list[str]]:
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
    max_picks = int(cfg.get("max_picks", 4))
    min_r, max_r = float(cfg.get("min_size_r", 0.25)), float(cfg.get("max_size_r", 1.5))
    max_total = float(cfg.get("max_total_open_risk_r", 3.0))
    losing_streak = bool(streak and streak.get("kind") == "losing" and streak.get("length", 0) >= 3)
    atr_gate_mode = str(cfg.get("atr_gate", "warn")).lower()
    earnings_gate_mode = str(cfg.get("earnings_gate", "warn")).lower()
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


def _build_rejections(candidates: dict, plan: dict) -> list[dict]:
    """Shadow ledger input: every analyst candidate that did NOT survive to the final
    plan, carrying its full plan levels so the evening loop can grade the counterfactual."""
    taken = {p["ticker"].upper() for p in plan.get("picks", [])}
    reasons = {str(r.get("ticker", "")).upper(): r.get("reason", "")
               for r in plan.get("rejected", []) or []}
    out = []
    for c in candidates.get("candidates", []) or []:
        t = str(c.get("ticker", "")).upper()
        if not t or t in taken:
            continue
        entry_plan = c.get("entry_plan") or {}
        out.append({
            "ticker": t,
            "direction": c.get("direction"),
            "setup_type": c.get("setup_type"),
            "reason": reasons.get(t) or "not selected by risk manager (no explicit reason)",
            "entry_type": entry_plan.get("type", "open"),
            "entry_price": entry_plan.get("price", c.get("entry_price")),
            "stop_price": c.get("stop_price"),
            "target_price": c.get("target_price"),
            "conviction": c.get("conviction"),
            "thesis": c.get("thesis"),
        })
    return out


def run(cfg: Config, run_date: date | None = None, force: bool = False) -> None:
    d = run_date or today_et()
    started = now_et().replace(tzinfo=None)
    con = ledger.connect()

    if not is_market_day(d) and not force:
        log("morning", f"{d} is not a market day — skipping (use --force to override)")
        ledger.log_run(con, "morning", d, started, "skipped")
        con.close()
        return

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
        log("morning", f"Stage 1/2 — analyst call ({model})...")
        analyst = llm.call(
            system=analyst_system, user=user_block, model=model,
            mock_name="morning_analyst", thinking=True, web_search=True,
        )
        candidates = analyst.parsed or {}
        n_cand = len(candidates.get("candidates", []))
        log("morning", f"Analyst produced {n_cand} candidates")

        corr_md = "" if cfg.offline else _correlation_matrix_md(
            [c.get("ticker", "") for c in candidates.get("candidates", [])])

        # 6. stage 2: risk manager
        risk_system = load_prompt("morning_risk", {
            "MAX_PICKS": str(cfg.get("max_picks", 4)),
            "MIN_SIZE_R": str(cfg.get("min_size_r", 0.25)),
            "MAX_SIZE_R": str(cfg.get("max_size_r", 1.5)),
            "MAX_TOTAL_RISK_R": str(cfg.get("max_total_open_risk_r", 3.0)),
        })
        risk_user = (
            f"# TODAY: {d.isoformat()}\n\n"
            f"## THE ANALYST'S CANDIDATES\n\n```json\n{json.dumps(candidates, indent=2)}\n```\n\n"
            f"## MARKET DATA\n\n{bundle_md}\n\n"
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

        # 7. validate + persist (ref prices cover every proposed ticker — live-fetched if needed)
        plan, warnings = _validate_plan(
            plan, cfg, ledger.current_streak(con),
            ledger.trailing_weighted_r(con, int(cfg.get("drawdown_window", 10))),
            ref_prices=_grounded_ref_prices(md, plan, bundle, d),
            atr_map=_grounded_atr(md, plan, bundle),
            earnings_map=_grounded_earnings(md, plan, bundle))
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
        pick_ids = ledger.save_daily_plan(con, d, plan, candidates, reg.get("tag", ""))
        n_rej = ledger.save_rejections(con, d, _build_rejections(candidates, plan))
        ledger.log_run(con, "morning", d, started, "ok", model, usage)

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

    except Exception as e:
        ledger.log_run(con, "morning", d, started, "error", error=str(e))
        log("morning", f"ERROR: {e}")
        raise
    finally:
        con.close()
