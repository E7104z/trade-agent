"""
WEEKLY LOOP (Sunday)

  full-ledger pattern mining -> promote/retire lessons -> REWRITE the playbook
  (the agent's accumulated skill, version-bumped and archived) -> report.

v3 (specs/WEEKLY_MINER_SPEC_v3.md): the miner is the SOLE writer of playbook.md.
It applies decay first, recomputes calibration, summarizes the shadow ledger
(without auto-loosening filters), reports progress on every open question,
enforces WR suppression below n=8 at write time, and commits the playbook to git.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date

from .. import ledger
from ..config import BASE_DIR, Config
from ..delivery import render_dashboard, send_email, weekly_email_html
from ..llm import LLMClient, load_prompt
from ..memory import build_weekly_memory, write_playbook
from ..util import log, now_et, today_et

STALE_SESSIONS = 30  # >= this many sessions without confirmation -> auto-demote


def _fmt_stats(stats: list[dict], label: str) -> str:
    lines = [f"### by {label}"]
    for s in stats:
        wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "-"
        extra = ""
        if s.get("t_stat") is not None:
            extra += f", stdR={s.get('std_r')}, t={s['t_stat']}"
        if s.get("baseline_avg_r") is not None:
            extra += f", naiveBaselineAvgR={s['baseline_avg_r']:+.2f}"
        lines.append(f"- {s['dim']}: n={s['n']}, triggered={s['triggered']}, wr={wr}, "
                     f"avgR={s['avg_r']}, totalWeightedR={s['total_weighted_r']}{extra}")
    return "\n".join(lines)


def _counterfactuals_md(mem: dict) -> str:
    """Null-model, trade-management, and sizing diagnostics — computed in code,
    never by the LLM. Edge must beat the baseline; management must beat holding."""
    m = mem.get("management", {})
    sc = mem.get("sizing_cf", {})
    stop_rate = (f"{m['stopped_then_target']}/{m['stopped']}"
                 if m.get("stopped") else "0/0")
    return (
        "## DETERMINISTIC COUNTERFACTUALS (computed by the system, not the LLM)\n\n"
        "### Edge vs null model (naive same-name open->close, planned-risk units)\n"
        f"- triggered trades: {m.get('triggered', 0)}; avgR={m.get('avg_r')}, "
        f"naive baseline avgR={m.get('avg_baseline_r')}, "
        f"EDGE VS BASELINE={m.get('edge_vs_baseline_r')} "
        "(<= 0 means the plan machinery adds nothing over buying/shorting the same names at the open)\n"
        f"- untriggered picks with baseline: {m.get('untriggered_with_baseline', 0)}, "
        f"avg baseline R walked away from: {m.get('avg_baseline_r_untriggered')} "
        "(large positive = entries too cute)\n\n"
        "### Trade management quality\n"
        f"- management edge (actual R minus hold-to-close R, avg): {m.get('management_edge_r')} "
        "(negative = the stop/target pair is destroying value vs just holding)\n"
        f"- stopped trades that hit target later same day: {stop_rate} "
        f"(avg eod R of stopped trades: {m.get('avg_eod_r_on_stopped')}) "
        "— a high rate means stops sit inside the noise\n\n"
        "### Sizing counterfactual\n"
        f"- n={sc.get('n', 0)}: actual size-weighted total {sc.get('actual_weighted_r')}R vs "
        f"flat-size {sc.get('flat_size_weighted_r')}R -> sizing edge {sc.get('sizing_edge_r')}R "
        "(negative at n>=20 = sizing decisions are destroying value; feed this into the calibration verdict)"
    )


def _v3_sections(mem: dict) -> str:
    """Render calibration, shadow, staleness, and question-progress data for the miner."""
    cal = mem.get("calibration", [])
    cal_md = "\n".join(
        f"- conviction {c['conviction']}: n={c['n']}, wr={c['win_rate']}, avgR={c['avg_r']}"
        for c in cal
    ) or "(no realized calibration rows yet)"

    sh = mem.get("shadow", {})
    reasons_md = "\n".join(
        f"  - \"{r['reason']}\": n={r['n']}, net forgone {r['net_forgone_r']:+.2f}R"
        for r in sh.get("by_reason", [])
    ) or "  (no scored rejections yet)"
    shadow_md = (
        f"- trailing sessions with shadow data: {sh.get('sessions', 0)}\n"
        f"- total rejected: {sh.get('total', 0)} "
        f"(would_have_won {sh.get('would_have_won', 0)}, "
        f"would_have_lost {sh.get('would_have_lost', 0)}, "
        f"scratch {sh.get('scratch', 0)}, unmeasurable {sh.get('unmeasurable', 0)})\n"
        f"- NET FORGONE R (positive = filters cost us): {sh.get('net_forgone_r', 0):+.2f}\n"
        f"- captured R by taken trades over same window: {sh.get('captured_r_window', 0):+.2f}\n"
        f"- forgone R grouped by rejection reason:\n{reasons_md}"
    )

    stale = mem.get("rule_staleness", [])
    stale_md = "\n".join(
        f"- {r['rule_id']}: last_confirmed {r['last_confirmed']}, "
        f"{r['sessions_since']} sessions since"
        + ("  << STALE: AUTO-DEMOTE to UNVALIDATED" if r["sessions_since"] >= STALE_SESSIONS else "")
        for r in stale
    ) or "(no rule confirmations recorded yet — rules keep their current last_confirmed values)"

    qp = mem.get("question_progress", [])
    qp_md = "\n".join(
        f"- Q{q['question_id']}: +{q['total']} this ledger, last event {q['last_event']}; "
        f"recent notes: " + " | ".join(q["recent_notes"][:3])
        for q in qp
    ) or "(no open-question events yet — report Progress unchanged, do not silently drop any question)"

    return (
        f"## CALIBRATION ROWS (aggregate by stated conviction)\n\n{cal_md}\n\n"
        f"## SHADOW LEDGER (trailing 20 sessions)\n\n{shadow_md}\n\n"
        f"## RULE CONFIRMATIONS / STALENESS (decay threshold: {STALE_SESSIONS} sessions)\n\n{stale_md}\n\n"
        f"## OPEN QUESTION PROGRESS EVENTS THIS LEDGER\n\n{qp_md}"
    )


def _check_wr_suppression(playbook_md: str) -> list[str]:
    """Write-time enforcement (spec §5): flag any line carrying wr/avgR at n<8."""
    violations = []
    for line in playbook_md.splitlines():
        m = re.search(r"n\s*=\s*(\d+)", line)
        if m and int(m.group(1)) < 8 and re.search(r"(wr\s*=|avgR\s*=\s*[+-]?\d)", line, re.IGNORECASE):
            violations.append(line.strip()[:120])
    return violations


def _git_commit_playbook(version: str) -> None:
    """Best-effort: commit the rewritten playbook so its evolution is a readable diff."""
    if not (BASE_DIR / ".git").exists():
        log("weekly", "No git repo — skipping playbook commit (git init to enable diff history)")
        return
    try:
        subprocess.run(["git", "add", "playbook.md", "playbook_history"],
                       cwd=BASE_DIR, check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "commit", "-m", f"playbook v{version} — weekly miner rewrite"],
                       cwd=BASE_DIR, check=True, capture_output=True, timeout=30)
        log("weekly", f"Playbook v{version} committed to git")
    except Exception as e:
        log("weekly", f"Git commit skipped: {e}")


def run(cfg: Config, run_date: date | None = None) -> None:
    d = run_date or today_et()
    started = now_et().replace(tzinfo=None)
    con = ledger.connect()

    try:
        mem = build_weekly_memory(con, cfg)
        overall = mem["overall"]
        log("weekly", f"Mining ledger: {overall['picks_graded']} graded picks, "
                      f"{len(mem['lessons'])} lessons on file, playbook v{mem['playbook_version']}")

        lessons_md = "\n".join(
            f"- [L{l['lesson_id']}] status={l['status']}, conf={l['confidence']}, "
            f"+{l['confirmations']}/-{l['contradictions']}, source={l['source']}, "
            f"setup={l['setup_type'] or 'any'}: {l['lesson']}"
            for l in mem["lessons"]
        ) or "(no lessons recorded yet)"

        regimes_md = "\n".join(
            f"- {r['date']}: {r['tag']}" for r in mem["regimes"]
        ) or "(no regime history)"

        system = load_prompt("weekly_miner", {})
        user = (
            f"# WEEKLY REVIEW — {d.isoformat()}\n\n"
            f"## OVERALL RECORD\n\n{json.dumps(overall, indent=2)}\n\n"
            f"{_fmt_stats(mem['by_setup'], 'setup_type')}\n\n"
            f"{_fmt_stats(mem['by_regime'], 'regime_tag')}\n\n"
            f"{_fmt_stats(mem['by_conviction'], 'conviction')}\n\n"
            f"{_fmt_stats(mem['by_direction'], 'direction')}\n\n"
            f"{_counterfactuals_md(mem)}\n\n"
            f"{_v3_sections(mem)}\n\n"
            f"## FULL TRADE LEDGER (CSV)\n\n```\n{mem['trades_csv']}\n```\n\n"
            f"## LESSON BANK (with IDs for your lesson_actions)\n\n{lessons_md}\n\n"
            f"## REGIME HISTORY (last 30 reads)\n\n{regimes_md}\n\n"
            f"## CURRENT PLAYBOOK (v{mem['playbook_version']}) — you are rewriting this\n\n"
            f"{mem['playbook']}\n\n"
            "Apply decay FIRST, then mine the ledger and produce the full output contract now."
        )

        model = cfg.models["weekly"]
        log("weekly", f"Pattern-mining call ({model})...")
        llm = LLMClient(cfg)
        result = llm.call(system=system, user=user, model=model,
                          mock_name="weekly_miner", thinking=True, web_search=False)
        mined = result.parsed or {}

        # apply lesson actions
        for act in mined.get("lesson_actions", []) or []:
            lid, action = act.get("lesson_id"), act.get("action")
            if not isinstance(lid, int):
                continue
            if action == "promote":
                ledger.set_lesson_status(con, lid, "promoted")
            elif action == "retire":
                ledger.set_lesson_status(con, lid, "retired")
            log("weekly", f"Lesson L{lid}: {action} — {act.get('reason','')}")

        for l in mined.get("new_lessons", []) or []:
            lid = ledger.insert_lesson(con, l, "weekly", d)
            log("weekly", f"New mined lesson L{lid}: {l['lesson']}")

        # rewrite the playbook (this loop is the SOLE writer of playbook.md)
        new_pb = mined.get("playbook_markdown", "")
        if new_pb and len(new_pb) > 200:
            violations = _check_wr_suppression(new_pb)
            for v in violations:
                log("weekly", f"WARN WR-suppression violation (n<8 with wr/avgR shown): {v}")
            version = write_playbook(new_pb, mined.get("changelog", ""), con)
            log("weekly", f"Playbook rewritten -> v{version} (old version archived)")
            if not os.environ.get("DATABASE_URL"):
                _git_commit_playbook(version)
        else:
            log("weekly", "WARN: miner returned no usable playbook — keeping current version")

        ledger.log_run(con, "weekly", d, started, "ok", model, result.usage)

        log("weekly", f"Summary: {mined.get('summary_for_email','(none)')}")
        if mined.get("current_best_edge"):
            log("weekly", f"Current best edge: {mined['current_best_edge']}")
        for f in mined.get("key_findings", []) or []:
            log("weekly", f"  Finding: {f.get('finding','')} (n={f.get('n','?')}, {f.get('strength','')})")
        for ex in mined.get("experiments", []) or []:
            log("weekly", f"  Experiment: {ex}")
        log("weekly", f"Tokens ~${result.usage['cost_usd']:.3f}")

        send_email(cfg, f"Trade Agent — Weekly Review {d.isoformat()}", weekly_email_html(mined))
        render_dashboard(con, cfg, None)

    except Exception as e:
        ledger.log_run(con, "weekly", d, started, "error", error=str(e))
        log("weekly", f"ERROR: {e}")
        raise
    finally:
        con.close()
