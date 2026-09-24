# INTEGRITY REPAIR SPEC v1 — stop the FDR batch from manufacturing edge

**Status:** implemented 2026-09-24 (`adc2c7f`), live in production; production acceptance pending. Proposed 2026-09-23 ([SELF_ASSESSMENT_2026-09-23.md](SELF_ASSESSMENT_2026-09-23.md) P1, P2, P8)
**Goals served:** 5 (primary), 1, 4
**Deadline:** ship before the weekly cron on **2026-09-26 at 14:00 UTC**, the first run that will see runner rows from two scan_versions and two sim_versions.
**Scope:** `agent/stats.py`, `agent/runner_research.py`, `agent/integrity.py`, `agent/memory.py`, `api/index.py`, `run.py`, the next free migration (currently v17), tests.
**Builds on:** RESEARCH_INTEGRITY_SPEC_v4. That spec's design rules stand: deterministic code decides what counts as a finding, and the measurement apparatus must never break a trading loop.

---

## 0. The problem

The integrity layer is the thing that stops the desk from mistaking noise for edge. Two defects inside it currently produce exactly that failure, and a third keeps anyone from seeing it in production.

### Defect A — the FDR batch overwrites the two-sample test

`stats.evaluate_search` recomputes every comparison with `one_sample_t(mean_r, std_r, n)` (`stats.py:368-371`). `runner_research.selection_edge` builds a two-sample comparison:
- `mean_r` = delta;
- `std_r` = the **selected arm's** std only;
- `n` = min(n_selected, n_unselected);
- its Welch t, df and p attached (`runner_research.py:225`, `:246-250`).

The batch discards the Welch result and tests the delta as if it were a one-sample mean with the selected arm's variance.

Reproduced (12 sessions; each has 3 selected ~N(0.3, 0.6) and 20 unselected ~N(0, 2.5)):

| | p | q | significant |
|---|---|---|---|
| Welch, as supplied | 0.0955 | — | — ("no detectable selection edge") |
| After `evaluate_search` | 0.0026 | 0.0026 | **True** |

Consequences:
- The miner is shown both: the Welch verdict in prose, and a starred row in the dimension table (`runner_research.py:281`, `:294-299`).
- `research_looks` records a survivor, which inflates `cumulative_survivors`.
- This happens on the one comparison whose purpose is to say whether the selector adds anything.

### Defect B — the runner corpus pools incompatible populations

Both production callers use `runner_research.candidate_rows(con)` with `scan_version=None` (`integrity.py:210`, `:264`). The filter exists (`runner_research.py:146`), but nothing passes it. There is no `sim_version` filter at all.

Production crossed both boundaries in the last 24 hours:
- **scan_version 2** went live 2026-09-23 22:44 UTC (`470a8d3`). It adds three row-removing gates (`runner_scan.py:45-55`): `require_rvol`, `LANE_EXCLUDED_TICKERS`, and a session stand-down on feed downgrade.
- **sim_version 4** went live 2026-09-24 00:02 UTC (`85a7993`). Entries are now no earlier than the scan tick or the selection decision. On 09-23 the walked-back entries had moved the field mean R from −0.52 to −0.41.

From Saturday, setup edge, selection edge, every runner dimension and the random-selection ablation will each be a mixture of two populations and two sets of fill rules.

### Defect B2 — the morning-lane dimensions pool the runner lane

`integrity.build_search` calls `ledger.stats_by(con, dim)` and `ledger.stats_by_outcome(con, dim)` with no lane (`integrity.py:201-202`). `_stats_rows` therefore applies `lane_clause(None)`, which adds no filter (`ledger.py:1038-1046`, `:1092`). `build_weekly_memory` does the same for the `by_*` tables the miner reads (`memory.py:283-293`).

So every one of the 14 "morning" cells (direction, conviction, regime, RVOL, stop/ATR …) mixes two things:
- morning picks, at the $500/R morning sizing;
- runner picks (`setup_type='dto_runner'`, up to 10 a day at $100/R, different entry semantics).

Selected runner trades are then counted a second time in the runner family. The miner prompt says the lanes must "never be pooled into one claim" (`weekly.py:938-941`), and the numbers it receives pool them.

### Defect C — the integrity state can't be read in production

`_QUERYABLE_TABLES` (`api/index.py:537-559`) omits:
- `research_rules`
- `research_observations`
- `research_looks`
- `selection_edge_history`
- `population_universe`
- `population_outcomes`
- `paired_arm_picks`

