# Self-Assessment Against the Standing Directive — 2026-09-23

**Type:** Baseline assessment. This is the "done" definition that [STANDING_DIRECTIVE.md](STANDING_DIRECTIVE.md) asks for.
**Audience:** Cline, working in this codebase. Jason reviews it.
**Production build assessed:** `f110230`, the latest READY production deployment (Vercel `dpl_3S2vGPvA25E8ga2BNYuiFuyMCk9m`). Production runs HEAD, so every code-path finding below describes live behaviour.
**Specs proposed:** seven, S1–S7, listed in §5.

---

## 0. Phase 0 status — read this first

Phase 0 is **only partly complete**. The code re-read is done. The production read is only partial.

### Verified directly in this assessment

| Source | What it showed |
|---|---|
| Re-read of `agent/integrity.py`, `research.py`, `stats.py`, `runner_research.py`, `loops/{morning,evening,weekly}.py`, `health.py`, `api/index.py`, the four prompts, `config.json`, `playbook.md` | The code-path findings below, each cited `file:line`. |
| Vercel production deployments | HEAD `f110230` is live. `470a8d3` (SCAN_VERSION 2) went live 2026-09-23 22:44 UTC. `85a7993` (sim_version 4) shipped in the 2026-09-24 00:02 UTC deploy. |
| Vercel runtime error clusters, 7 days | The morning job started and got as far as its earnings lookups on every weekday 09-14 → 09-23 (per-day signatures at 11:11 UTC). The runner job did the same 09-21 → 09-23 (15:18 UTC). There were yfinance premarket 1-min bursts: `RCL` ×67 on 09-23 and `NBIS` ×64 on 09-17. ETFs trigger "no earnings dates" lookups every day. |
| Vercel runtime logs | A 7-day query returned only about 20 minutes of lines (2026-09-24 03:54–04:15 UTC). Retention on this plan is short, so anything the loops print, including every weekly integrity WARN, is unrecoverable soon after the run. |
| Local reproduction | The FDR-batch defect in G5.1, with the numbers quoted there. |

### Established earlier (commit record, dated)

- `cc28fbb` (09-22): Anthropic credit was exhausted on these runs:
  - morning on 09-02 and 09-04;
  - weekly on 09-04;
  - runner run **63** and evening runs **64, 65, 66** on 09-22.

  Morning run **62** on 09-22 was `ok` ($1.19).
- `85a7993` (09-23):
  - Alpaca daily bars returned 403 "on every recent date". The code silently fell back to yfinance, which nulled day OHLC and prev close in grading.
  - Evening runs took 455–518 s on runner-heavy days against a 300 s budget.
  - Runner entries walked back up to 2 h inflated the 09-23 field mean R from −0.52 to −0.41.
- `ad74c0f` (09-22):
  - 7 of 10 runner picks graded `invalid_fill` on 09-21 before that fix.
  - The 09-21 weekly rewrite was lost to a cp1252 logging crash.
  - The runner-research figures quoted there (680 triggered, setup edge +0.185R, p = 0.011) come from the **local** ledger (294 + 321 + 65 = 680 triggered rows) under pre-fix entry semantics. They are not production numbers.
- `f110230` (09-23):
  - Until this commit, Alpaca refused **every** runner bracket order (sub-penny prices, 422).
  - UBER was carried from 09-10 to 09-15.
- Project memory: until the Algo Trader Plus upgrade on 09-23, real-time SIP returned 403, the scan fell back to IEX, and the runner lane stood down.

### Not verified — blocked

`/api/table/runs?limit=40` and `/api/status` return 401 without an app session. This assessment ran without the app password, by design. A local search for credentials was attempted and correctly refused. So the following were **not read** from production:
- the `runs` table;
- `loop_health`;
- the current playbook version;
- production row counts.

Every claim below that depends on them is tagged **[prod-unverified]**. To close the gap, open each of these in an authenticated browser session and save the JSON:

```
/api/table/runs?limit=40
/api/status
/api/table/playbook_versions?limit=3
/api/table/daily_plans?since=2026-08-19&limit=60
/api/table/runner_sessions?since=2026-09-10
/api/table/runner_candidate_outcomes?since=2026-09-17&limit=1000
/api/table/live_orders?since=2026-09-10
```

