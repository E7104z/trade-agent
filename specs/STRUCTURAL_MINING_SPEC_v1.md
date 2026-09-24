# STRUCTURAL MINING SPEC v1 — winners' shared attributes, long and short, all under one FDR batch

**Status:** proposed, 2026-09-23 ([SELF_ASSESSMENT_2026-09-23.md](SELF_ASSESSMENT_2026-09-23.md) G5.3–G5.6)
**Goals served:** 5 (primary), 1, 3
**Scope:** `agent/stats.py`, `agent/integrity.py`, `agent/runner_research.py`, `agent/population.py`, `agent/buckets.py` (from S2), `agent/research.py` (width reporting), `prompts/weekly_miner.md`; the next free migration; tests.
**Depends on:** S1, because the batch must be correct before it is widened. Also S1's `Comparison.test` field, and S2's `agent/buckets.py`.
**Writer isolation:** unchanged. Everything here feeds the weekly miner's input. Only the weekly loop writes the playbook.

---

## 0. The problem

The directive asks for exhaustive search across setups, tickers, timeframes and regimes, for *structural* attributes of winners, long or short, with no false edge. Today the search is:

- **One-dimensional only.** Every comparison has `n_dimensions = 1`: 14 morning dimensions (`integrity.py:64-79`) and 10 runner dimensions (`runner_research.py:52-63`). The two-dimension cap (`research.MAX_RULE_DIMENSIONS = 2`) permits pairs, but no pair is ever computed. So a two-dimension rule candidate can only be discovered by the miner reading the CSV, outside the batch.
- **Blind to consistency.** A 1-D cell that survives FDR is reported as a single pooled number. Nothing asks whether it holds within each regime, each direction, each lane, each month. That test separates a *structural* attribute from a pooled artifact of one hot week.
- **Mean R only.** `stats.py` has no proportion test. "What do winners have in common?" is a question about win *frequency* by attribute, and it is never asked.
- **Thin on the short side by construction.**
  - The runner lane is long-only (`evening.py:281`).
  - Morning shorts are rare.
  - The population monkeys grade every premarket gapper both ways: momentum and fade, long and short (`population.py:25-27`, `:230`). The local ledger shows ~34 rows per arm per day, half of them short.
  - But `population_outcomes` feed only the null comparison (`integrity.py:249-250`, `population_md`) and are never mined by dimension. The largest short-side corpus the system owns is unused for Goal 5.

**Every widening below adds looks.** All of it goes into the **same** Benjamini–Hochberg batch as today, is recorded in `research_looks`, and is printed as width. Wider search raises every q-value; that is the correct price, and it is trade-off T4 below.

---

## Phase 0 — production verification (hard blocker; a self-check, not a sign-off)

This needs S1 Phase 1 (readable `research_looks` and `population_*`). Data access means an authenticated session or a local `DATABASE_URL`. If neither is available, record Phase 0 as blocked and stop. Never search the machine for credentials.

1. **Baseline width.** From the latest `research_looks` session, record:
   - comparisons per family;
   - testable m;
   - survivors;
   - the cumulative survivors from `looks_summary`.

   This is the "before" for T4.
2. **Pair admissibility.** For each lane and dimension, count the levels with n ≥ 8. List the dimensions with at least 2 such levels. Only those can enter a pair (Phase 1 rule).
3. **Population volume.** Pull `/api/table/population_outcomes?since=2026-08-19&limit=1000` and count rows by `arm` × `direction` × `plan_date`, with non-null `r_multiple`. Confirm grading happens daily in production. The local ledger has only 2 graded days, so it can't show this.
4. **Short-side n.** Count triggered, fill-valid rows with `direction = 'short'` per lane.

---

## Phase 1 — pre-registered two-dimension families

1. Add a new module-level declaration, `PAIR_SPEC`, versioned as `PAIR_SPEC_VERSION`. It is a fixed list declared in code **before** looking, and it changes only by commit, like the open-question registry. Initial proposal:

   | lane | pair |
   |---|---|
   | morning | `setup_type × regime_tag` |
   | morning | `direction × trend_side_200d` |
   | morning | `direction × regime_tag` |
   | morning | `setup_type × stop_atr_bucket` |
   | runner | `runner_stop_atr_bucket × runner_rvol_bucket` |
   | runner | `runner_regime × runner_detection_bucket` |
   | runner | `runner_trend_side_200d × runner_tc_bucket` |

2. **Admission rule, stated in advance.** A pair is computed in a run only if both marginal dimensions pass Phase 0 check 2 (at least 2 levels with n ≥ 8 in that lane). Pairs that fail are listed as "declared, not yet admissible", not silently dropped.
3. Every computed joint cell is a `Comparison` with `n_dimensions = 2` in the same batch. Cells under n = 8 are still tested and counted, as `evaluate_search` already does, and are never significant.
4. S2's rule registration then takes a two-condition predicate's discovery numbers **from the batch**, not the "unscreened" fallback, whenever the pair was admitted.

Tests:
- An inadmissible pair produces no comparisons and one listing line.
- The joint-cell labels come from `buckets.label`.
- No `n_dimensions = 3` comparison can be constructed; assert this.

