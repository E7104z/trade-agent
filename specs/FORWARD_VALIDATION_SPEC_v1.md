# FORWARD VALIDATION SPEC v1 — make "validated edge" reachable

**Status:** implemented 2026-09-24 (`3f831f7`), live in production; production acceptance pending. Proposed 2026-09-23 ([SELF_ASSESSMENT_2026-09-23.md](SELF_ASSESSMENT_2026-09-23.md) P3, G1.1–G1.3)
**Goals served:** 1 (primary), 5, 4
**Scope:** new `agent/buckets.py`; `agent/research.py`, `agent/integrity.py`, `agent/ledger.py`, `agent/runner_research.py`, `agent/loops/evening.py`, `agent/loops/weekly.py`, `prompts/weekly_miner.md`; the next free migration; tests.
**Depends on:** S1 (a correct FDR batch as the source of discovery numbers, and readable `research_*` tables).
**Writer isolation:** the evening loop writes only `research_observations` and `research_rules` counters. Both are ledger tables, already outside `playbook.md`, so it gains no playbook path. The weekly loop stays the sole playbook writer.

---

## 0. The problem

RESEARCH_INTEGRITY_SPEC_v4 designed the right lifecycle:

> DISCOVERED → QUARANTINED → (forward-only observations) → VALIDATED | RETIRED

Its load-bearing guard is `record_rule_observation`, which refuses anything not dated after `quarantined_at`. **That function has no caller.** Grep the repo: it is defined at `research.py:258`, and its docstring says "Called by the evening loop". `evening.py` never calls it. As a result:
- every QUARANTINED rule has `forward_n = 0` forever;
- `eligible_for_production` (`research.py:352`) can never graduate one or retire one;
- `check_playbook_provenance` (`research.py:457`) refuses any edge claim that isn't VALIDATED.

So the only compliant Current Best Edge is "NO VALIDATED EDGE YET", permanently, whatever the market does. Goal 1 is structurally unreachable.

The hook could not have been written as the system stands, for two reasons:
- **Rules cannot be matched to trades.** `rule_candidates` carry dimension *names* only, e.g. `["setup_type", "regime_tag"]` (`weekly_miner.md:176-183`). There are no levels. Their `discovery_n`, `discovery_effect` and `discovery_q` are the miner's own numbers, stored as given (`weekly.py:1027-1031`).
- **Bucketing has no single source.** Morning-lane buckets are SQL `CASE` expressions (`_OUTCOME_DIM_EXPR`, `ledger.py:1138`). Runner buckets are Python (`runner_research.bucket_of`). Code that matches a trade against a rule would be a third copy, free to disagree with what the miner was shown. That is the same failure `runner_playbook_section` was made a shared function to prevent.

Two further gaps:
- **Forward tests are uncorrected.** `eligible_for_production` graduates on per-rule `p < 0.05`. With K rules reaching target in the same run, K uncorrected tests are the multiple-comparison artifact the directive forbids, moved one stage later.
- **Decay is LLM-applied.** The 30-session demotion is done by the miner (`weekly_miner.md:12-14`); code only prints `<< STALE` (`weekly.py:581`). The staleness clock counts `daily_plans` rows (`ledger.py:1727`), so a day the morning loop failed doesn't age a rule. Playbook stamps like `last_confirmed v1` (`playbook.md:87-90`) are version labels, not dates.

---

## Phase 0 — production verification (hard blocker; a self-check, not a sign-off)

This needs S1 Phase 1, which makes the `research_*` tables readable. Data access means an authenticated session or a local `DATABASE_URL`. If neither is available, record Phase 0 as blocked and stop. Never search the machine for credentials.

1. Pull `/api/table/research_rules?limit=200` and count by `state`. Expected: every QUARANTINED rule has `forward_n = 0`.
2. Pull `/api/table/research_observations?limit=50`. Expected: empty. **If it is not empty, something writes it. Find that path before building a second one, and amend this spec.**
3. Pull `/api/table/llm_archive?limit=20` and inspect every weekly response. Count `rule_candidates`, and record whether any carries levels in its statement text that could seed a predicate (they get re-registered in Phase 2, not migrated).
4. Pull `/api/table/rule_confirmations?limit=200`. Confirm `rule_id` values are `setup_type` strings. Record the date span.
5. Current production playbook (from `/api/table/playbook_versions?limit=1`): list the formats every `last_confirmed` stamp uses.

