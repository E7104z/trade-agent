"""
Memory assembly — the four layers fed into every morning call:

  1. Trade Ledger   -> last-N trades with outcomes + aggregate stats
  2. Lesson Bank    -> active lessons ranked by confidence and confirmations
  3. Evolved Playbook -> playbook.md locally (git-tracked); playbook_versions
     table when hosted on Vercel+Neon (filesystem is read-only there)
  4. Regime State   -> today's regime + recent history

Plus self-awareness: streaks, hot/cold setups, sizing guidance derived from the ledger.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import date
from typing import Any

from . import ledger
from .config import PLAYBOOK_HISTORY_DIR, PLAYBOOK_PATH, Config

_HOSTED = lambda: bool(os.environ.get("DATABASE_URL"))  # noqa: E731


# ------------------------------------------------------------------- playbook

def read_playbook(con: Any = None) -> tuple[str, str]:
    """Returns (playbook_text, version). DB-backed when hosted, file-backed locally."""
    if _HOSTED() and con is not None:
        row = con.execute(
            "SELECT version, markdown FROM playbook_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row:
            return row[1], str(row[0])
        # first run on a fresh DB: seed from the repo's committed playbook.md (v1)
        if PLAYBOOK_PATH.exists():
            text = PLAYBOOK_PATH.read_text(encoding="utf-8")
            m = re.search(r"<!--\s*version:\s*(\d+)\s*-->", text)
            version = int(m.group(1)) if m else 1
            con.execute(
                "INSERT INTO playbook_versions (version, markdown, changelog) VALUES (?, ?, ?)",
                [version, text, "seeded from repo playbook.md"],
            )
            return text, str(version)
        return "(no playbook yet)", "0"
    if not PLAYBOOK_PATH.exists():
        return "(no playbook yet)", "0"
    text = PLAYBOOK_PATH.read_text(encoding="utf-8")
    m = re.search(r"<!--\s*version:\s*(\d+)\s*-->", text)
    return text, (m.group(1) if m else "1")


def write_playbook(new_markdown: str, changelog: str, con: Any = None) -> str:
    """Archive current playbook, write new one with bumped version. Returns new version."""
    old_text, old_version = read_playbook(con)
    new_version = str(int(old_version) + 1)
    # ensure version header present
    if "<!-- version:" in new_markdown:
        new_markdown = re.sub(r"<!--\s*version:\s*\d+\s*-->", f"<!-- version: {new_version} -->", new_markdown)
    else:
        new_markdown = f"<!-- version: {new_version} -->\n\n" + new_markdown
    if changelog:
        new_markdown += f"\n\n---\n\n## Changelog (v{new_version})\n\n{changelog}\n"

    if _HOSTED() and con is not None:
        con.execute(
            "INSERT INTO playbook_versions (version, markdown, changelog) VALUES (?, ?, ?)",
            [int(new_version), new_markdown, changelog],
        )
        return new_version

    if PLAYBOOK_PATH.exists():
        PLAYBOOK_HISTORY_DIR.mkdir(exist_ok=True)
        shutil.copy2(PLAYBOOK_PATH, PLAYBOOK_HISTORY_DIR / f"playbook_v{old_version}.md")
    PLAYBOOK_PATH.write_text(new_markdown, encoding="utf-8")
    return new_version


# ------------------------------------------------------------- context blocks

def _trades_table(trades: list[dict]) -> str:
    if not trades:
        return "(no graded trades yet — this is early in the track record; be conservative)"
    header = ("| date | ticker | dir | setup | conv | size_r | triggered | exit | R | MFE | MAE | grade |\n"
              "|---|---|---|---|---|---|---|---|---|---|---|---|")
    lines = [header]
    for t in trades:
        def f(x, d=2):
            return "" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else str(x))
        lines.append(
            f"| {t['plan_date']} | {t['ticker']} | {t['direction']} | {t['setup_type']} "
            f"| {f(t['conviction'],0)} | {f(t['size_r'])} | {'Y' if t['triggered'] else 'N'} "
            f"| {t['exit_reason'] or ''} | {f(t['r_multiple'])} | {f(t['mfe_r'])} "
            f"| {f(t['mae_r'])} | {t['grade'] or ''} |"
        )
    return "\n".join(lines)


def _stats_table(stats: list[dict], label: str) -> str:
    """WR/avgR are SUPPRESSED below n=8 triggered trades (playbook v3): LLMs anchor
    on numbers over caveats, so small-sample rates never enter the morning context."""
    if not stats:
        return f"(no {label} stats yet)"
    lines = [f"| {label} | picks | triggered | win rate | avg R | total wR |", "|---|---|---|---|---|---|"]
    for s in stats:
        if s["triggered"] >= 8:
            wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "-"
            avg = f"{s['avg_r']:+.2f}" if s["avg_r"] is not None else "-"
        else:
            wr = avg = "n<8 suppressed"
        lines.append(f"| {s['dim']} | {s['n']} | {s['triggered']} | {wr} | {avg} | {s['total_weighted_r']:+.2f} |")
    return "\n".join(lines)


def _lessons_block(lessons: list[dict]) -> str:
    if not lessons:
        return "(lesson bank is empty — first days of operation)"
    lines = []
    for l in lessons:
        net = (l["confirmations"] or 0) - (l["contradictions"] or 0)
        lines.append(
            f"- [L{l['lesson_id']}] ({l['confidence']}, net confirmations {net:+d}, "
            f"setup={l['setup_type'] or 'any'}, regime={l['regime'] or 'any'}) {l['lesson']}"
        )
    return "\n".join(lines)


def _regime_block(regimes: list[dict]) -> str:
    if not regimes:
        return "(no regime history yet)"
    lines = []
    for r in regimes:
        lines.append(
            f"- {r['date']}: {r['tag']} | VIX {r['vix']:.1f} ({r['vix_5d_chg_pct']:+.1f}% 5d) "
            f"| SPY vs 20d {r['spy_vs_20d_pct']:+.1f}% | breadth {r['sectors_above_20d']}/11 sectors > 20d"
            if r.get("vix") is not None else f"- {r['date']}: {r['tag']}"
        )
    return "\n".join(lines)


def _self_awareness(con: duckdb.DuckDBPyConnection) -> str:
    streak = ledger.current_streak(con)
    overall = ledger.overall_stats(con)
    setup_stats = ledger.stats_by(con, "setup_type")

    lines = []
    if overall["picks_graded"] == 0:
        lines.append("- Track record: EMPTY. You have no proven edge yet. Size at the minimum, "
                      "prefer the highest-quality A+ setups only, and use no_trade freely.")
    else:
        wr = f"{overall['win_rate']:.0%}" if overall["win_rate"] is not None else "n/a"
        lines.append(
            f"- Track record: {overall['triggered']} triggered trades, win rate {wr}, "
            f"avg R {overall['avg_r'] if overall['avg_r'] is not None else 'n/a'}, "
            f"total weighted R {overall['total_weighted_r']:+.2f}."
        )
    if streak["kind"] == "losing" and streak["length"] >= 3:
        lines.append(
            f"- WARNING: You are on a {streak['length']}-trade LOSING streak. Per your risk policy, "
            "cut size (max 0.5R per pick today) and demand A+ quality or use no_trade."
        )
    elif streak["kind"] == "winning" and streak["length"] >= 3:
        lines.append(
            f"- You are on a {streak['length']}-trade winning streak. Do NOT get euphoric or oversize; "
            "keep process identical."
        )
    hot = [s for s in setup_stats if s["triggered"] >= 8 and (s["win_rate"] or 0) >= 0.6]
    cold = [s for s in setup_stats if s["triggered"] >= 8 and (s["win_rate"] or 1) <= 0.35]
    if hot:
        lines.append("- Hot setups (win rate >= 60%, n >= 8): " + ", ".join(f"{s['dim']} ({s['win_rate']:.0%}, n={s['triggered']})" for s in hot))
    if cold:
        lines.append("- Cold setups (win rate <= 35%, n >= 8): " + ", ".join(f"{s['dim']} ({s['win_rate']:.0%}, n={s['triggered']})" for s in cold)
                      + " — avoid or demand exceptional quality.")
    return "\n".join(lines)


def build_morning_memory(con: duckdb.DuckDBPyConnection, cfg: Config) -> dict:
    """Returns {'text': <markdown block>, 'playbook_version': str}."""
    playbook, version = read_playbook(con)
    mem_cfg = cfg.memory_cfg
    trades = ledger.recent_trades(con, mem_cfg["recent_trades_in_context"])
    lessons = ledger.active_lessons(con, mem_cfg["max_active_lessons_in_context"])
    regimes = ledger.recent_regimes(con, 5)
    setup_stats = ledger.stats_by(con, "setup_type")

    text = f"""## YOUR PLAYBOOK (v{version} — your accumulated, evidence-backed rules)

