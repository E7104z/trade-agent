"""
Schema v4 — research integrity, population baseline, friction stress, paired arms.

INTEGRATION: exactly one line in agent/ledger.py.

    from . import migrations_v4
    _MIGRATIONS = _MIGRATIONS + migrations_v4.STATEMENTS

`ledger.connect()` already executes every entry of `_MIGRATIONS` in BOTH the
DuckDB and the Postgres branch, and the Postgres branch regex-rewrites the token
DOUBLE to DOUBLE PRECISION. So:

  - Every statement here must be idempotent (CREATE TABLE IF NOT EXISTS /
    ADD COLUMN IF NOT EXISTS). Running twice is normal, not an error.
  - Write the bare token DOUBLE, never DOUBLE PRECISION — the Postgres branch
    performs that substitution itself and would otherwise emit the invalid
    "DOUBLE PRECISION PRECISION".
  - No sequences and no DEFAULT nextval(): these tables use natural or
    composite keys so the schema stays identical across both engines.

NOTHING HERE CHANGES EXISTING BEHAVIOR. Every added column is nullable with no
default, so existing rows read exactly as they did before, and every added table
is empty until a new code path writes to it.
"""
from __future__ import annotations

__all__ = ["STATEMENTS", "SCHEMA_VERSION", "research_schema"]

SCHEMA_VERSION = 4


def research_schema() -> list[str]:
    """The v4 statements plus every LATER additive column on the v4 tables.

    For test fixtures that build only the research layer on an in-memory
    database: `STATEMENTS` alone describes the tables as v4 created them, and
    code written against a later migration (research_looks.test, ...) would
    fail on it. Production never calls this -- ledger._MIGRATIONS already
    applies every migration in order. Defined as a function so STATEMENTS,
    and therefore the schema fingerprint, is untouched.
    """
    from . import migrations_v17, migrations_v20, migrations_v32
    return (list(STATEMENTS) + list(migrations_v17.RESEARCH_STATEMENTS)
            + list(migrations_v20.RESEARCH_STATEMENTS)
            + list(migrations_v32.RESEARCH_STATEMENTS))