`research_rules`, `research_observations`, `research_looks`, `population_outcomes` and `selection_edge_history` are **not reachable** through `/api/table`. S1 Phase 1 fixes that; see G4.3.

**Rule for every spec below:** if the data access needed for its Phase 0 is not available, record Phase 0 as blocked and stop. Do not search the machine for credentials.

---

## 1. Scorecard

| # | Goal | Status | Why, in one line |
|---|---|---|---|
| 1 | Trade, learn, refine → validated edge | **NOT MET** | The path from pattern to validated rule is disconnected. `record_rule_observation` has no caller, so no rule can ever validate. |
| 2 | Pull data every day | **PARTIALLY MET** | Collection is broad, but an LLM outage still destroys LLM-independent data, and silent provider degradation went undetected. |
| 3 | Relentless data seeking | **PARTIALLY MET** | Counterfactual streams exist. Nothing ranks n<8 cells or steers collection toward them, and the risk prompt rewards not trading. |
| 4 | Update itself daily | **PARTIALLY MET** | The evening loop writes daily, but its confirmations are LLM-judged and ungrounded, and integrity violations are print-only. |
| 5 | Exhaustive structural search, through the integrity layer | **PARTIALLY MET** | There are 24 one-dimensional families under one FDR batch. Three defects inside that batch manufacture or blur edge. There is no 2-D, cross-lane, population or short-side structure mining. |

No goal is fully met.

---

## 2. Goal-by-goal evidence

### Goal 1 — Trade, learn, refine, update toward edge — NOT MET

**What works**
- Grading is deterministic, against real 1-minute bars, and fill-validity is gated in code before an LLM sees a pick:
  - `invalid_fill` reviews are stripped at `evening.py:85-110`;
  - tainted days skip lessons at `evening.py:848-856`;
  - runner `INVALID FILL` rows are masked at `evening.py:156-161`.
- Live paper orders on both lanes should work as of `f110230`. Before that, every runner bracket was refused, so the runner lane had no live fills before 2026-09-24.

**Gaps**
- **G1.1 The validation path is disconnected.**
  - `research.record_rule_observation` (`research.py:258`) has **no caller** anywhere in the repo. Its docstring says "Called by the evening loop", but `evening.py` never calls it.
  - So QUARANTINED rules sit at `forward_n = 0` forever, and `eligible_for_production` (`research.py:352`) can never graduate or retire one.
  - `check_playbook_provenance` (`research.py:457`) therefore makes "NO VALIDATED EDGE YET" the only compliant Current Best Edge, permanently, whatever the evidence says.
- **G1.2 Rules can't be matched to trades even if the hook existed.** `rule_candidates` carry dimension *names* only (`weekly_miner.md:176-183`, consumed at `weekly.py:1026-1033`): no levels, no predicate. Their `discovery_n`, `discovery_effect` and `discovery_q` are the miner's own numbers, not system-computed ones.
- **G1.3 There are two disjoint rule systems, and decay is LLM-applied.**
  - Playbook Setup Rules age on `rule_confirmations` keyed by `setup_type` (`ledger.py:1714`), which are stamped by LLM letter grades (`evening.py:435-438`).
  - Their `last_confirmed v1` stamps (`playbook.md:87-90`) are version labels, not dates, so they cannot be converted to a session count.
  - The 30-session demotion is performed by the LLM (`weekly_miner.md:12-14`). Code only prints `<< STALE` (`weekly.py:581`).
  - The staleness clock counts `daily_plans` rows (`ledger.py:1727`), so days the morning loop failed don't age a rule.
- **G1.4 Inherited constants still gate what is traded.** R:R ≥ 2.0 (`morning_risk.md:16`), `max_picks` 4, and the streak and drawdown sizing clamps are *labelled* `[UNVALIDATED prior]` but not re-derived.
  - Risk clamps as priors are compatible with "automation enforces sizing limits".
  - The R:R bar is different: it decides which evidence gets collected at all.
  - Q13/Q14 are the pre-registered path for re-deriving R:R.
- **G1.5 Playbook freshness [prod-unverified].**
  - The newest playbook in git is v5 (2026-09-06, `a8b8d0f`). Hosted rewrites go only to `playbook_versions` (`memory.py:93-98`).
  - Until 2026-09-23 the weekly miner ran inside Friday evening's 300 s budget, where a timeout lost the rewrite (`README.md:83`).
  - If production is still at v5, nothing has been refined in 17 days.

