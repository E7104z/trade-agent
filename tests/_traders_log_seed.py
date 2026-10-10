"""A fixture ledger for the trader's log tests (TRADERS_LOG_SPEC_v1)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from agent import ledger

D = date(2026, 10, 2)

PLAYBOOK = """# Trading Agent Playbook
<!-- version: 9 -->

## Calibration

Calibration verdict (computed, Q3): not yet computable — conviction 4-5 has n=3 of 8.
"""


def _pick(ticker, direction="long", setup="gap_go", lane=None, size=0.25):
    return {"ticker": ticker, "direction": direction, "setup_type": setup, "conviction": 3,
            "size_r": size, "entry_plan": {"type": "open"}, "entry_price": 10.0,
            "stop_price": 9.5 if direction == "long" else 10.5,
            "target_price": 11.0 if direction == "long" else 9.0, "exploration_lane": lane}


def seed_session(con, d: date = D) -> dict[str, int]:
    plan = {"no_trade": False, "market_stance": "x", "regime_tag": "chop",
            "picks": [_pick("AAA"), _pick("BBB", "short", "gap_fade", "coverage"),
                      _pick("CCC", setup="breakout"), _pick("DDD"), _pick("EEE")]}
    ids = ledger.save_daily_plan(con, d, plan, {"candidates": []}, "chop")
    by = dict(zip(["AAA", "BBB", "CCC", "DDD", "EEE"], ids))
    outs = {
        "AAA": dict(triggered=True, exit_reason="target", r_planned=1.2, r_multiple=1.1),
        "BBB": dict(triggered=True, exit_reason="stop", r_planned=-1.0, r_multiple=-1.05),
        "CCC": dict(triggered=False, exit_reason="invalid_fill", r_planned=None, r_multiple=None),
        "DDD": dict(triggered=True, exit_reason="eod", r_planned=3.0, r_multiple=3.0,
                    execution_anomaly=True),
        "EEE": dict(triggered=False, exit_reason="no_trigger", r_planned=None, r_multiple=None),
    }
    for t, o in outs.items():
        ledger.save_outcome(con, {"pick_id": by[t], **o})
    # Shadow ledger: two scored rejections, one unscored.
    for t, f, graded in (("SH1", 0.8, True), ("SH2", -1.0, True), ("SH3", None, False)):
        con.execute("""INSERT INTO rejections (plan_date, ticker, reason, forgone_r, eod_outcome,
                           graded_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [d, t, "rr: R:R below 2.0", f, "would_have_won" if (f or 0) > 0 else "would_have_lost",
                     datetime(2026, 10, 2, 20, 0) if graded else None])
    # The backlog snapshot the evening just wrote.
    for rank, (typ, iid, n, eta, stalled) in enumerate((
            ("cell", "morning:stop_atr_bucket=0.5-1", 3, 4, False),
            ("question", "Q3", 5, None, True)), 1):
        con.execute("""INSERT INTO evidence_backlog (session_date, rank, item_type, item_id, lane, n,
                           target, rate_10s, eta_sessions, stalled, what_counts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    [d, rank, typ, iid, "morning", n, 8, 1, eta, stalled, f"counts for {iid}"])
    # Calibration: conviction 3 clears n>=8, conviction 5 does not.
    for i in range(9):
        ledger.add_calibration_row(con, d - timedelta(days=i + 1), f"C{i}", 3, 0.5 if i % 2 else -0.5)
    for i in range(2):
        ledger.add_calibration_row(con, d - timedelta(days=i + 1), f"F{i}", 5, 2.0)
    return dict(by)


def playbook(tmp_path, monkeypatch):
    from agent import memory
    path = tmp_path / "playbook.md"
    path.write_text(PLAYBOOK, encoding="utf-8")
    monkeypatch.setattr(memory, "PLAYBOOK_PATH", path)
    return path