STATEMENTS: list[str] = [

    # ---------------------------------------------------------------- looks
    # One row per comparison shown to the weekly miner. This is what turns
    # "the miner searched widely" into a number the FDR correction can use.
    """CREATE TABLE IF NOT EXISTS research_looks (
        session_date   DATE NOT NULL,
        dimension      VARCHAR NOT NULL,
        level          VARCHAR NOT NULL,
        n              INTEGER,
        mean_r         DOUBLE,
        std_r          DOUBLE,
        t_stat         DOUBLE,
        p_value        DOUBLE,
        q_value        DOUBLE,
        significant    BOOLEAN,
        mde            DOUBLE,
        n_dimensions   INTEGER,
        note           VARCHAR
    )""",

    # ---------------------------------------------------------------- rules
    # The quarantine state machine. forward_* are running sums so rule_states()
    # is one cheap query rather than an aggregate over observations.
    """CREATE TABLE IF NOT EXISTS research_rules (
        rule_id            VARCHAR PRIMARY KEY,
        statement          VARCHAR NOT NULL,
        dimensions         VARCHAR,
        n_dimensions       INTEGER,
        state              VARCHAR,
        source             VARCHAR,
        discovered_at      DATE,
        discovery_n        INTEGER,
        discovery_effect   DOUBLE,
        discovery_q        DOUBLE,
        quarantined_at     DATE,
        validated_at       DATE,
        retired_at         DATE,
        forward_n_required INTEGER,
        forward_n          INTEGER,
        forward_sum_r      DOUBLE,
        forward_sumsq_r    DOUBLE,
        note               VARCHAR
    )""",

    # Per-event forward observations. Kept alongside the running sums so a
    # future audit can recompute them independently — the sums are a cache, the
    # events are the record.
    """CREATE TABLE IF NOT EXISTS research_observations (
        rule_id       VARCHAR NOT NULL,
        session_date  DATE NOT NULL,
        r_multiple    DOUBLE
    )""",

    # ------------------------------------------------------------ population
    # The unconditional null: the full premarket gap-scan population, snapshotted
    # BEFORE any selection happens. baseline_r on the outcomes table is
    # conditioned on names the analyst already chose, so it answers "did our
    # entry/exit machinery beat holding these?" — not "did our name selection
    # beat the opportunity set?" These two tables answer the second question.
    """CREATE TABLE IF NOT EXISTS population_universe (
        plan_date        DATE NOT NULL,
        symbol           VARCHAR NOT NULL,
        last             DOUBLE,
        prev_close       DOUBLE,
        gap_pct          DOUBLE,
        atr14            DOUBLE,
        atr_pct          DOUBLE,
        adv20            DOUBLE,
        in_scan_universe BOOLEAN,
        was_picked       BOOLEAN,
        captured_at      TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS population_outcomes (
        plan_date     DATE NOT NULL,
        symbol        VARCHAR NOT NULL,
        arm           VARCHAR NOT NULL,   -- momentum | fade
        direction     VARCHAR,
        entry_price   DOUBLE,
        stop_price    DOUBLE,
        exit_price    DOUBLE,
        exit_reason   VARCHAR,            -- stop | eod | no_data
        r_multiple    DOUBLE,
        day_open      DOUBLE,
        day_high      DOUBLE,
        day_low       DOUBLE,
        day_close     DOUBLE,
        sim_version   INTEGER,
        graded_at     TIMESTAMP
    )""",

    # ----------------------------------------------------------- paired arms
    # Same-day concurrent playbook / context-mode arms. Only arm A is ever
    # traded and written to picks; arm B lives here and is graded by the same
    # simulator, so a playbook-version comparison is never confounded by the
    # market having moved between weeks.
    """CREATE TABLE IF NOT EXISTS paired_arms (
        plan_date         DATE NOT NULL,
        arm               VARCHAR NOT NULL,   -- A (production) | B (challenger)
        playbook_version  VARCHAR,
        context_mode      VARCHAR,
        prompt_version    VARCHAR,
        model             VARCHAR,
        no_trade          BOOLEAN,
        plan_json         VARCHAR,
        analyst_json      VARCHAR,
        regime_tag        VARCHAR,
        cost_usd          DOUBLE,
        created_at        TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS paired_arm_picks (
        plan_date     DATE NOT NULL,
        arm           VARCHAR NOT NULL,
        ticker        VARCHAR NOT NULL,
        direction     VARCHAR,
        setup_type    VARCHAR,
        entry_type    VARCHAR,
        entry_price   DOUBLE,
        stop_price    DOUBLE,
        target_price  DOUBLE,
        size_r        DOUBLE,
        conviction    INTEGER,
        time_stop     VARCHAR,
        thesis        VARCHAR,
        triggered     BOOLEAN,
        entry_fill    DOUBLE,
        exit_price    DOUBLE,
        exit_reason   VARCHAR,
        r_multiple    DOUBLE,
        sim_version   INTEGER,
        graded_at     TIMESTAMP
    )""",

    # ------------------------------------------------- friction stress ladder
    # Parallel R columns. The existing r_multiple keeps its exact current
    # semantics under the configured flat slippage — these are ADDITIONAL
    # re-simulations at 2x and 3x friction. A rule whose edge dies at 2x was a
    # fee estimate, not an edge, and the miner is shown all three.
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS r_multiple_2x DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS r_multiple_3x DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS slippage_bps_used DOUBLE",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS friction_model VARCHAR",
    "ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS participation_pct DOUBLE",

    # ------------------------------------------------ context-channel ablation
    # Which memory context the morning call actually saw. Without this the
    # last-20-trades channel cannot be ablated or even attributed after the
    # fact — it is the most-read, least-gated input in the system.
    "ALTER TABLE daily_plans ADD COLUMN IF NOT EXISTS context_mode VARCHAR",
    "ALTER TABLE daily_plans ADD COLUMN IF NOT EXISTS paired_run BOOLEAN",

    # Ties a pick to the quarantined rule it was an expression of, so the
    # evening loop knows which rules to credit with a forward observation.
    "ALTER TABLE picks ADD COLUMN IF NOT EXISTS rule_id VARCHAR",
]