## Phase 2 — the structural-consistency check (descriptive; adds no looks)

1. For every comparison flagged `significant` in the batch, compute its **sign and n within each stratum**:
   - lane, where the dimension exists in both lanes with the **same** bucket definition in `buckets.py` (e.g. stop/ATR, 200d side);
   - `regime_tag`;
   - `direction`;
   - calendar month, as the timeframe.
2. Report "consistent in k of K strata with n ≥ 8", suppressed below n = 8 per stratum. Show no p-values and no stratum means, only sign and n. This is a robustness read, not a new test, so it does not enter the batch.
3. **Label rule for the miner:** a finding may be called *structural* only if it (a) survived FDR in the batch **and** (b) has the same sign in every stratum with n ≥ 8, with at least 2 such strata. Otherwise it is a pooled finding and is labelled as one.

Test: a synthetic cell that is positive in one regime and negative in another, and positive pooled, is labelled "pooled, not structural".

## Phase 3 — mine the population family (the short side)

1. Join `population_outcomes` to `population_universe` on `(plan_date, symbol)`, and to `daily_plans.regime_tag`. Dimensions, all computable from stored columns:
   - `pop_gap_atr_bucket` (`gap_pct / atr_pct`, same edges as `gap_atr_bucket` via `buckets.py`);
   - `pop_adv_bucket`;
   - `pop_in_scan_universe`;
   - `pop_regime`;
   - `pop_was_picked`.
2. Cells are formed per `arm × direction × dimension level`, so **long and short are always separate cells**. Family prefix: `population:`. Same BH batch.
3. Filter to one `sim_version`, as S1 does for the runner.
4. The markdown labels the family "mechanical-rule evidence over the whole gap population — not desk selection". A population finding may *nominate* a morning-lane rule candidate with the same predicate. That rule is then quarantined and forward-tested on desk trades (S2) like any other; population rows never validate it (S6 Phase 5).
5. Optional, and gated on Phase 0 cost: enrich `population_universe` with `trend_side_200d` from the evening's daily-bar prefetch. Ship only if it doesn't threaten S4's time budget.

Tests:
- Long and short rows never share a cell.
- The family is in the same `evaluate_search` call as the morning and runner families.
- Rows from two sim_versions never pool.

## Phase 4 — the winner-frequency family

1. Add `stats.fisher_exact_2x2(a, b, c, d)`: an exact, two-sided, stdlib-only test via the hypergeometric distribution, using `math.lgamma`.
2. For every 1-D cell in each lane, test in-cell vs out-of-cell × win vs not-win.
   - **Win uses the ledger's existing definition:** triggered and `r_multiple > 0`, fill-valid only (`ledger._stats_rows` already counts `wins` this way). No new threshold is introduced.
   - Each test is a `Comparison` with `test = "fisher"`, so S1's Phase 2 keeps its p-value.
3. It joins the same batch. Win-rate display still obeys n ≥ 8 suppression everywhere.

Tests:
- Fisher p against known 2×2 tables, to 1e-9.
- A `fisher` comparison's p survives `evaluate_search` unchanged.
- Width accounting counts it.

## Phase 5 — say how wide the search is

1. `provenance_md` gains a per-family width table:

   | family | comparisons | testable | survivors |
   |---|---|---|---|
   | morning 1-D | … | … | … |
   | morning 2-D | … | … | … |
   | runner 1-D | … | … | … |
   | runner 2-D | … | … | … |
   | population | … | … | … |
   | winner-frequency | … | … | … |

   It is followed by expected false positives uncorrected (0.05 × m).
2. `weekly_miner.md`: "A wider search is not more evidence. The q-values already price the width; cite them. A finding is *structural* only under Phase 2's label rule."
3. `exploration_lane` (from S6) and `lane` become 1-D dimensions, so probe trades and lane differences are measured rather than pooled.

---

## Explicitly not proposed here

**Runner short-side detection.** A "falling runner" scan would be new detection semantics: scan_version 3, its own parity harness and its own Phase 0. It is a new hypothesis, not a mining change. It is listed as an open item for a future spec, so it isn't smuggled in.

## Trade-off T4 (flagged, not silently resolved)

Phases 1, 3 and 4 may multiply the testable m several-fold. Every existing cell's q rises, and some findings that would survive today's narrower batch will not survive the wider one. That is Goal 5's breadth traded against Goal 1's power. The resolution:
- families are pre-registered in code;
- admission is decided by a rule stated in advance;
- every look is counted and printed;
- FDR sets the price.

If Phase 0 shows the widened m would leave **no** cell able to survive at plausible effect sizes (use `stats.required_n`), ship Phases 2 and 5 first, and hold Phases 1, 3 and 4 until n grows. Say so in the commit.

## Acceptance (production evidence)

- The first weekly run after shipping writes `research_looks` rows for the `morning 2-D`, `population:` and `fisher` families, with the width table in the LLM archive.
- Every "structural" claim in the playbook is backed by a Phase 2 consistency line.
- Update G5.3–G5.6 in the self-assessment, citing the `run_id` and the new m.
