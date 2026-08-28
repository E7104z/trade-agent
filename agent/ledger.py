"""
The Trade Ledger — append-only ground truth in DuckDB.

Everything hangs off this schema:
  picks           every idea the agent has ever committed to, with full trade plan
  outcomes        what actually happened (fills, MFE/MAE, R-multiple, exit reason)
  lessons         the lesson bank: evening extractions + weekly promotions
  daily_plans     one row per morning run (including no-trade days)
  regime_history  daily market-regime snapshot
  runs            operational log: model, tokens, cost, status per loop execution
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

from .config import DB_PATH, ensure_dirs
from .providers import INDEX_ETFS, SCAN_UNIVERSE

_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS seq_pick_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_lesson_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_run_id START 1;

CREATE TABLE IF NOT EXISTS picks (
    pick_id          INTEGER PRIMARY KEY DEFAULT nextval('seq_pick_id'),
    plan_date        DATE NOT NULL,
    ticker           VARCHAR NOT NULL,
    direction        VARCHAR NOT NULL,          -- long | short
    setup_type       VARCHAR,                   -- fixed taxonomy, see prompts
    catalyst         VARCHAR,
    thesis           VARCHAR,
    conviction       INTEGER,                   -- 1..5
    size_r           DOUBLE,                    -- planned risk in R
    entry_type       VARCHAR,                   -- open | limit | breakout
    entry_price      DOUBLE,
    stop_price       DOUBLE,
    target_price     DOUBLE,
    target2_price    DOUBLE,
    time_stop        VARCHAR,
    invalidation     VARCHAR,
    regime_tag       VARCHAR,
    prompt_version   VARCHAR,
    playbook_version VARCHAR,
    in_scan_universe BOOLEAN,                  -- was ticker in the fixed premarket scan list at pick time?
    created_at       TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS outcomes (
    pick_id        INTEGER PRIMARY KEY,
    eval_date      DATE,
    triggered      BOOLEAN,
    entry_fill     DOUBLE,
    exit_price     DOUBLE,
    exit_reason    VARCHAR,        -- target | stop | eod | no_trigger | time_stop | flatten
                                    -- (time_stop/flatten added by sim_version 2, see Task 3/4)
    r_multiple     DOUBLE,
    mfe_r          DOUBLE,
    mae_r          DOUBLE,
    day_open       DOUBLE,
    day_high       DOUBLE,
    day_low        DOUBLE,
    day_close      DOUBLE,
    day_volume     BIGINT,
    missed_move_r  DOUBLE,         -- for untriggered picks: what was left on the table
    baseline_r     DOUBLE,         -- null model: open->close in pick's direction / planned risk
    eod_r          DOUBLE,         -- triggered: R if held fill->close with no stop/target
    target_hit_after_stop BOOLEAN, -- stopped trades: did price reach target later same day?
    execution_anomaly BOOLEAN,     -- fill landed >0.5 planned-R from the plan; grade execution only
    fill_vs_plan_r DOUBLE,         -- |fill - planned entry| / planned risk
    grade          VARCHAR,        -- A..F process grade from evening loop
    grade_notes    VARCHAR,
    graded_at      TIMESTAMP
);

CREATE TABLE IF NOT EXISTS lessons (
    lesson_id      INTEGER PRIMARY KEY DEFAULT nextval('seq_lesson_id'),
    created_date   DATE,
    lesson         VARCHAR NOT NULL,
    setup_type     VARCHAR,
    regime         VARCHAR,
    tags           VARCHAR,        -- comma separated
    confidence     VARCHAR,        -- low | medium | high
    evidence       VARCHAR,
    source         VARCHAR,        -- evening | weekly
    confirmations  INTEGER DEFAULT 0,
    contradictions INTEGER DEFAULT 0,
    status         VARCHAR DEFAULT 'active'   -- active | promoted | retired
);

CREATE TABLE IF NOT EXISTS daily_plans (
    plan_date       DATE PRIMARY KEY,
    no_trade        BOOLEAN,
    market_summary  VARCHAR,
    plan_json       VARCHAR,
    analyst_json    VARCHAR,
    regime_tag      VARCHAR,
    notes_for_evening VARCHAR,
    created_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS regime_history (
    date                    DATE PRIMARY KEY,
    trend                   VARCHAR,   -- uptrend | downtrend | chop
    risk                    VARCHAR,   -- risk_on | risk_off | neutral
    vix                     DOUBLE,
    vix_5d_chg_pct          DOUBLE,
    spy_vs_20d_pct          DOUBLE,
    spy_vs_50d_pct          DOUBLE,
    sectors_above_20d       INTEGER,   -- of 11
    iwm_spy_5d_rel_pct      DOUBLE,
    realized_vol_10d        DOUBLE,
    tag                     VARCHAR,   -- compact label, e.g. 'uptrend/risk_on/lowvol'
    notes                   VARCHAR
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY DEFAULT nextval('seq_run_id'),
    loop          VARCHAR,
    run_date      DATE,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    status        VARCHAR,      -- ok | error | skipped
    model         VARCHAR,
    input_tokens  BIGINT DEFAULT 0,
    output_tokens BIGINT DEFAULT 0,
    web_searches  INTEGER DEFAULT 0,
    cost_usd      DOUBLE DEFAULT 0,
    error         VARCHAR
);

-- Playbook storage for hosted (Vercel + Neon) deployments, where the
-- filesystem is read-only outside /tmp and playbook.md can't be rewritten
-- on disk. Local CLI use keeps the git-tracked playbook.md file instead
-- (see agent/memory.py read_playbook/write_playbook for the mode switch).
CREATE TABLE IF NOT EXISTS playbook_versions (
    version      INTEGER PRIMARY KEY,
    markdown     VARCHAR NOT NULL,
    changelog    VARCHAR,
    created_at   TIMESTAMP DEFAULT current_timestamp
);

-- ------------------------------------------------------------------ v3 tables

CREATE SEQUENCE IF NOT EXISTS seq_rejection_id START 1;

CREATE TABLE IF NOT EXISTS rejections (
    rejection_id  INTEGER PRIMARY KEY DEFAULT nextval('seq_rejection_id'),
    plan_date     DATE NOT NULL,
    ticker        VARCHAR NOT NULL,
    direction     VARCHAR,
    setup_type    VARCHAR,
    reason        VARCHAR,          -- risk manager's kill reason, verbatim
    entry_type    VARCHAR,
    entry_price   DOUBLE,
    stop_price    DOUBLE,
    target_price  DOUBLE,
    conviction    INTEGER,
    thesis        VARCHAR,
    -- shadow outcome (filled by evening loop)
    eod_outcome   VARCHAR,          -- would_have_won | would_have_lost | scratch | unmeasurable
    forgone_r     DOUBLE,           -- positive = the filter cost us
    graded_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calibration (
    session_date       DATE,
    ticker             VARCHAR,
    stated_conviction  INTEGER,
    realized_r         DOUBLE,
    win                BOOLEAN
);

CREATE TABLE IF NOT EXISTS rule_confirmations (
    rule_id       VARCHAR,          -- setup_type the rule keys on
    session_date  DATE,
    pick_id       INTEGER
);

CREATE TABLE IF NOT EXISTS open_question_progress (
    question_id   INTEGER,
    session_date  DATE,
    increment     INTEGER,
    note          VARCHAR
);

-- ------------------------------------------------------- Task 5: lesson holdout

-- Per-event log of lesson "confirm" actions (mirrors rule_confirmations).
-- Needed because the in-sample/out-of-sample split (vs lessons.promoted_at)
-- and the optional trailing-window holdout (config lesson_holdout_sessions)
-- both depend on WHEN each confirmation happened, not just a running total.
CREATE TABLE IF NOT EXISTS lesson_confirmations (
    lesson_id     INTEGER,
    session_date  DATE
);

-- ------------------------------------------------- indicator layer v2 (ATR/levels/context)

-- Fundamentals move slowly; short interest is exchange-reported roughly
-- biweekly. Cached with a session-based TTL (see cached_fundamentals) so the
-- morning bundle doesn't re-fetch yfinance for every gap-scan ticker every
-- day. short_asof is ALWAYS stored alongside short_pct_float -- stale-and-
-- labeled beats fresh-looking fiction (spec: agent/providers.py fundamentals()).
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol          VARCHAR PRIMARY KEY,
    mkt_cap         DOUBLE,
    float_shares    DOUBLE,
    short_pct_float DOUBLE,
    short_asof      VARCHAR,
    fetched_date    DATE
);
"""

