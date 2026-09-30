# Playbook audit fixes (v1) — 2026-09-29

**Status:** implemented locally, all tests passing (1115). **Not deployed.** Phase 0 below is a hard blocker before shipping, per the Standing Directive.

**Trigger:** an external review of production playbook v9 against the enforcement code. Every finding in it was checked against the code; the ones that held are fixed here, and three more defects surfaced while tracing them (marked *found in audit*).

---

## What was wrong, and what changed

### 1. The measurement layer

| Defect | Evidence | Fix |
|---|---|---|
| Runner RVOL bucket mislabelled | `min_rvol` is 0.0 but the lowest label was still "1.5-2", so every RVOL under 2.0x went there (local data: median 0.52x, p90 1.43x) — 983/1003 rows | `buckets.runner_label`: `<0.5, 0.5-1, 1-1.5, 1.5-2, 2-3, 3-5, 5+` |
| Runner stop-ATR bucket degenerate | EMA9 stop vs **daily** ATR14 is < 0.5 ATR by construction (median stop 0.8% of price) — 992/1003 rows | runner `<0.5` split into `<0.15, 0.15-0.3, 0.3-0.5`; `RUNNER_STOP_ATR_COARSE` maps back for cross-lane reads |
| One fact counted as four "STRUCTURAL" findings | a cell holding ~99% of a dimension, tested against 0, is the lane mean again | `stats.evaluate_search`: a cell with >= 90% of its dimension's rows is `degenerate` — shown, not tested, not a look |
| Selection verdict from one session | 9 selected / 442 unselected in ONE contested session passed the n >= 8 floor | `runner_research.selection_edge` needs >= 3 contested sessions (same bar as the random control) |
| Two R measures in one FDR batch (*found in audit*) | 1-D cells read `r_planned`; pairs, win-frequency, 2-D discovery and forward observations read `r_multiple` | `research.realized_r` everywhere in research/structural |

### 2. Runner picks pooled into the morning desk (*found in audit*)

Runner selections are stored in `picks` (setup_type `dto_runner`). These all read `picks` with no lane filter: the evening calibration rows and open-question counters (local data: 10 of 11 calibration rows were runner picks), the participation funnel, shadow "captured R", the management/sizing counterfactuals, and the selection ablation. Every one is now morning-lane only. Stored rows are filtered at read time (`calibration_aggregate`, `question_progress_totals`), and the evening loop no longer writes them. Nothing stored is rewritten.

### 3. The selection ablation was biased (*found in audit*)

"Beat 0.3% of random portfolios" was an artifact. The desk's arm held only its TRIGGERED picks (runner picks included), while the random arm drew from rejections worth 0R when they never filled. A losing book ranked against zero-diluted portfolios lands near 0% whatever its selection skill. Method 2 counts an untriggered taken pick as 0R, the same as an untriggered rejection, and is morning-only. Rows computed by method 1 are shown as **withdrawn** in the playbook and on the dashboard (`selection_edge_history.method_version`, migration v25).

### 4. The shadow ledger's reading

- One window for the shadow ledger and the funnel (the trailing 20 non-degraded morning sessions), one degraded-session predicate, and a reconciliation line (funnel rejections = graded − degraded + ungraded). This closes the 150 vs 142 vs 141 mismatch.
- Verdict = one-sample test on mean forgone R per scored rejection. The "TAIL ARTIFACT → filters saved money" framing is gone: the right tail is part of the filters' cost, and the ex-top-3 figure is a sensitivity read only.
- `kill_checks` (every binding check; risk prompt v12): a rejection that failed two checks is `multiple`, charged to no single filter (the UAL case). Categories use the 11-value kill-check enum where recorded.

### 5. Open questions are answered by code (`agent/question_eval.py`)

Each question has a declared analysis. Progress is the count its threshold is compared against (the smaller arm for a two-arm question). A question is answerable only when every arm reaches threshold, and only then does it get a Result, FDR-corrected across the questions. Registry changes: Q1 counts only observations where both entries filled, and labels `[taken]`/`[shadow]` (WING); Q9 compares triggered trades by stop bucket, not stopped trades, which all lose ~1R; Q13/Q14 are one paired test with untriggered = 0R on both arms; Q5 excludes rows with no kill check; Q10 is flagged as having no consistent definition; **Q15 (new): does chosen size predict R?** Sizing had no test at all.

### 6. Write-time enforcement (`agent/playbook_enforcement.py`)

- **Status, not keywords:** every Setup/Regime Rules line must carry a VALIDATED `[rule: R…]` tag or `[UNVALIDATED prior]`; the system adds the tag. Sizing lines citing evidence (`n=`, `last_confirmed`) too (the n=1 correlated-cap rule).
- **Tier ≤ realized n:** "provisional, n=0 triggered" is corrected to `none`.
- **Decay in every rule section**, from each line's own `last_confirmed`, and the miner is shown every dated line.
- **Honest redaction:** a withheld setup line names the setup and its status.
- **System-rendered sections:** Calibration, Selection Edge, Runner Lane, Shadow Ledger Summary, Participation Funnel (new), Open Questions.
- **Regime coverage:** an observed regime with no rule (uptrend/risk_off) gets an explicit "no playbook rule" line.
- **Changelog checked:** the same line checks; lines about system-rendered sections, unanswerable or withheld questions, or withheld setups are withheld; the system writes the decay line. The email gets the checked changelog, and key findings that fail the same checks are dropped.
- The Reading section's contract was rewritten to match what is enforced.

### 7. Dashboard

"Current best edge" crowned a setup on 3 trades. It now shows a VALIDATED rule, else the best setup at n >= 8 labelled descriptive. The "Runner selection edge" card was the morning ablation; it is relabelled, and method-1 rows are withdrawn. The open-questions card shows answerability.

---

## Phase 0 — verify against production before deploying

Claude has no production read path. Jason, please pull these (the `/api/table` JSON is enough):

1. `runner_candidates` — distribution of `rvol` (expect most < 1.5; confirms the relabel matters).
2. `runner_candidate_outcomes` — distribution of `stop_dist_atr` (confirms the 0.15/0.3 edges split the population; adjust before shipping if not).
3. `picks` where `setup_type='dto_runner'` joined to `calibration` on (plan_date, ticker) — how many calibration rows are runner-only.
4. `research_rules` — any rule whose `predicate_json` names `runner_rvol_bucket` or `runner_stop_atr_bucket`. A predicate on a relabelled level would stop matching; none are expected (v9 lists only two QUARANTINED rules, both morning-lane).
5. `selection_edge_history` — the rows that will render as withdrawn.

## Deploy notes

- Migration v25 is additive (three nullable columns) and runs once on the first connect after deploy.
- Nothing rewrites stored data. v9 stays live until the next weekly rewrite (Saturday), when the new enforcement first applies.
- Prompts changed: `weekly_miner` v15, `morning_risk` v12 (optional `kill_checks` field).
- Not changed on purpose: forward-validation thresholds (forward n >= 8, 10 sessions). Both v9 candidates were quarantined at the last weekly run, so forward n = 0/8 is expected, not a stall. Lowering the bar to make the state machine "move" would trade integrity for motion.
