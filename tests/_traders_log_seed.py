"""A fixture ledger for the trader's log tests (TRADERS_LOG_SPEC_v1)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from agent import ledger, runner_exits

D = date(2026, 10, 2)

PLAYBOOK = """# Trading Agent Playbook
<!-- version: 9 -->

## Calibration

Calibration verdict (computed, Q3): not yet computable — conviction 4-5 has n=3 of 8.

## Runner Lane

Lane stop condition: 1/20 sessions measured -- too early to read. Nothing significantly positive yet.
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
    # A runner pick, its candidates, selection, cap note and exit replays.
    from agent import runner_selection
    from agent.config import Config
    rid = runner_selection.save_runner_pick(
        con, d, {"ticker": "RUN", "detection_price": 10.0, "stop_price": 9.8, "target_price": 10.4},
        {"rationale": "x", "conviction": 3},
        Config(raw={"runner": {"size_r": 0.25}, "min_size_r": 0.25}))
    ledger.save_outcome(con, {"pick_id": rid, "triggered": True, "exit_reason": "eod",
                              "r_planned": -0.4, "r_multiple": -0.4})
    for t in ("RUN", "R2", "R3"):
        con.execute("INSERT INTO runner_candidates (session_date, ticker) VALUES (?, ?)", [d, t])
    ledger.mark_runner_selected(con, d, "RUN", 1, rid)
    ledger.upsert_runner_session(con, {
        "session_date": d, "selection_skipped_reason": "industry cap 2/industry dropped 1 (Banks: R3)"})
    v = runner_exits.POLICY_VERSION
    ledger.save_runner_exit_replays(con, d, [
        {"ticker": t, "policy": pol, "policy_version": v, "r_planned": r}
        for t, pol, r in (("RUN", "plan", -1.0), ("R2", "plan", 2.0),
                          ("RUN", "arm_a_floor", -0.5), ("R2", "arm_a_floor", 1.0))])
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
    return {**by, "RUN": rid}


def playbook(tmp_path, monkeypatch):
    from agent import memory
    path = tmp_path / "playbook.md"
    path.write_text(PLAYBOOK, encoding="utf-8")
    monkeypatch.setattr(memory, "PLAYBOOK_PATH", path)
    return path