# Idempotent upgrades for databases created before these columns existed
# (both DuckDB and Postgres support ADD COLUMN IF NOT EXISTS).
_MIGRATIONS = [
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS baseline_r DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS eod_r DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS target_hit_after_stop BOOLEAN",
    # v3 fill-validity gate (agent/grading.py): invalid_fill trades are hard-rejected
    # before grading; surviving fills far from the planned entry are flagged here so
    # the evening grader (and weekly miner) can exclude them from thesis lessons and
    # rule confirmations instead of narrating a test that never actually happened.
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS execution_anomaly BOOLEAN",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS fill_vs_plan_r DOUBLE",
    # Task 4: version the simulation semantics. Existing rows (graded before
    # this migration ran) default to 1; everything graded from here on writes
    # CURRENT_SIM_VERSION explicitly (see agent/grading.py). Never backfilled
    # silently — see run.py regrade for the explicit, opt-in recompute path.
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS sim_version INTEGER DEFAULT 1",
    "ALTER TABLE rejections ADD COLUMN IF NOT EXISTS sim_version INTEGER DEFAULT 1",
    # Task 5: out-of-sample holdout for lesson confirmation. promoted_at is the
    # session date a lesson was written down; confirmations on/before it are
    # in-sample, after it are out-of-sample. Backfill existing lessons with
    # their creation date (idempotent -- only touches rows still NULL).
    "ALTER TABLE lessons ADD COLUMN IF NOT EXISTS promoted_at DATE",
    "UPDATE lessons SET promoted_at = created_date WHERE promoted_at IS NULL",
    # Off-universe provenance: was this ticker inside the fixed premarket scan
    # list, or did it only surface via the unrestricted movers/actives
    # screeners (or news/EDGAR)? Lets the weekly miner check whether
    # SCAN_UNIVERSE's coverage gap is actually biasing which setups confirm.
    "ALTER TABLE picks ADD COLUMN IF NOT EXISTS in_scan_universe BOOLEAN",
    # Indicator layer (specs/INDICATOR_LAYER_SPEC_v1.md §4) — VWAP/RVOL/9EMA
    # fields computed deterministically by agent/grading.py at grading time.
    # Session-level fields apply whether or not the pick triggered;
    # fill-dependent ones (vwap_at_fill, ema9_5m_loss_et) only when it did.
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS adv20 DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS vwap_at_fill DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS fill_vs_vwap_pct DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS vwap_side_at_fill VARCHAR",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS first_vwap_reclaim_et TIMESTAMP",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS first_vwap_reject_et TIMESTAMP",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS vwap_crosses INTEGER",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS pct_time_above_vwap DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS rvol_at_1000 DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS rvol_at_1200 DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS rvol_close DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS ema9_5m_loss_et TIMESTAMP",
    # Indicator layer v2 (ATR/levels/context) — agent/grading.py computes these
    # deterministically at grading time from daily_context/fundamentals passed
    # in by agent/loops/evening.py. Never gates fills or changes R math; grader
    # INPUT and miner FUEL only.
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS atr14 DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS stop_dist_atr DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS target_dist_atr DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS gap_atr DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS or15_high DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS or15_low DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS or_break_dir VARCHAR",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS or_break_et TIMESTAMP",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS fill_vs_or VARCHAR",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS mae_before_mfe BOOLEAN",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS time_to_mfe_min DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS gap_fill_frac_1000 DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS gap_fill_frac_1200 DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS gap_fill_frac_close DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS trend_side_200d VARCHAR",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS ext_atr_at_entry DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS short_pct_float DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS float_shares DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS mkt_cap DOUBLE",
]

# --- v4 research integrity layer (specs/RESEARCH_INTEGRITY_SPEC_v4.md) ---
# Appended rather than inlined so the v4 schema stays reviewable as one unit.
# connect() already executes every entry in BOTH the DuckDB and Postgres
# branches, and the Postgres branch rewrites DOUBLE -> DOUBLE PRECISION, so
# these statements need no engine-specific handling.
from . import migrations_v4  # noqa: E402
_MIGRATIONS = _MIGRATIONS + migrations_v4.STATEMENTS


def connect() -> Any:
    """
    Returns a DuckDB connection for local/CLI use, or a Postgres-backed
    connection (agent.pgshim.PGConnection) when DATABASE_URL is set — the
    mode used by the hosted (Vercel + Neon) deployment, where there is no
    persistent local disk for a DuckDB file. Both expose the same
    con.execute(sql, params).fetchone()/.fetchall() interface, so every
    query function below works unchanged in either mode.
    """
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        from .pgshim import connect_pg
        con = connect_pg(dsn)
        # DOUBLE isn't a valid standalone Postgres type name (DuckDB accepts
        # it as an alias for DOUBLE PRECISION) — translate for Postgres only.
        con.execute(re.sub(r"\bDOUBLE\b", "DOUBLE PRECISION", _SCHEMA))
        for mig in _MIGRATIONS:
            con.execute(re.sub(r"\bDOUBLE\b", "DOUBLE PRECISION", mig))
        return con
    import duckdb
    ensure_dirs()
    con = duckdb.connect(str(DB_PATH))
    con.execute(_SCHEMA)
    for mig in _MIGRATIONS:
        con.execute(mig)
    return con


# ------------------------------------------------------------------ picks/plans