**Call:** the system trades and grades honestly, but by construction it cannot turn evidence into a validated rule.

### Goal 2 — Pull data every day — PARTIALLY MET

**What works**
- **Breadth.**
  - The runner lane prices every detected candidate, traded or not (`evening.py:181-319`). The local ledger shows 250–331 candidates per session, e.g. 09-17: 306 detected / 294 triggered; 09-21: 322 / 321.
  - The population monkeys grade every premarket gapper, long and short (`population.py:25-27`).
  - The shadow ledger grades every rejection (`evening.py:322-365`).
- **Outages are told apart from stand-downs.**
  - Morning: `session_degraded` and `data_health` (`weekly.py:329-371`).
  - Runner: `provider_degraded` and `feed_downgraded` (`health.py:146-158`).
  - Failures: `api_error` vs `error` (`failures.py`).
  - Health is reported per stage (`health.py`).

**Gaps**
- **G2.1 An LLM outage destroys data that needs no LLM.**
  - The population snapshot, `integrity.on_morning`, runs at `morning.py:1120`, after the analyst call (`:951`) and the risk call (`:982`).
  - Calibration rows and the deterministic open-question increments (`_v3_events`, `evening.py:870`) run after the grader call (`:820`).
  - Credit has already run out on 09-02, 09-04 and 09-22 (four runs that day). No alert fires before or when it happens.
- **G2.2 There is no coverage metric.**
  - The 403 → yfinance fallback that nulled day OHLC "on every recent date" was found by an audit, not by the system.
  - The evening loop has no session-level plumbing check. If bars are missing for every symbol, each row becomes `no_data` (`grading.py:444-445`) and the run is logged `ok`.
- **G2.3 Single points of failure.**
  - Hobby crons fire once a day with no retry.
  - The intraday monitor depends on one Windows machine.
  - GitHub Actions has been dead since 2026-09-09 (`README.md:55`).
- **G2.4 [prod-unverified]** The runner lane stood down every session until the 09-23 plan upgrade. `runner_sessions` will show how many sessions were lost.

### Goal 3 — Relentless data seeking — PARTIALLY MET

**What works**
- The exploration lane (`config.json:18-24`): 1 pick per day, R:R 1.8–2.0, 0.25R.
- The shadow ledger, the population monkeys, the runner field, and paired Arm B.
- MDE per cell (`stats.py:274`), and the under-powered list shown to the miner (`integrity.py:358-380`).

**Gaps**
- **G3.1 n<8 is reported, not worked.**
  - The under-powered list reaches only the weekly miner, and only cells with MDE > 1.0R, top 25.
  - The morning analyst's context (`agent/memory.py`) contains nothing from the research layer: no backlog, no quarantined rules, no open-question needs.
  - Nothing ranks cells by missing evidence or steers collection toward them.
- **G3.2** The only deliberate evidence purchase is one pick per day, along one axis (R:R).
- **G3.3 The risk manager is incentivized against data.**
  - "You are rewarded for what you reject and for the days you send the desk home flat" (`morning_risk.md:4`).
  - "If fewer than 2 candidates survive … strongly consider no_trade" (`morning_risk.md:79`).
  - Local ledger (not production): 5 of 6 morning plans were no-trade. The production rate is **[prod-unverified]**; `daily_plans` will show it.
- **G3.4** Open questions only advance when the desk happens to trade the setup. No stream is pointed at them on purpose.

### Goal 4 — Update itself every day — PARTIALLY MET

**What works**
- The evening loop writes daily: lessons (deduped), lesson confirmations, calibration, rule confirmations, open-question progress, shadow outcomes, runner outcomes.
- `health.py:261-294` flags grading that teaches nothing.
- **Writer isolation holds in code.**
  - The evening loop has no write path to `playbook.md`.
  - `weekly.py:1062` is the only production caller of `write_playbook`.
  - Mock runs cannot write it (`weekly.py:988-999`).

**Gaps**
- **G4.1 Integrity checks are advisory and invisible in production.**
  - Eight checkers at `weekly.py:1039-1061` only log a WARN, and `write_playbook` runs regardless (`:1062`).
  - `log()` is a print (`util.py:83-101`), and Vercel's runtime-log retention here is minutes to an hour.
  - The morning agent and the runner selector act on whatever was written.