{playbook}

## YOUR LESSON BANK (active lessons, most trusted first)

{_lessons_block(lessons)}

## YOUR LAST {len(trades)} GRADED TRADES

{_trades_table(trades)}

## YOUR PERFORMANCE BY SETUP TYPE

{_stats_table(setup_stats, 'setup')}

## MARKET REGIME — RECENT READS (most recent first)

{_regime_block(regimes)}

## SELF-AWARENESS CHECK (computed from your ledger — obey these)

{_self_awareness(con)}
"""
    return {"text": text, "playbook_version": version}


def build_weekly_memory(con: duckdb.DuckDBPyConnection, cfg: Config) -> dict:
    """Everything the weekly miner needs."""
    playbook, version = read_playbook(con)
    return {
        "playbook": playbook,
        "playbook_version": version,
        "trades_csv": ledger.all_trades_csv(con),
        "overall": ledger.overall_stats(con),
        "by_setup": ledger.stats_by(con, "setup_type"),
        "by_regime": ledger.stats_by(con, "regime_tag"),
        "by_conviction": ledger.stats_by(con, "conviction"),
        "by_direction": ledger.stats_by(con, "direction"),
        "lessons": ledger.all_lessons(con),
        "regimes": ledger.recent_regimes(con, 30),
        # v3: the self-deception closers
        "calibration": ledger.calibration_aggregate(con),
        "shadow": ledger.shadow_summary(con, 20),
        "rule_staleness": ledger.rule_staleness(con),
        "question_progress": ledger.question_progress_totals(con),
    }