def save_daily_plan(
    con: duckdb.DuckDBPyConnection,
    plan_date: date,
    plan: dict,
    analyst: dict,
    regime_tag: str,
) -> list[int]:
    """Persist plan row + individual picks. Returns inserted pick_ids."""
    con.execute("DELETE FROM daily_plans WHERE plan_date = ?", [plan_date])
    con.execute(
        """INSERT INTO daily_plans
           (plan_date, no_trade, market_summary, plan_json, analyst_json, regime_tag, notes_for_evening)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            plan_date,
            bool(plan.get("no_trade")),
            plan.get("market_stance", ""),
            json.dumps(plan),
            json.dumps(analyst),
            regime_tag,
            plan.get("notes_for_evening", ""),
        ],
    )
    # replace any picks previously stored for this date (re-runs)
    con.execute("DELETE FROM outcomes WHERE pick_id IN (SELECT pick_id FROM picks WHERE plan_date = ?)", [plan_date])
    con.execute("DELETE FROM picks WHERE plan_date = ?", [plan_date])

    ids: list[int] = []
    for p in plan.get("picks", []):
        ticker = p["ticker"].upper()
        row = con.execute(
            """INSERT INTO picks
               (plan_date, ticker, direction, setup_type, catalyst, thesis, conviction,
                size_r, entry_type, entry_price, stop_price, target_price, target2_price,
                time_stop, invalidation, regime_tag, prompt_version, playbook_version,
                in_scan_universe)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               RETURNING pick_id""",
            [
                plan_date,
                ticker,
                p["direction"],
                p.get("setup_type"),
                p.get("catalyst"),
                p.get("thesis_final") or p.get("thesis"),
                p.get("conviction"),
                p.get("size_r"),
                (p.get("entry_plan") or {}).get("type", p.get("entry_type", "open")),
                p.get("entry_price"),
                p.get("stop_price"),
                p.get("target_price"),
                p.get("target2_price"),
                p.get("time_stop"),
                p.get("invalidation"),
                plan.get("regime_tag", ""),
                plan.get("prompt_version", ""),
                plan.get("playbook_version", ""),
                ticker in SCAN_UNIVERSE or ticker in INDEX_ETFS,
            ],
        ).fetchone()
        ids.append(row[0])
    return ids


def picks_for_date(con: duckdb.DuckDBPyConnection, d: date) -> list[dict]:
    cols = [c[0] for c in con.execute("DESCRIBE picks").fetchall()]
    rows = con.execute("SELECT * FROM picks WHERE plan_date = ? ORDER BY pick_id", [d]).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def plan_for_date(con: duckdb.DuckDBPyConnection, d: date) -> dict | None:
    row = con.execute(
        "SELECT plan_date, no_trade, market_summary, plan_json, regime_tag, notes_for_evening "
        "FROM daily_plans WHERE plan_date = ?",
        [d],
    ).fetchone()
    if not row:
        return None
    return {
        "plan_date": row[0],
        "no_trade": row[1],
        "market_summary": row[2],
        "plan": json.loads(row[3]) if row[3] else {},
        "regime_tag": row[4],
        "notes_for_evening": row[5],
    }


# ------------------------------------------------------------------- outcomes

def save_outcome(con: duckdb.DuckDBPyConnection, o: dict) -> None:
    con.execute("DELETE FROM outcomes WHERE pick_id = ?", [o["pick_id"]])
    sim_version = o.get("sim_version")
    if sim_version is None:
        from .grading import CURRENT_SIM_VERSION
        sim_version = CURRENT_SIM_VERSION
    con.execute(
        """INSERT INTO outcomes
           (pick_id, eval_date, triggered, entry_fill, exit_price, exit_reason,
            r_multiple, mfe_r, mae_r, day_open, day_high, day_low, day_close,
            day_volume, missed_move_r, baseline_r, eod_r, target_hit_after_stop,
            execution_anomaly, fill_vs_plan_r,
            adv20, vwap_at_fill, fill_vs_vwap_pct, vwap_side_at_fill,
            first_vwap_reclaim_et, first_vwap_reject_et, vwap_crosses,
            pct_time_above_vwap, rvol_at_1000, rvol_at_1200, rvol_close,
            ema9_5m_loss_et,
            atr14, stop_dist_atr, target_dist_atr, gap_atr,
            or15_high, or15_low, or_break_dir, or_break_et, fill_vs_or,
            mae_before_mfe, time_to_mfe_min,
            gap_fill_frac_1000, gap_fill_frac_1200, gap_fill_frac_close,
            trend_side_200d, ext_atr_at_entry, short_pct_float, float_shares, mkt_cap,
            r_multiple_2x, r_multiple_3x, slippage_bps_used, friction_model, participation_pct,
            grade, grade_notes, graded_at, sim_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                   ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            o["pick_id"], o["eval_date"], o.get("triggered"), o.get("entry_fill"),
            o.get("exit_price"), o.get("exit_reason"), o.get("r_multiple"),
            o.get("mfe_r"), o.get("mae_r"), o.get("day_open"), o.get("day_high"),
            o.get("day_low"), o.get("day_close"), o.get("day_volume"),
            o.get("missed_move_r"), o.get("baseline_r"), o.get("eod_r"),
            o.get("target_hit_after_stop"), o.get("execution_anomaly"), o.get("fill_vs_plan_r"),
            o.get("adv20"), o.get("vwap_at_fill"), o.get("fill_vs_vwap_pct"), o.get("vwap_side_at_fill"),
            o.get("first_vwap_reclaim_et"), o.get("first_vwap_reject_et"), o.get("vwap_crosses"),
            o.get("pct_time_above_vwap"), o.get("rvol_at_1000"), o.get("rvol_at_1200"), o.get("rvol_close"),
            o.get("ema9_5m_loss_et"),
            o.get("atr14"), o.get("stop_dist_atr"), o.get("target_dist_atr"), o.get("gap_atr"),
            o.get("or15_high"), o.get("or15_low"), o.get("or_break_dir"), o.get("or_break_et"),
            o.get("fill_vs_or"), o.get("mae_before_mfe"), o.get("time_to_mfe_min"),
            o.get("gap_fill_frac_1000"), o.get("gap_fill_frac_1200"), o.get("gap_fill_frac_close"),
            o.get("trend_side_200d"), o.get("ext_atr_at_entry"), o.get("short_pct_float"),
            o.get("float_shares"), o.get("mkt_cap"),
            o.get("r_multiple_2x"), o.get("r_multiple_3x"), o.get("slippage_bps_used"),
            o.get("friction_model"), o.get("participation_pct"),
            o.get("grade"), o.get("grade_notes"),
            o.get("graded_at", datetime.now()), sim_version,
        ],
    )


def apply_grades(con: duckdb.DuckDBPyConnection, reviews: list[dict], picks: list[dict]) -> int:
    """Attach LLM process-grades to outcomes. Matches by pick_id, falls back to ticker."""
    by_ticker = {p["ticker"].upper(): p["pick_id"] for p in picks}
    n = 0
    for r in reviews:
        pid = r.get("pick_id")
        if pid is None or pid not in {p["pick_id"] for p in picks}:
            pid = by_ticker.get(str(r.get("ticker", "")).upper())
        if pid is None:
            continue
        notes = r.get("why") or r.get("what_happened") or ""
        con.execute(
            "UPDATE outcomes SET grade = ?, grade_notes = ?, graded_at = current_timestamp WHERE pick_id = ?",
            [r.get("grade"), notes, pid],
        )
        n += 1
    return n