---

## Phase 1 — one bucketing source

1. Create `agent/buckets.py`. Each dimension is declared once, as data: `{dimension: (column, edges, labels, null_label='?')}`. This covers every entry in `integrity.STATS_DIMENSION_SPEC` and `runner_research.MINING_DIMENSIONS`. Categorical dimensions (`setup_type`, `direction`, `trend_side_200d`, `regime_tag` …) are declared as passthrough with `COALESCE(…,'?')`.
2. Two derivations come from that one declaration:
   - `sql_case(dimension, alias)`, which replaces `_OUTCOME_DIM_EXPR`;
   - `label(dimension, row)`, which replaces `runner_research.bucket_of` and is re-exported under the old name for callers.
3. A parity test runs the SQL and the Python over the same fixture of boundary values (every edge ± ε, plus None) and asserts identical labels for every dimension.
4. **No bucket edge changes in this phase.** `research_looks` rows after the change are identical to before on a fixture ledger; assert this.

## Phase 2 — rules carry a machine-readable predicate, and the system computes their discovery numbers

1. Contract change in `weekly_miner.md`, same prompt-version bump as S3:

   ```json
   "rule_candidates": [{
     "statement": "…",
     "predicate": {"lane": "morning",
                   "conditions": [{"dimension": "setup_type", "level": "gap_go"},
                                  {"dimension": "regime_tag", "level": "uptrend/risk_on/lowvol"}]}
   }]
   ```

   It has 1 or 2 conditions; `MAX_RULE_DIMENSIONS` is unchanged. The miner no longer supplies `discovery_n`, `discovery_effect` or `discovery_q`.
2. `research.register_rule` validates the predicate against this run's `search.comparisons`:
   - each dimension must be one the lane actually mines;
   - each level must be a label the system emitted for it this run.

   Anything else is recorded as state **`REJECTED_UNMATCHABLE`**, with the reason, in the graveyard like `REJECTED_STRUCTURAL`.
3. **Discovery numbers are computed by the system:**
   - 1 condition: n, mean R and q come from that cell's `Comparison` in the S1-corrected batch.
   - 2 conditions: the joint cell is computed deterministically from the same row set. `discovery_q = NULL` with note `"2-D cell outside this run's FDR batch — unscreened at discovery"`, until S7's pre-registered 2-D cells put it inside the batch. Forward validation, not discovery, is what admits a rule, so an unscreened discovery is safe to quarantine.
   - Any number the miner wrote in `statement` is kept verbatim in `note` for audit only.
4. **Identity.** `rule_id_for(statement, dims)` receives the canonical predicate as `["setup_type=gap_go", "regime_tag=…"]`. The same predicate with reworded prose gives the same id, which closes the laundering path at the identity layer, as v4 intended.
5. Migration, additive:
   - `research_rules.predicate_json`, `research_rules.lane`;
   - `research_observations.source_key`, with a unique `(rule_id, source_key)`.

Tests:
- A valid 1-D and a valid 2-D predicate register with system-computed numbers.
- A made-up level is rejected as `REJECTED_UNMATCHABLE`.
- A 3-condition predicate is rejected as `REJECTED_STRUCTURAL`.
- Reworded prose with the same predicate returns the existing rule.

## Phase 3 — the evening loop records forward observations

1. Add `research.observe_session(con, d)`. It is deterministic, and runs in the evening after outcomes are saved and runner candidates are priced. Under S4 that is before the grader LLM call.
2. The fill-valid rows it considers, per lane:
   - **morning:** outcomes with `triggered = TRUE`, `exit_reason <> 'invalid_fill'`, `execution_anomaly` not true, and `setup_type <> 'dto_runner'`;
   - **runner:** `runner_candidate_outcomes` with `triggered = TRUE`, `r_multiple IS NOT NULL`, and the current scan and sim version (S1).

   Runner rules are statements about the candidate population, selected or not. The selector's choices are a separate question, and selection edge answers it.
3. For every rule in state QUARANTINED or VALIDATED whose lane matches:
   - label each row with `buckets.label`;
   - on a predicate match, call `record_rule_observation(con, rule_id, d, r, source_key)`.

   Source keys are `outcome:<pick_id>` or `runner:<date>:<ticker>`. The forward-only guard (`obs_date > quarantined_at`) is untouched; this phase is the first caller it has ever had.