So nobody can verify, from production, whether Defect A has already written a false survivor, or whether any rule is registered. This also blocks the Phase 0 of S2, S5 and S7.

---

## Phase 0 — production verification (hard blocker; a self-check, not a sign-off)

Run these before writing code. If a result contradicts the premise above, stop and amend this spec with what you found.

Data access means an authenticated session on `/api/table/...`, or `DATABASE_URL` set locally (the `scripts/db_preflight.py` pattern). If neither is available, record Phase 0 as blocked and stop. Never search the machine for credentials.

1. **Version mix.** Pull `/api/table/runner_candidates?since=2026-09-10&limit=1000` and `/api/table/runner_candidate_outcomes?since=2026-09-10&limit=1000`, and count rows by `scan_version` × `sim_version`.
   - Expected: only (1, 3) before 2026-09-24, then (2, 4) from 2026-09-24.
   - Also note any `sim_version` < 3.
2. **Weekly schedule.** Pull `/api/table/runs?limit=40`. Record the last `weekly` run, its status and its date, and confirm the next weekly run is still pending.
3. **Detection diff.** Run `git diff 1a8a618 470a8d3 -- agent/runner_scan.py agent/runner_indicators.py`. Confirm the v1 → v2 change is **only** the three row-removing gates listed above. Phase 4's re-gating is valid only if nothing else in detection changed.
4. **Stored gate inputs.** Confirm `runner_candidates` stores what the v2 gates need: `rvol` non-null implies `prev_volume` was present (`runner_scan.py:438`). Confirm `runner_sessions.gates_unavailable` carries `feed_downgraded`.
5. **Split repair.** Confirm `scripts/backfill_change_pct.py` has been run against production. Rows before `1a8a618` may otherwise carry reverse-split artifacts.
6. **Lane mix (B2).** From `/api/table/picks?since=2026-08-19&limit=1000` joined to `/api/table/outcomes?since=2026-08-19&limit=1000`, count triggered outcomes by lane (`setup_type = 'dto_runner'` or not). This measures how much of every "morning" cell is actually runner evidence today.

---

## Phase 1 — make the integrity tables readable (P8)

Add these to `_QUERYABLE_TABLES`, with their date columns (verified against the schema):

| table | date column |
|---|---|
| `research_rules` | `discovered_at` |
| `research_observations` | `session_date` |
| `research_looks` | `session_date` |
| `selection_edge_history` | `week_ending` |
| `population_universe` | `plan_date` |
| `population_outcomes` | `plan_date` |
| `paired_arm_picks` | `plan_date` |

Constraints:
- Whitelist only. The endpoint stays SELECT-only.
- Table and column names are trusted constants, never request input.

Test: extend `tests/test_live_route.py` or add `tests/test_table_whitelist.py`. Each new table answers `?limit=1`, and an unknown table still returns 404.

**Ship Phase 1 alone first**, then re-run Phase 0 against `research_looks` to find out whether a false `runner_selection_edge` survivor is already recorded. If one is, record it in the Phase 2 commit message. It is historical evidence of the defect, and it must not be deleted.

## Phase 2 — comparisons keep the test they were built with (Defect A)

1. Add `test: str = "one_sample"` to `stats.Comparison` and to `to_dict()`.
2. `evaluate_search` runs `one_sample_t` only when `test == "one_sample"`. For any other test it keeps the supplied `t_stat`, `df` and `p_value`. A non-one-sample comparison with `p_value is None` is untestable: it passes through BH as `None`, exactly like an empty cell.
3. MDE:
   - `one_sample`: unchanged.
   - `welch`: compute from the two arms' standard errors. Carry the arms on the comparison as `n_a, std_a, n_b, std_b`, or set MDE to `None` with the note `"two-sample MDE not computed"`. Never compute it from one arm.
4. `selection_edge` emits `test="welch"` with its own t/df/p.
   - Set `std_r=None`. A single-arm std must not be stored as if it described the comparison.
   - Keep `n=min(n_selected, n_unselected)`, so the n ≥ 8 floor still applies to the smaller arm.
5. `research_looks` gains a `test` column (next migration, additive, default `'one_sample'`).
6. Audit every other producer of `Comparison` (`annotate_stats_rows`, `candidate_comparisons`, `setup_edge`) and confirm each is genuinely one-sample.