- **G4.2 Daily confirmations are LLM-judged and ungrounded. This is the confabulation risk.**
  - The grader's output contract requires a "tape narrative" (`evening_grader.md:45`), while the prompt concedes it is given no bars (`:30`). The call runs without extended thinking (`evening.py:821`).
  - `lesson_updates` confirm/contradict entries are accepted without checking that the lesson's condition occurred (`evening.py:865-867`), yet confirmations drive promotion (`weekly_miner.md:89`).
  - One A/B grade stamps the whole setup's decay clock (`evening.py:435-438`).
  - Numbers cited in lessons are never checked against the computed fields the prompt tells the grader to quote (`evening_grader.md:26-28`).
  - Confirmations are a bare `+1` plus INSERT (`ledger.py:795-806`), so every evening retry double-counts them.
  - Contradictions get no dated row, so the holdout split sees confirmations out-of-sample but not contradictions, which biases tiers upward.
- **G4.3 Diagnostic blind spot.** The integrity layer's own tables are not in `_QUERYABLE_TABLES` (`api/index.py:537-559`), so production integrity state can't be inspected without a database credential.
- **G4.4** Research diagnostics (the FDR search, the under-powered list, the runner edges) are computed only inside the weekly call.

### Goal 5 — Exhaustive structural search through the integrity layer — PARTIALLY MET

**What works**
- One Benjamini–Hochberg batch covers 14 morning dimensions, 10 runner dimensions, and the runner setup and selection edges (`integrity.py:191-216`).
- n<8 is never significant (`stats.py:381`).
- The two-dimension cap is enforced (`research.py:73`).
- The looks registry records cumulative search width (`research.py:114-181`).
- MDE, the population and random-selection nulls, and the friction stress ladder are all in place.

**Gaps.** The first two are defects inside the integrity layer itself.
- **G5.1 [DEFECT] The FDR batch overwrites a two-sample test with a one-sample test.**
  - `evaluate_search` re-tests every comparison as one-sample (`stats.py:368-371`). That overwrites the Welch test `selection_edge` supplies (`runner_research.py:225`).
  - Reproduced with 12 sessions, each 3 selected ~N(0.3, 0.6) and 20 unselected ~N(0, 2.5):

    | Where the test runs | p | q | Verdict |
    |---|---|---|---|
    | Welch (as supplied) | 0.0955 | — | "no detectable selection edge" |
    | Inside the FDR batch | 0.0026 | 0.0026 | **significant = True** |

  - The miner sees both readings: the prose verdict and a starred table row (`runner_research.py:281-299`).
  - `research_looks` records a survivor.
- **G5.2 [DEFECT] The runner corpus pools incompatible populations.**
  - Both callers call `candidate_rows(con)` with no `scan_version` (`integrity.py:210`, `:264`), despite `470a8d3` saying the filter exists.
  - There is no `sim_version` filter at all.
  - Production crossed both boundaries in the last 24 h: scan_version 2 from 2026-09-23 22:44 UTC, sim_version 4 from 2026-09-24 00:02 UTC.
  - **The first mixed weekly run is the Saturday 2026-09-26 cron (`0 14 * * 6`).**
- **G5.2b [DEFECT] The morning dimensions pool both lanes.**
  - `build_search` calls `stats_by` and `stats_by_outcome` with no lane (`integrity.py:201-202`), and so does `build_weekly_memory` (`memory.py:283-293`).
  - So all 14 "morning" cells mix morning picks with runner picks: up to 10 a day, at a different $/R, under different entry semantics.
  - Selected runner trades are then counted a second time in the runner family.
  - This contradicts the miner prompt's own "never pool them" (`weekly.py:938-941`).
  - `invalid_fill` rows are correctly excluded from the means: they carry `triggered=False`.
- **G5.3 One-dimensional only.**
  - Every comparison has `n_dimensions = 1`.
  - Pairs are allowed but never computed, so a two-dimension rule candidate's numbers come from the miner reading the CSV.
- **G5.4 No cross-lane structure.**
  - The runner and morning stop-distance buckets share edges (`runner_research.py:116-121`) but are never read together.
  - Nothing checks consistency across strata, which is what separates a *structural* attribute from a pooled artifact.
