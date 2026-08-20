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
from ..llm import LLMClient, load_prompt, merge_usage, prompt_version
from ..memory import build_morning_memory
from ..providers import MarketData, render_bundle_md
from ..regime import compute_regime, regime_md
from ..util import is_market_day, log, now_et, today_et


def _validate_plan(plan: dict, cfg: Config, streak: dict | None = None) -> tuple[dict, list[str]]:
    """Hard-validate and normalize the risk manager's plan. Returns (plan, warnings).

    The playbook's losing-streak rule (>=3 losers: cap 0.5R, demand conviction >= 4)
    is enforced MECHANICALLY here — the prompt asks the risk manager to apply it,
    but discipline rules must not depend on an LLM remembering them.
    """
    warnings: list[str] = []
    max_picks = int(cfg.get("max_picks", 4))
    min_r, max_r = float(cfg.get("min_size_r", 0.25)), float(cfg.get("max_size_r", 1.5))
    max_total = float(cfg.get("max_total_open_risk_r", 3.0))
    losing_streak = bool(streak and streak.get("kind") == "losing" and streak.get("length", 0) >= 3)

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
            f"## REGIME READ\n\n{reg_md}\n\n"
            f"{mem['text']}\n\n"
            "Run every candidate through the kill checklist and produce the final plan now."
        )
        log("morning", f"Stage 2/2 — risk manager call ({model})...")
        risk = llm.call(
            system=risk_system, user=risk_user, model=model,
            mock_name="morning_risk", thinking=True, web_search=False,
        )
        plan = risk.parsed or {}

        # 7. validate + persist
        plan, warnings = _validate_plan(plan, cfg, ledger.current_streak(con))
        for w in warnings:
            log("morning", f"VALIDATE: {w}")
        plan["regime_tag"] = reg.get("tag", "")
        plan["prompt_version"] = f"analyst_v{prompt_version('morning_analyst')}/risk_v{prompt_version('morning_risk')}"
        plan["playbook_version"] = mem["playbook_version"]

        pick_ids = ledger.save_daily_plan(con, d, plan, candidates, reg.get("tag", ""))
        n_rej = ledger.save_rejections(con, d, _build_rejections(candidates, plan))
        usage = merge_usage(analyst.usage, risk.usage)
        ledger.log_run(con, "morning", d, started, "ok", model, usage)

        # 8. deliver
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