def ungraded_picks(con: duckdb.DuckDBPyConnection, d: date) -> list[dict]:
    cols = [c[0] for c in con.execute("DESCRIBE picks").fetchall()]
    rows = con.execute(
        """SELECT p.* FROM picks p LEFT JOIN outcomes o ON p.pick_id = o.pick_id
           WHERE p.plan_date = ? AND o.pick_id IS NULL ORDER BY p.pick_id""",
        [d],
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


# -------------------------------------------------------------------- lessons

def insert_lesson(con: duckdb.DuckDBPyConnection, l: dict, source: str, created: date) -> int:
    row = con.execute(
        """INSERT INTO lessons
           (created_date, lesson, setup_type, regime, tags, confidence, evidence, source, promoted_at)
           VALUES (?,?,?,?,?,?,?,?,?) RETURNING lesson_id""",
        [
            created, l["lesson"], l.get("setup_type"), l.get("regime"),
            ",".join(l.get("tags", [])) if isinstance(l.get("tags"), list) else l.get("tags"),
            l.get("confidence", "low"), l.get("evidence"), source, created,
        ],
    ).fetchone()
    return row[0]


def update_lesson_feedback(con: duckdb.DuckDBPyConnection, lesson_id: int, action: str,
                          session_date: date | None = None) -> None:
    if action == "confirm":
        con.execute("UPDATE lessons SET confirmations = confirmations + 1 WHERE lesson_id = ?", [lesson_id])
        # Task 5: log the event date too (in addition to, not instead of, the
        # running counter above) so the weekly loop can split in-sample vs
        # out-of-sample confirmations relative to lessons.promoted_at.
        if session_date is not None:
            con.execute("INSERT INTO lesson_confirmations (lesson_id, session_date) VALUES (?, ?)",
                       [lesson_id, session_date])
    elif action == "contradict":
        con.execute("UPDATE lessons SET contradictions = contradictions + 1 WHERE lesson_id = ?", [lesson_id])


def set_lesson_status(con: duckdb.DuckDBPyConnection, lesson_id: int, status: str) -> None:
    con.execute("UPDATE lessons SET status = ? WHERE lesson_id = ?", [status, lesson_id])


def active_lessons(con: duckdb.DuckDBPyConnection, limit: int = 25) -> list[dict]:
    cols = [c[0] for c in con.execute("DESCRIBE lessons").fetchall()]
    rows = con.execute(
        """SELECT * FROM lessons WHERE status IN ('active','promoted')
           ORDER BY (CASE confidence WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END) DESC,
                    confirmations - contradictions DESC, created_date DESC
           LIMIT ?""",
        [limit],
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def all_lessons(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cols = [c[0] for c in con.execute("DESCRIBE lessons").fetchall()]
    rows = con.execute("SELECT * FROM lessons ORDER BY lesson_id").fetchall()
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------- Task 5: lesson out-of-sample holdout

def lesson_confidence_tier(confirmations_in_sample: int, confirmations_out_of_sample: int) -> str:
    """
    Confidence tier for ONE lesson's confirmation evidence, gating on WHEN each
    confirmation happened relative to the lesson's promoted_at (see
    lesson_holdout_stats below) -- not just how many there are in total.

    none < provisional < low < moderate < established (same names as the
    playbook-rule tiers in prompts/weekly_miner.md: none=0, provisional=1-7,
    low=8-19, moderate=20-39, established=40+).

    provisional/low can be earned on in-sample evidence alone (n_total) --
    early learning proceeds exactly as it does today. moderate/established
    require OUT-OF-SAMPLE evidence achieved on ITS OWN: reaching moderate
    needs as much out-of-sample n (>= 8) as it normally takes to earn low on
    any evidence; established needs as much out-of-sample n (>= 20) as it
    normally takes to earn moderate. This keeps the escalation the same size
    step at every tier and never lets in-sample volume alone carry a lesson
    past low, no matter how large it gets.
    """
    n_in = confirmations_in_sample or 0
    n_out = confirmations_out_of_sample or 0
    n_total = n_in + n_out
    if n_out >= 20:
        return "established"
    if n_out >= 8:
        return "moderate"
    if n_total >= 8:
        return "low"
    if n_total >= 1:
        return "provisional"
    return "none"


def lesson_holdout_stats(con: duckdb.DuckDBPyConnection, lesson_id: int,
                        promoted_at: date | None, holdout_sessions: int = 0) -> dict:
    """In-sample/out-of-sample confirmation counts for one lesson, plus its
    gated tier (lesson_confidence_tier).

    holdout_sessions (config `lesson_holdout_sessions`, default 0/disabled) is
    the TRUE withheld-window mechanism: when > 0, confirmations that fall
    within the most recent `holdout_sessions` distinct sessions are excluded
    entirely (neither in-sample nor out-of-sample) rather than merely split by
    promoted_at -- a genuine rolling holdout, for later once sessions are
    cheap. At 0 it does nothing and this function reduces to the plain
    promoted_at split.
    """
    dates = [r[0] for r in con.execute(
        "SELECT session_date FROM lesson_confirmations WHERE lesson_id = ?", [lesson_id]
    ).fetchall()]

    if holdout_sessions:
        recent = con.execute(
            "SELECT DISTINCT plan_date FROM daily_plans ORDER BY plan_date DESC LIMIT ?",
            [holdout_sessions],
        ).fetchall()
        if len(recent) == holdout_sessions:
            cutoff = min(r[0] for r in recent)  # earliest date inside the withheld window
            dates = [d for d in dates if d < cutoff]

    if promoted_at is not None:
        n_in = sum(1 for d in dates if d <= promoted_at)
        n_out = sum(1 for d in dates if d > promoted_at)
    else:
        # no promoted_at on record (shouldn't happen post-migration) -- treat
        # everything as in-sample rather than overclaiming out-of-sample rigor.
        n_in, n_out = len(dates), 0

    return {
        "confirmations_in_sample": n_in,
        "confirmations_out_of_sample": n_out,
        "tier": lesson_confidence_tier(n_in, n_out),
    }


# ------------------------------------------------- indicator layer v2: fundamentals cache

def _session_cutoff(con: duckdb.DuckDBPyConnection, n: int) -> date:
    """The oldest `fetched_date` still considered fresh: the date of the Nth
    most recent distinct plan_date. Falls back to a conservative calendar
    estimate (2 calendar days per session) when there isn't yet n sessions of
    history — a cold ledger must not treat everything as infinitely fresh."""
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT plan_date FROM daily_plans ORDER BY plan_date DESC LIMIT ?", [n]
    ).fetchall()]
    if len(dates) == n:
        return min(dates)
    return date.today() - timedelta(days=n * 2)


def cached_fundamentals(con: duckdb.DuckDBPyConnection, symbols: list[str],
                        max_age_sessions: int = 3) -> dict[str, dict]:
    """Fundamentals rows fetched within the last `max_age_sessions` trading
    sessions. Stale or never-fetched symbols are simply absent from the
    result -- callers (agent/providers.py fundamentals()) re-fetch those."""
    if not symbols:
        return {}
    cutoff = _session_cutoff(con, max_age_sessions)
    placeholders = ",".join("?" for _ in symbols)
    rows = con.execute(
        f"SELECT symbol, mkt_cap, float_shares, short_pct_float, short_asof, fetched_date "
        f"FROM fundamentals WHERE symbol IN ({placeholders})", symbols
    ).fetchall()
    out: dict[str, dict] = {}
    for sym, mkt_cap, float_shares, short_pct, short_asof, fetched in rows:
        if fetched is not None and fetched >= cutoff:
            out[sym] = {"mkt_cap": mkt_cap, "float_shares": float_shares,
                       "short_pct_float": short_pct, "short_asof": short_asof}
    return out


def save_fundamentals(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> None:
    """Upsert freshly-fetched fundamentals rows, stamped with today's date."""
    today = date.today()
    for r in rows:
        con.execute("DELETE FROM fundamentals WHERE symbol = ?", [r["symbol"]])
        con.execute(
            "INSERT INTO fundamentals (symbol, mkt_cap, float_shares, short_pct_float, "
            "short_asof, fetched_date) VALUES (?,?,?,?,?,?)",
            [r["symbol"], r.get("mkt_cap"), r.get("float_shares"), r.get("short_pct_float"),
             r.get("short_asof"), today],
        )


# --------------------------------------------------------------------- regime

def save_regime(con: duckdb.DuckDBPyConnection, r: dict) -> None:
    con.execute("DELETE FROM regime_history WHERE date = ?", [r["date"]])
    con.execute(
        """INSERT INTO regime_history
           (date, trend, risk, vix, vix_5d_chg_pct, spy_vs_20d_pct, spy_vs_50d_pct,
            sectors_above_20d, iwm_spy_5d_rel_pct, realized_vol_10d, tag, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            r["date"], r.get("trend"), r.get("risk"), r.get("vix"),
            r.get("vix_5d_chg_pct"), r.get("spy_vs_20d_pct"), r.get("spy_vs_50d_pct"),
            r.get("sectors_above_20d"), r.get("iwm_spy_5d_rel_pct"),
            r.get("realized_vol_10d"), r.get("tag"), r.get("notes"),
        ],
    )


def recent_regimes(con: duckdb.DuckDBPyConnection, n: int = 5) -> list[dict]:
    cols = [c[0] for c in con.execute("DESCRIBE regime_history").fetchall()]
    rows = con.execute("SELECT * FROM regime_history ORDER BY date DESC LIMIT ?", [n]).fetchall()
    return [dict(zip(cols, r)) for r in rows]


# ------------------------------------------------------------ analytics/memory

def recent_trades(con: duckdb.DuckDBPyConnection, n: int = 20) -> list[dict]:
    rows = con.execute(
        """SELECT p.pick_id, p.plan_date, p.ticker, p.direction, p.setup_type, p.conviction,
                  p.size_r, o.triggered, o.exit_reason, o.r_multiple, o.mfe_r, o.mae_r,
                  o.grade, p.regime_tag, o.missed_move_r
           FROM picks p JOIN outcomes o ON p.pick_id = o.pick_id
           ORDER BY p.plan_date DESC, p.pick_id DESC LIMIT ?""",
        [n],
    ).fetchall()
    keys = ["pick_id", "plan_date", "ticker", "direction", "setup_type", "conviction",
            "size_r", "triggered", "exit_reason", "r_multiple", "mfe_r", "mae_r",
            "grade", "regime_tag", "missed_move_r"]
    return [dict(zip(keys, r)) for r in rows]


STATS_DIMENSIONS: frozenset[str] = frozenset(
    {"setup_type", "regime_tag", "direction", "conviction", "in_scan_universe"}
)


def _stats_rows(con: duckdb.DuckDBPyConnection, dim_expr: str) -> list[dict]:
    """Shared aggregation body for stats_by / stats_by_outcome — `dim_expr` is
    a trusted (whitelisted by the caller) SQL expression, never caller-supplied
    text, since DuckDB has no way to bind a column/expression as a parameter.

    Includes dispersion (std_r) and a plain t-statistic (avg_r / std-error) computed
    HERE, deterministically — the LLM must never be trusted to derive significance
    from a CSV. |t| < ~1.5 means the average is statistically indistinguishable
    from zero at that sample size, regardless of how big n looks.
    """
    rows = con.execute(
        f"""SELECT {dim_expr} AS dim,
                   COUNT(*) AS n,
                   SUM(CASE WHEN o.triggered THEN 1 ELSE 0 END) AS triggered,
                   SUM(CASE WHEN o.triggered AND o.r_multiple > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(CAST(AVG(CASE WHEN o.triggered THEN o.r_multiple END) AS NUMERIC), 3) AS avg_r,
                   ROUND(CAST(SUM(CASE WHEN o.triggered THEN o.r_multiple * COALESCE(p.size_r, 1) ELSE 0 END) AS NUMERIC), 3) AS total_weighted_r,
                   STDDEV_SAMP(CASE WHEN o.triggered THEN o.r_multiple END) AS std_r,
                   ROUND(CAST(AVG(CASE WHEN o.triggered THEN o.baseline_r END) AS NUMERIC), 3) AS baseline_avg_r
            FROM picks p JOIN outcomes o ON p.pick_id = o.pick_id
            GROUP BY 1 ORDER BY n DESC"""
    ).fetchall()
    out = []
    for r in rows:
        n_trig = r[2] or 0
        avg_r = float(r[4]) if r[4] is not None else None
        std_r = float(r[6]) if r[6] is not None else None
        t_stat = None
        if avg_r is not None and std_r and std_r > 0 and n_trig >= 2:
            t_stat = round(avg_r / (std_r / (n_trig ** 0.5)), 2)
        out.append({
            "dim": r[0], "n": r[1], "triggered": n_trig, "wins": r[3] or 0,
            "win_rate": round((r[3] or 0) / n_trig, 3) if n_trig else None,
            "avg_r": avg_r,
            "total_weighted_r": float(r[5]) if r[5] is not None else 0.0,
            "std_r": round(std_r, 3) if std_r is not None else None,
            "t_stat": t_stat,
            "baseline_avg_r": float(r[7]) if r[7] is not None else None,
        })
    return out


def stats_by(con: duckdb.DuckDBPyConnection, dim: str) -> list[dict]:
    """Aggregate performance stats grouped by a picks column (setup_type, regime_tag, direction, conviction)."""
    if dim not in STATS_DIMENSIONS:
        raise ValueError(
            f"stats_by: unsupported dimension {dim!r}; expected one of {sorted(STATS_DIMENSIONS)}"
        )
    # dim is now guaranteed to be one of the whitelisted identifiers above, so
    # f-string interpolation of a column name here is safe.
    return _stats_rows(con, f"COALESCE(CAST(p.{dim} AS VARCHAR),'?')")


# Indicator-layer dimensions (specs/INDICATOR_LAYER_SPEC_v1.md §5) — these live
# on OUTCOMES (computed at grading time), not on picks, so they can't go
# through stats_by's p.{dim} lookup. rvol_bucket buckets rvol_at_1000 (the
# morning-relevant checkpoint) into <1x / 1-2x / >2x of ADV20.
OUTCOME_STATS_DIMENSIONS: frozenset[str] = frozenset({
    "rvol_bucket", "vwap_side_at_fill",
    # indicator layer v2 (specs/INDICATOR_LAYER_SPEC_v2.md §6)
    "stop_atr_bucket", "gap_atr_bucket", "ext_bucket", "trend_side_200d",
    "mcap_bucket", "short_bucket", "or_alignment",
})

_OUTCOME_DIM_EXPR = {
    "rvol_bucket": (
        "CASE WHEN o.rvol_at_1000 IS NULL THEN '?' "
        "WHEN o.rvol_at_1000 < 1 THEN '<1x' "
        "WHEN o.rvol_at_1000 < 2 THEN '1-2x' ELSE '>2x' END"
    ),
    "vwap_side_at_fill": "COALESCE(o.vwap_side_at_fill, '?')",
    # Q9: stop distance sweet spot
    "stop_atr_bucket": (
        "CASE WHEN o.stop_dist_atr IS NULL THEN '?' "
        "WHEN o.stop_dist_atr < 0.5 THEN '<0.5' "
        "WHEN o.stop_dist_atr < 1 THEN '0.5-1' "
        "WHEN o.stop_dist_atr < 2 THEN '1-2' ELSE '>2' END"
    ),
    # Q10: blue-sky (small gap/ATR) vs overhead-supply (large gap/ATR)
    "gap_atr_bucket": (
        "CASE WHEN o.gap_atr IS NULL THEN '?' "
        "WHEN o.gap_atr < 1 THEN '<1' "
        "WHEN o.gap_atr < 2 THEN '1-2' ELSE '>2' END"
    ),
    "ext_bucket": (
        "CASE WHEN o.ext_atr_at_entry IS NULL THEN '?' "
        "WHEN o.ext_atr_at_entry < -2 THEN '<-2' "
        "WHEN o.ext_atr_at_entry <= 2 THEN '-2..+2' ELSE '>+2' END"
    ),
    # Q11: trend alignment (the 200d question)
    "trend_side_200d": "COALESCE(o.trend_side_200d, '?')",
    "mcap_bucket": (
        "CASE WHEN o.mkt_cap IS NULL THEN '?' "
        "WHEN o.mkt_cap < 2e9 THEN '<2B' "
        "WHEN o.mkt_cap < 10e9 THEN '2-10B' ELSE '>10B' END"
    ),
    # Q12: shortedness
    "short_bucket": (
        "CASE WHEN o.short_pct_float IS NULL THEN '?' "
        "WHEN o.short_pct_float < 5 THEN '<5%' "
        "WHEN o.short_pct_float < 15 THEN '5-15%' ELSE '>15%' END"
    ),
    "or_alignment": (
        "CASE WHEN o.or_break_dir IS NULL OR o.fill_vs_or IS NULL THEN '?' "
        "WHEN (p.direction='long' AND o.or_break_dir='up') "
        "  OR (p.direction='short' AND o.or_break_dir='down') THEN 'with' "
        "WHEN (p.direction='long' AND o.or_break_dir='down') "
        "  OR (p.direction='short' AND o.or_break_dir='up') THEN 'against' "
        "ELSE '?' END"
    ),
}


def stats_by_outcome(con: duckdb.DuckDBPyConnection, dim: str) -> list[dict]:
    """Same shape as stats_by, grouped by an indicator dimension computed onto
    the outcomes table by agent/grading.py (rvol_bucket, vwap_side_at_fill)."""
    if dim not in OUTCOME_STATS_DIMENSIONS:
        raise ValueError(
            f"stats_by_outcome: unsupported dimension {dim!r}; "
            f"expected one of {sorted(OUTCOME_STATS_DIMENSIONS)}"
        )
    return _stats_rows(con, _OUTCOME_DIM_EXPR[dim])


def equity_curve(con: duckdb.DuckDBPyConnection, account_size: float, dollars_per_r: float) -> list[dict]:
    rows = con.execute(
        """SELECT o.eval_date, SUM(COALESCE(o.r_multiple,0) * COALESCE(p.size_r,1)) AS day_r
           FROM outcomes o JOIN picks p ON p.pick_id = o.pick_id
           WHERE o.triggered
           GROUP BY o.eval_date ORDER BY o.eval_date"""
    ).fetchall()
    curve, equity, cum_r = [], float(account_size), 0.0
    for d, day_r in rows:
        day_r = float(day_r or 0)
        cum_r += day_r
        equity += day_r * dollars_per_r
        curve.append({"date": str(d), "day_r": round(day_r, 3),
                      "cum_r": round(cum_r, 3), "equity": round(equity, 2)})
    return curve


def current_streak(con: duckdb.DuckDBPyConnection) -> dict:
    """Consecutive win/loss streak over triggered trades, most recent first."""
    rows = con.execute(
        """SELECT o.r_multiple FROM outcomes o JOIN picks p ON p.pick_id = o.pick_id
           WHERE o.triggered AND o.r_multiple IS NOT NULL
           ORDER BY p.plan_date DESC, p.pick_id DESC LIMIT 30"""
    ).fetchall()
    if not rows:
        return {"kind": "none", "length": 0}
    sign = 1 if rows[0][0] > 0 else -1
    length = 0
    for (r,) in rows:
        if (r > 0 and sign == 1) or (r <= 0 and sign == -1):
            length += 1
        else:
            break
    return {"kind": "winning" if sign == 1 else "losing", "length": length}


def overall_stats(con: duckdb.DuckDBPyConnection) -> dict:
    row = con.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN o.triggered THEN 1 ELSE 0 END),
                  SUM(CASE WHEN o.triggered AND o.r_multiple > 0 THEN 1 ELSE 0 END),
                  AVG(CASE WHEN o.triggered THEN o.r_multiple END),
                  SUM(CASE WHEN o.triggered THEN o.r_multiple * COALESCE(p.size_r,1) ELSE 0 END)
           FROM picks p JOIN outcomes o ON p.pick_id = o.pick_id"""
    ).fetchone()
    n, trig, wins, avg_r, total_r = row
    return {
        "picks_graded": n or 0,
        "triggered": trig or 0,
        "wins": wins or 0,
        "win_rate": round((wins or 0) / trig, 3) if trig else None,
        "avg_r": round(avg_r, 3) if avg_r is not None else None,
        "total_weighted_r": round(total_r or 0.0, 3),
    }


def trailing_weighted_r(con: duckdb.DuckDBPyConnection, n: int = 10) -> dict:
    """Sum of size-weighted R over the last n triggered trades — the drawdown
    circuit breaker's input. Catches slow bleeds that never form a losing streak."""
    rows = con.execute(
        """SELECT o.r_multiple * COALESCE(p.size_r, 1)
           FROM outcomes o JOIN picks p ON p.pick_id = o.pick_id
           WHERE o.triggered AND o.r_multiple IS NOT NULL
           ORDER BY p.plan_date DESC, p.pick_id DESC LIMIT ?""",
        [n],
    ).fetchall()
    vals = [float(r[0]) for r in rows]
    return {"n": len(vals), "sum_weighted_r": round(sum(vals), 3)}


def management_quality(con: duckdb.DuckDBPyConnection) -> dict:
    """Deterministic counterfactual aggregates: is the stop/target machinery adding
    value over just holding, are stops inside the noise, and does the desk beat
    the naive same-name open->close baseline (the null model)?"""
    row = con.execute(
        """SELECT
             SUM(CASE WHEN o.triggered THEN 1 ELSE 0 END),
             AVG(CASE WHEN o.triggered THEN o.r_multiple END),
             AVG(CASE WHEN o.triggered THEN o.baseline_r END),
             AVG(CASE WHEN o.triggered AND o.eod_r IS NOT NULL
                      THEN o.r_multiple - o.eod_r END),
             SUM(CASE WHEN o.exit_reason = 'stop' THEN 1 ELSE 0 END),
             SUM(CASE WHEN o.exit_reason = 'stop' AND o.target_hit_after_stop
                      THEN 1 ELSE 0 END),
             AVG(CASE WHEN o.exit_reason = 'stop' THEN o.eod_r END),
             AVG(CASE WHEN o.triggered = FALSE AND o.baseline_r IS NOT NULL
                      THEN o.baseline_r END),
             SUM(CASE WHEN o.triggered = FALSE AND o.baseline_r IS NOT NULL
                      THEN 1 ELSE 0 END)
           FROM outcomes o"""
    ).fetchone()
    (n_trig, avg_r, avg_base, mgmt_edge, n_stop, n_stop_then_target,
     avg_eod_stopped, avg_base_untrig, n_untrig) = row
    rnd = lambda v: round(float(v), 3) if v is not None else None  # noqa: E731
    return {
        "triggered": n_trig or 0,
        "avg_r": rnd(avg_r),
        "avg_baseline_r": rnd(avg_base),
        "edge_vs_baseline_r": rnd((avg_r - avg_base) if avg_r is not None and avg_base is not None else None),
        "management_edge_r": rnd(mgmt_edge),          # actual R minus hold-to-close R
        "stopped": n_stop or 0,
        "stopped_then_target": n_stop_then_target or 0,
        "avg_eod_r_on_stopped": rnd(avg_eod_stopped), # what stopped trades did by the close
        "untriggered_with_baseline": n_untrig or 0,
        "avg_baseline_r_untriggered": rnd(avg_base_untrig),  # what fussy entries walked away from
    }


def sizing_counterfactual(con: duckdb.DuckDBPyConnection) -> dict:
    """Did size allocation add value? Compares actual size-weighted total R to the
    same trades at a uniform (average) size. Positive edge = sizing skill is real."""
    row = con.execute(
        """SELECT SUM(o.r_multiple * COALESCE(p.size_r, 1)),
                  SUM(o.r_multiple),
                  AVG(COALESCE(p.size_r, 1)),
                  COUNT(*)
           FROM outcomes o JOIN picks p ON p.pick_id = o.pick_id
           WHERE o.triggered AND o.r_multiple IS NOT NULL"""
    ).fetchone()
    actual, sum_r, avg_size, n = row
    if not n:
        return {"n": 0, "actual_weighted_r": 0.0, "flat_size_weighted_r": 0.0, "sizing_edge_r": 0.0}
    flat = float(sum_r) * float(avg_size)
    return {
        "n": n,
        "actual_weighted_r": round(float(actual), 3),
        "flat_size_weighted_r": round(flat, 3),
        "sizing_edge_r": round(float(actual) - flat, 3),
    }


def all_trades_csv(con: duckdb.DuckDBPyConnection) -> str:
    """Compact CSV of the entire graded ledger for the weekly miner."""
    rows = con.execute(
        """SELECT p.plan_date, p.ticker, p.direction, p.setup_type, p.conviction, p.size_r,
                  p.entry_price, p.stop_price, p.target_price, p.regime_tag,
                  o.triggered, o.exit_reason, o.r_multiple, o.mfe_r, o.mae_r,
                  o.missed_move_r, o.baseline_r, o.eod_r, o.target_hit_after_stop, o.grade,
                  COALESCE(o.sim_version, 1)
           FROM picks p JOIN outcomes o ON p.pick_id = o.pick_id
           ORDER BY p.plan_date, p.pick_id"""
    ).fetchall()
    header = ("date,ticker,dir,setup,conv,size_r,entry,stop,target,regime,"
              "triggered,exit,r,mfe_r,mae_r,missed_r,baseline_r,eod_r,target_after_stop,grade,"
              "sim_version")
    lines = [header]
    for r in rows:
        lines.append(",".join("" if v is None else str(v) for v in r))
    return "\n".join(lines)


def sim_version_breakdown(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Counts of graded outcomes by sim_version, with the plan_date span each
    version covers. Exposes the Task 3/4 simulation-semantics cutover to the
    weekly miner so it doesn't read exit-reason mix as one homogeneous pile
    across a boundary where the rules actually changed."""
    rows = con.execute(
        """SELECT COALESCE(o.sim_version, 1) AS v, COUNT(*),
                  MIN(p.plan_date), MAX(p.plan_date)
           FROM outcomes o JOIN picks p ON p.pick_id = o.pick_id
           GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    return [{"sim_version": r[0], "n": r[1], "first_date": str(r[2]), "last_date": str(r[3])}
            for r in rows]


def graded_picks_since(con: duckdb.DuckDBPyConnection, from_date: date) -> list[dict]:
    """Picks with an existing outcome row, plan_date >= from_date -- the
    candidate set for `run.py regrade`."""
    cols = [c[0] for c in con.execute("DESCRIBE picks").fetchall()]
    rows = con.execute(
        """SELECT p.* FROM picks p JOIN outcomes o ON p.pick_id = o.pick_id
           WHERE p.plan_date >= ? ORDER BY p.plan_date, p.pick_id""",
        [from_date],
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def log_run(con: duckdb.DuckDBPyConnection, loop: str, run_date: date, started: datetime,
            status: str, model: str = "", usage: dict | None = None, error: str = "") -> None:
    u = usage or {}
    con.execute(
        """INSERT INTO runs (loop, run_date, started_at, finished_at, status, model,
                             input_tokens, output_tokens, web_searches, cost_usd, error)
           VALUES (?,?,?,current_timestamp,?,?,?,?,?,?,?)""",
        [loop, run_date, started, status, model,
         u.get("input_tokens", 0), u.get("output_tokens", 0),
         u.get("web_searches", 0), u.get("cost_usd", 0.0), error[:2000]],
    )


def recent_runs(con: duckdb.DuckDBPyConnection, n: int = 10) -> list[dict]:
    cols = [c[0] for c in con.execute("DESCRIBE runs").fetchall()]
    rows = con.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", [n]).fetchall()
    return [dict(zip(cols, r)) for r in rows]


# ------------------------------------------------------------------ v3: shadow ledger

def save_rejections(con: duckdb.DuckDBPyConnection, plan_date: date, rejections: list[dict]) -> int:
    """Persist killed candidates (with their full plan levels) for counterfactual grading."""
    con.execute("DELETE FROM rejections WHERE plan_date = ?", [plan_date])
    for r in rejections:
        con.execute(
            """INSERT INTO rejections
               (plan_date, ticker, direction, setup_type, reason, entry_type,
                entry_price, stop_price, target_price, conviction, thesis)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                plan_date, r["ticker"].upper(), r.get("direction"), r.get("setup_type"),
                r.get("reason", ""), r.get("entry_type", "open"), r.get("entry_price"),
                r.get("stop_price"), r.get("target_price"), r.get("conviction"),
                r.get("thesis"),
            ],
        )
    return len(rejections)


def ungraded_rejections(con: duckdb.DuckDBPyConnection, d: date) -> list[dict]:
    cols = [c[0] for c in con.execute("DESCRIBE rejections").fetchall()]
    rows = con.execute(
        "SELECT * FROM rejections WHERE plan_date = ? AND eod_outcome IS NULL ORDER BY rejection_id",
        [d],
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def rejections_for_date(con: duckdb.DuckDBPyConnection, d: date) -> list[dict]:
    cols = [c[0] for c in con.execute("DESCRIBE rejections").fetchall()]
    rows = con.execute(
        "SELECT * FROM rejections WHERE plan_date = ? ORDER BY rejection_id", [d]
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def save_rejection_outcome(con: duckdb.DuckDBPyConnection, rejection_id: int,
                           eod_outcome: str, forgone_r: float | None,
                           sim_version: int | None = None) -> None:
    if sim_version is None:
        from .grading import CURRENT_SIM_VERSION
        sim_version = CURRENT_SIM_VERSION
    con.execute(
        "UPDATE rejections SET eod_outcome = ?, forgone_r = ?, graded_at = current_timestamp, "
        "sim_version = ? WHERE rejection_id = ?",
        [eod_outcome, forgone_r, sim_version, rejection_id],
    )


def shadow_summary(con: duckdb.DuckDBPyConnection, trailing_sessions: int = 20) -> dict:
    """Shadow-ledger aggregates over the trailing N graded sessions."""
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT plan_date FROM rejections WHERE eod_outcome IS NOT NULL "
        "ORDER BY plan_date DESC LIMIT ?", [trailing_sessions]).fetchall()]
    if not dates:
        return {"sessions": 0, "total": 0, "would_have_won": 0, "would_have_lost": 0,
                "scratch": 0, "unmeasurable": 0, "net_forgone_r": 0.0, "by_reason": [],
                "captured_r_window": 0.0}
    lo = min(dates)
    row = con.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN eod_outcome='would_have_won' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN eod_outcome='would_have_lost' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN eod_outcome='scratch' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN eod_outcome='unmeasurable' THEN 1 ELSE 0 END),
                  SUM(COALESCE(forgone_r, 0))
           FROM rejections WHERE eod_outcome IS NOT NULL AND plan_date >= ?""", [lo]).fetchone()
    by_reason = con.execute(
        """SELECT reason, COUNT(*), ROUND(CAST(SUM(COALESCE(forgone_r,0)) AS NUMERIC), 2)
           FROM rejections WHERE eod_outcome IS NOT NULL AND plan_date >= ?
           GROUP BY reason ORDER BY SUM(COALESCE(forgone_r,0)) DESC""", [lo]).fetchall()
    captured = con.execute(
        """SELECT COALESCE(SUM(o.r_multiple * COALESCE(p.size_r,1)), 0)
           FROM outcomes o JOIN picks p ON p.pick_id = o.pick_id
           WHERE o.triggered AND o.eval_date >= ?""", [lo]).fetchone()[0]
    return {
        "sessions": len(dates), "total": row[0] or 0,
        "would_have_won": row[1] or 0, "would_have_lost": row[2] or 0,
        "scratch": row[3] or 0, "unmeasurable": row[4] or 0,
        "net_forgone_r": round(row[5] or 0.0, 2),
        "by_reason": [{"reason": r[0], "n": r[1], "net_forgone_r": float(r[2]) if r[2] is not None else 0.0} for r in by_reason],
        "captured_r_window": round(float(captured or 0.0), 2),
    }


# ------------------------------------------------------------------ v3: calibration

def add_calibration_row(con: duckdb.DuckDBPyConnection, session_date: date, ticker: str,
                        conviction: int | None, realized_r: float | None) -> None:
    con.execute("DELETE FROM calibration WHERE session_date = ? AND ticker = ?",
                [session_date, ticker])
    con.execute(
        "INSERT INTO calibration (session_date, ticker, stated_conviction, realized_r, win) "
        "VALUES (?,?,?,?,?)",
        [session_date, ticker, conviction, realized_r,
         (realized_r > 0) if realized_r is not None else None],
    )


def calibration_aggregate(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """SELECT stated_conviction, COUNT(*),
                  SUM(CASE WHEN win THEN 1 ELSE 0 END),
                  ROUND(CAST(AVG(realized_r) AS NUMERIC), 3)
           FROM calibration WHERE realized_r IS NOT NULL
           GROUP BY stated_conviction ORDER BY stated_conviction DESC"""
    ).fetchall()
    return [{"conviction": r[0], "n": r[1],
             "win_rate": round((r[2] or 0) / r[1], 3) if r[1] else None,
             "avg_r": float(r[3]) if r[3] is not None else None} for r in rows]


# ------------------------------------------------- v3: rule confirmations / decay

def add_rule_confirmation(con: duckdb.DuckDBPyConnection, rule_id: str,
                          session_date: date, pick_id: int) -> None:
    con.execute(
        "INSERT INTO rule_confirmations (rule_id, session_date, pick_id) VALUES (?,?,?)",
        [rule_id, session_date, pick_id],
    )


def rule_staleness(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Per rule: latest confirmation date + sessions of desk operation since."""
    rows = con.execute(
        """SELECT rc.rule_id, MAX(rc.session_date) AS last_confirmed,
                  (SELECT COUNT(DISTINCT dp.plan_date) FROM daily_plans dp
                   WHERE dp.plan_date > MAX(rc.session_date)) AS sessions_since
           FROM rule_confirmations rc GROUP BY rc.rule_id ORDER BY rc.rule_id"""
    ).fetchall()
    return [{"rule_id": r[0], "last_confirmed": str(r[1]), "sessions_since": r[2] or 0}
            for r in rows]


# --------------------------------------------------- v3: open question progress

def add_question_progress(con: duckdb.DuckDBPyConnection, question_id: int,
                          session_date: date, increment: int, note: str) -> None:
    con.execute(
        "INSERT INTO open_question_progress (question_id, session_date, increment, note) "
        "VALUES (?,?,?,?)",
        [question_id, session_date, increment, note[:500]],
    )


def question_progress_totals(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """SELECT question_id, SUM(increment), COUNT(*),
                  MAX(session_date)
           FROM open_question_progress GROUP BY question_id ORDER BY question_id"""
    ).fetchall()
    out = []
    for r in rows:
        notes = con.execute(
            "SELECT note FROM open_question_progress WHERE question_id = ? "
            "ORDER BY session_date DESC LIMIT 5", [r[0]]).fetchall()
        out.append({"question_id": r[0], "total": r[1] or 0, "events": r[2],
                    "last_event": str(r[3]), "recent_notes": [n[0] for n in notes]})
    return out