- **G5.5 The short side is thin by construction.**
  - The runner lane is long-only (`evening.py:281`).
  - The population monkeys grade every gapper long and short, but `population_outcomes` are used only as a null (`integrity.py:249-250`) and never mined.
- **G5.6 Only mean R is tested.** `stats.py` has no win-rate or proportion statistic.

---

## 3. Priority fixes — what is currently working against the goals

Ranked by damage, then by urgency.

| # | Problem | Goals hurt | Fixed by | Why now |
|---|---|---|---|---|
| **P1** | The FDR batch turns a null selection edge into a significant one (G5.1). | 5, 1 | S1 | Ship before the 2026-09-26 weekly cron. |
| **P2** | The runner corpus mixes scan_version 1/2 and sim_version 3/4 (G5.2), and the morning dimensions pool both lanes (G5.2b). | 5, 1 | S1 | Same deadline. The first mixed run is 09-26. |
| **P3** | No rule can ever validate (G1.1, G1.2). | 1 | S2 | Goal 1 is structurally blocked until this lands. |
| **P4** | Playbook violations are print-only and never block anything. The prompt's `t >= 1.5` edge bar contradicts the FDR / VALIDATED rule (G4.1). | 1, 4 | S3 | The next rewrite is 09-26. |
| **P5** | An LLM outage deletes deterministic data (G2.1), and there is no coverage alarm (G2.2). | 2 | S4 | Every lost day is permanent. |
| **P6** | Grader confabulation vectors (G4.2). | 1, 4 | S5 | Lesson and rule counts are growing on ungrounded confirmations. |
| **P7** | The risk prompt rewards no-trade days, and the analyst can't see the backlog (G3.1, G3.3). | 2, 3 | S6 | Participation is the data bottleneck. |
| **P8** | Production integrity tables are unreadable (G4.3). | 4 | S1 Phase 1 | It blocks the Phase 0 of S2, S5 and S7. |

---

## 4. Trade-offs between goals

None of the proposals relaxes any of these:
- n<8 suppression;
- FDR control;
- the forward-only observation guard;
- the two-dimension cap;
- fill-validity gates;
- writer isolation. The weekly miner stays the sole writer of `playbook.md`.

Where goals pull against each other:

| # | Tension | Resolution |
|---|---|---|
| T1 | S1 version-pinning cuts runner n today (Goal 3 volume vs Goal 5 integrity). | Integrity wins. Recover n by re-pricing under sim_version 4 (`run.py regrade-runner`, opt-in, already exists) and by re-gating v1 rows through the v2 gate chain from their stored inputs. That is re-derivation, not inheritance. |
| T2 | S3 enforcement vs freshness: refusing a whole rewrite would freeze learning (Goal 4). | Neutralize line by line. Only the offending line is withheld, and a failed Runner Lane section falls back to the explicit no-rules placeholder. |
| T3 | S6 buys more exploration: more paper exposure and a noisier "desk" record (Goal 1 clarity) vs more data (Goals 2 and 3). | Every probe is tagged, and exploration status becomes a mined dimension, so its effect is measured, not pooled. Paper account only. |
| T4 | S7 widens the search, which raises every q (Goal 5 breadth vs Goal 1 power). | Families are pre-registered in code, every look is counted, and the width is printed. FDR prices the widening; nothing is exempt. |
| T5 | Money, not code: Vercel Pro removes the single-machine intraday dependency, and Anthropic credit top-ups end the recurring outages. | Jason's decision. Flagged in S4; not done autonomously. |

---

## 5. Spec index and order

| Order | Spec | Closes | Size | Depends on |
|---|---|---|---|---|
| 1 | [INTEGRITY_REPAIR_SPEC_v1](INTEGRITY_REPAIR_SPEC_v1.md) | P1, P2, P8 | S | nothing. **Deadline 2026-09-26 14:00 UTC.** |
| 2 | [WRITE_TIME_ENFORCEMENT_SPEC_v1](WRITE_TIME_ENFORCEMENT_SPEC_v1.md) | P4 | M | S1 Phase 1 (to read violations back) |
| 3 | [DATA_CONTINUITY_SPEC_v1](DATA_CONTINUITY_SPEC_v1.md) | P5 | M | nothing |
| 4 | [FORWARD_VALIDATION_SPEC_v1](FORWARD_VALIDATION_SPEC_v1.md) | P3 | L | S1 (a correct batch to take discovery numbers from) |
| 5 | [GRADER_GROUNDING_SPEC_v1](GRADER_GROUNDING_SPEC_v1.md) | P6 | M | S2's predicate schema |
| 6 | [EVIDENCE_BACKLOG_SPEC_v1](EVIDENCE_BACKLOG_SPEC_v1.md) | P7, G4.4 | M | S2 (quarantined rules), S4 (coverage) |
| 7 | [STRUCTURAL_MINING_SPEC_v1](STRUCTURAL_MINING_SPEC_v1.md) | G5.3–G5.6 | L | S1 (the batch must be correct before it is widened) |

