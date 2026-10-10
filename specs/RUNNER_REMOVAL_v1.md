# RUNNER_REMOVAL_v1 -- the intraday runner lane is gone

Owner directive, 2026-10-10: "I don't want the runner strategy anymore. We need to get rid of it entirely."

## Removed

* **Modules:** `runner_scan`, `runner_selection`, `runner_universe`, `runner_default_universe`,
  `runner_indicators`, `runner_entry`, `runner_exits`, `runner_research`, `runner_regate`,
  `runner_view`, `loops/runner`, and the `runner_selector` prompt, mock and persona role.
* **Live path:** the `/cron/runner` and `/cron/runner-late` crons (vercel.json), the runner tick inside
  `/cron/intraday-check`, the `runner` loop and its dashboard button, `/api/runner/*`, the runner
  dashboard page and lane card, the runner tables in `/api/table`, the `runner` config block.
* **Learning loop:** runner candidate grading and exit replays in the evening, the runner family in the
  weekly FDR batch (1-D cells, setup/selection edge, drift, exit policies, construction arms), the
  runner pairs in the 2-D spec (`PAIR_SPEC_VERSION` 3), the `runner` bucket lane, the Runner Lane section
  of the playbook (a leftover one in a miner draft is dropped), the runner health stage and self-check,
  the runner slots in the trader's log, runner counts in the persona monitor.
* **Grading:** `entry_after_map`, `RUNNER_MIN_REWARD_R` and `simulate_pick(min_reward_r=...)`. Sim v6 was
  a runner-only rule; sim_version numbering is unchanged.
* **CLI:** `runner`, `runner-status`, `runner-exits`, `runner-evidence`, `regrade-runner`, `regate-runner`.
* **Prompts:** evening_grader v10, weekly_miner v18, traders_log v10. The persona sheet stays at version 1
  (it only lost an unused role), so the persona flag keeps working.

## Kept on purpose

* **Production data.** No table is dropped and no row is deleted: `runner_*` tables, `picks` rows with
  `setup_type = 'dto_runner'`, `live_orders`, research observations. The migrations are unchanged.
  `ledger.RUNNER_SETUP` and the lane filters remain so the legacy rows never enter a morning statistic.
* **`runner_universe_cache`** now serves the gap field's instrument classes (`ledger.get_universe_cache`).
* **Dashboard equity curve** is the morning desk's (`lane="morning"`); legacy runner rows are left out.

## One production effect

`research.retire_removed_lane_rules` runs at the start of each forward-observation pass and retires every
live rule whose lane is `runner` or `runner_exit`. Such a rule can gain no further forward evidence. Its
row, observations and history are kept. Idempotent.

## Goals (STANDING_DIRECTIVE)

Goal 3 (seek data) loses the runner corpus (about ten sessions of scan v3 evidence). The directive's
integrity rules are untouched: n<8 suppression, FDR, the forward-only guard, the 2-D cap, writer isolation.