Tests (`tests/test_stats.py`, `tests/test_runner_research.py`):
- The reproduction above: the Welch p survives `evaluate_search` unchanged, and `significant` is False.
- A two-sample comparison with `p_value=None` is excluded from the BH count m.
- Mixed batch: one Welch and several one-sample comparisons, with q-values computed across all of them.
- `runner_md`'s table row for `runner_selection_edge` carries the same p/q as the prose line.

## Phase 3 — pin the runner corpus to one scan and sim version (Defect B)

1. `candidate_rows(con, sessions=60, scan_version=None, sim_version=None)` gains a `sim_version` filter.
2. `integrity.build_search` and `integrity.weekly_block` pass `scan_version=runner_scan.SCAN_VERSION` and `sim_version=grading.CURRENT_SIM_VERSION`. Both call sites share one helper, so they cannot drift.
3. `runner_md` gains a deterministic line such as "excluded N rows from other versions: scan v1 × sim v3 = 612, …". The miner then knows why n fell. Excluded rows' statistics are **not** shown.
4. The weekly `_sim_version_md` warning stays for the morning lane.
   - If Phase 0 finds more than one `sim_version` among triggered **morning** outcomes, apply the same pinning to `ledger.stats_by` and `ledger.stats_by_outcome`.
   - Otherwise, record that it was checked and not needed.
5. **Defect B2.**
   - `build_search` passes `lane="morning"` to `stats_by` and `stats_by_outcome`. The runner lane is already represented by its own family.
   - `build_weekly_memory` does the same for the `by_*` tables, and adds a separate `by_*_runner_picks` view only if Phase 0 shows the miner needs it; otherwise, omit it.
   - `STATS_DIMENSION_SPEC` comparisons are relabelled `morning:<dim>` in `research_looks`, so the registry states which lane every look came from.

Tests:
- Both integrity callers pass the current versions; assert on the call.
- A v1 row and a v2 row never reach the same `Comparison`.
- The exclusion line counts correctly.
- A `dto_runner` pick never appears in any `STATS_DIMENSION_SPEC` cell.

## Phase 4 — recover n legitimately (explicit and opt-in; trade-off T1)

Pinning cuts runner n to the sessions since 2026-09-24. Two recovery paths exist. Both are re-derivation from this system's own stored inputs, not inheritance:

1. **Re-price under sim_version 4.**
   - `run.py regrade-runner --from 2026-09-11 --force` already exists (explicit and opt-in, `85a7993`). It refuses more than 500 rows without `--force`, and production will exceed that.
   - It re-prices with the current `honest_entry_time` and preserves selection stamps.
   - Run it against production only after Phase 3 ships, so the weekly block never mixes the two semantics.
2. **Re-gate v1 rows** with a new command, `run.py regate-runner --from 2026-09-11 [--apply]`. It is a dry run by default.
   - Apply the three v2 gates to each v1 row's **stored** inputs:
     - `rvol IS NOT NULL`;
     - ticker not in `LANE_EXCLUDED_TICKERS`;
     - session not feed-downgraded.
   - Rows that pass are relabelled `scan_version=2`, with a new `regated_from=1` provenance column (next migration). Rows that fail stay v1.
   - Valid only if Phase 0 check 3 confirmed that detection differs by exactly those gates. Otherwise do not build it, and say why in the commit.

Parity test: build a fixture of v1 rows, run them through the live v2 gate chain (`runner_scan` functions, not a re-implementation), and assert the relabel set is identical.

---

## Constraints

- Additive schema only. No existing column changes meaning.
- `evaluate_search` stays dependency-free (stdlib only).
- Nothing here writes to `playbook.md`.
- Every runner comparison still joins the **same** BH batch as the morning dimensions. No comparison may be moved out of the batch to rescue its q-value.
- If any Phase 2 or Phase 3 change throws inside `integrity._safe`, the weekly loop degrades rather than halting (v4-1). A test proves this.

## Acceptance (production evidence, not merged code)

- `/api/table/research_looks?since=2026-09-26` shows `runner_selection_edge` with `test='welch'`, and its p matches the prose line in that week's LLM archive entry.
- The 2026-09-26 weekly archive (`/api/table/llm_archive?limit=5`) shows the exclusion line, with only (scan 2, sim 4) rows in the runner block, or with re-gated and re-priced rows if Phase 4 ran first.
- Update G5.1 and G5.2 in the self-assessment, citing that run's `run_id`.