4. `record_rule_observation` becomes idempotent on `(rule_id, source_key)`, because evening retries happen (runs 64–66 on 09-22). It returns False on a duplicate, and the counters are unchanged.
5. The evening RUN SUMMARY line gains `forward observations recorded=N`.

Tests:
- A fixture day with one matching and one non-matching trade records exactly one observation.
- A rerun of the same date records zero more.
- `invalid_fill` and `execution_anomaly` rows never record.
- An observation dated on the quarantine day is refused (the existing guard).

## Phase 4 — the forward stage gets multiplicity control and deterministic decay

1. **Cohort FDR.** In `eligible_for_production`:
   - gather every QUARANTINED rule that meets both its forward-n target and its time floor this run;
   - BH-correct their forward p-values at level `alpha` (0.05, the existing per-rule bar). A cohort of one is then exactly the old test, and larger cohorts are only stricter. (As implemented: an earlier draft said `research.fdr_q` = 0.10, which would have *loosened* a lone rule's bar from 0.05 to 0.10.)
   - graduate only the BH-rejected rules whose effect has the same sign as at discovery.

   Rules that reach target without surviving are RETIRED, as today. Untestable samples (p is None) keep waiting, as today. **This is a tightening, not a relaxation.**
2. **Decay by silence.** A VALIDATED rule with no forward observation for ≥ 30 **market sessions** becomes QUARANTINED again, with a fresh `quarantined_at` and `note += 'decayed: silent N sessions'`. Market sessions are counted with `util.is_market_day` between the last observation date and the run date, not with `daily_plans`. A second decay retires it with reason `repeatedly stale`, matching the playbook's graveyard rule. The 30 comes from the directive and the playbook's principle 8; it is not a new constant.
3. **Decay by reversal.** A VALIDATED rule whose trailing `forward_n_required` observations have the opposite sign at `p < alpha` is RETIRED with reason `forward effect reversed`.
4. All transitions are applied in code inside `integrity.weekly_block`, before the miner call. The miner receives them as facts in `_graduation_md`, which already says "do not re-decide them; state them".
5. Setup-rule staleness (`ledger.rule_staleness`) is recomputed with the same market-calendar session count, and its STALE list is handed to the miner as applied fact. S3's line-level enforcement gains a ninth checker: a Setup Rules line for a setup that is stale must carry `[auto-demoted: stale N sessions]` or `UNVALIDATED`; otherwise the line is replaced.

Tests (the BH arithmetic is exact at level α = 0.05; don't approximate it):
- m = 1, p = 0.04: the bound is 0.05, so it graduates. This is identical to the old per-rule test.
- m = 2, p = (0.04, 0.30): the rank-1 bound is 0.025 and the rank-2 bound 0.05, so **neither** graduates.
- m = 3, p = (0.04, 0.60, 0.70): the rank-1 bound is 0.0167, so **nothing** graduates. Uncorrected, the 0.04 rule would have passed at α = 0.05. This case is the reason for the phase.
- m = 2, p = (0.001, 0.60): the strong rule still graduates.
- Decay counts market sessions across a holiday correctly.
- Second decay retires.
- Reversal retires.

---

## Constraints

- n ≥ 8 forward, the 10-day time floor, the two-dimension cap and sign consistency all stay. Phase 4 adds a correction; it removes nothing.
- Discovery numbers never come from the LLM again.
- The research layer still must not break a trading loop. `observe_session` runs under `integrity._safe`, and a failure logs and returns 0.

## Expected effect, stated honestly

- **Runner lane:** it prices roughly 250–330 candidates per session (local ledger), so a runner rule can reach 8 forward observations within days. The 10-day floor, not n, will bind, which is its purpose: one regime can't validate a rule.
- **Morning lane:** rules will take weeks, because participation is the bottleneck (S6).
- "NO VALIDATED EDGE YET" may remain true for a while. The difference after this spec is that it can become false.

## Acceptance (production evidence)

- `/api/table/research_observations?since=<ship date>` gains rows every session that has a matching trade.
- `research_rules.forward_n` moves.
- Within 10+ sessions of the first quarantine, the weekly `_graduation_md` shows at least one decision (GRADUATED, RETIRED or explicit WAITING with counts) computed from real forward data.
- Update G1.1–G1.3 in the self-assessment, citing the rule ids.