The order holds even though S2 closes the biggest gap. S1 has a hard date. S3 and S4 stop harm that is happening now. S2 is the largest change, and it needs S1's corrected batch as its source of discovery numbers.

---

## 6. How to use this document

For every future task:

1. Name the goals it moves and the G / P items it closes.
2. Name any T-style trade-off it introduces.
3. Run its Phase 0 against production.

When a spec lands, update its row in §1 and cite the run IDs or table rows that prove the change in production. A goal moves to **FULLY MET** only on production evidence, never on merged code.

---

## 7. Implementation log — 2026-09-24

All seven specs are implemented, tested (913 tests pass) and **live in production**. Deployments: `dpl_3c58DvKDrH4PwpWCQx3KXJWCLxea` (S1), `dpl_GCEDr2PpQfxUAkvtZCHcDAmk6ZSR` (S3+S4), `dpl_2RQ4imSPYDk1CiJs86wtoV2e9c4b` (S2+S5), `dpl_Ek268mQoGAryUdBALK5cGw7Myhdq` (S6+S7+morning fix). Scorecard rows in §1 **do not change yet**: a goal moves only on production evidence (§6), and each spec's Acceptance section says what that evidence is.

| Spec | Commit | Deviation from the spec as written |
|---|---|---|
| S1 | `adc2c7f` | Morning dimensions keep their bare names in `research_looks`: no `morning:` prefix. The families are already unambiguous. |
| S3 | `f6bc604` | Provenance blocks only in Current Best Edge / Setup Rules / Regime Rules, never on a line that negates edge. Run on the real v5 playbook, the unscoped checker flagged exactly those innocent lines. The Runner Lane keeps its own blocking checker. |
| S4 | `7443b45`, `baa66da` | The plumbing rule is "null on every graded row, ≥ 5 rows" (the directive's definition). No per-field baseline threshold, because production null baselines were unreadable. |
| S2 | `3f831f7` | Cohort BH runs at α = 0.05, not q = 0.10. A cohort of one is the old test; larger cohorts only get stricter. |
| S5 | `f3f3fce` | A quoted number matches when an input number *rounds* to it. The ±1 draft let `9.99x` match a `10:00` timestamp. |
| S6 | `a63fba5` | The morning block round-robins across rules, questions and cells. A straight top 8 would have been all 14 open questions. |
| S7 | `f615261` | `pop_adv_bucket` was not built: no re-derived edges exist for it, and inventing some would break the directive's first constraint. |

**Still pending (needs Jason or production access):**
1. **Every spec's Phase 0 production check.** This session could not read `/api/*`. `research_*`, `playbook_violations`, `data_coverage`, `alerts`, `grader_rejections`, `evidence_backlog` and `daily_diagnostics` are now readable at `/api/table/<name>`.
2. **Opt-in data recovery**, which needs `DATABASE_URL` and was not run:
   - `run.py regate-runner --from 2026-09-11` (dry run first, then `--apply`);
   - `run.py regrade-runner --from 2026-09-11 --force`.

   Without them, the runner corpus the weekly search mines starts at 2026-09-24.
3. **The 2026-09-24 morning run timed out** (504 at 300 s: both LLM calls returned, but the run was killed before the plan was saved). The run lock was retryable from 11:41 UTC, so a dashboard re-run can recover the day. The same timeout hit on 08-28, so the morning is at the budget independently of these changes. From tomorrow it records per-stage timings (`runs.stages`), and health warns above 210 s. The decision on trimming it (bundle size, moving arm B off the request, splitting the run) should be made from those timings.
4. **Money, not code (T5):** Anthropic credit/auto-reload, and Vercel Pro. Credit exhaustion now emails one alert per day and no longer costs deterministic data.
