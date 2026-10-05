# EDGE ACCELERATION SPEC v1 — from data to validated edge, faster, without lowering a bar

**Status:** built 2026-10-04 (commits 85c8c49 … this one). Jason's direction the same day: "Data to develop an edge as fast as possible. Don't ask me, just build the agent to do this." Phase 0 is no longer a person's job: Part 3 makes the system ask its own production data every evening.
**Goals served:** 1 (primary), 2, 3, 4, 5.
**Writer isolation:** unchanged. Nothing here writes `playbook.md`; every decision below is applied in code and handed to the weekly miner as fact.

---

## 0. The problem, measured

The pipeline was: data → weekly FDR batch → the miner nominates a rule → quarantine → one forward look at n ≥ 8 → VALIDATED or RETIRED (permanently, under the graveyard rule). Three things stopped real edges reaching the end of it:

1. **The forward test could not see a realistic edge.** Trade R has a standard deviation of 1.0–1.9R (local ledger: morning 1.45, runner 1.90, population 0.99, shadow 1.65). One look at n = 8 and two-sided 0.05 needs a forward mean near +1.2R. Simulated on R shaped like this desk's (stop −1R, target +2R, session exits between; scratch study 2026-10-04):

   | true edge | P(validated), old single look | P(retired) |
   |---|---|---|
   | +0.3R | 6% | 94% |
   | +0.5R | 12% | 88% |
   | +1.0R | 50% | 50% |

   The trader-hypothesis test was worse: a mean ≤ 0 at the first look (n = 8) refuted the idea, which a real +0.3R idea shows about a third of the time.
2. **A survivor reached the forward test only if the miner nominated it** in prose that week. Exit policies were the exception, because their family is pre-registered.
3. **Evidence arrived one hand-picked trade at a time.** The morning desk's cells gather a forward observation every other session. The population family (the short side, Goal 5) was in the batch as looks but had no lane, so it could never become a rule at all.

And every spec's Phase 0 was blocked, because only a person pasting `/api/table` JSON could read production.

---

## Part 1 — the forward test spends one budget across many looks (`agent/sequential.py`, migration v32)

- One-sided α = 0.025 in the direction of discovery. That equals the old two-sided 0.05 plus the sign check, so the budget is unchanged.
- Cumulative spending A(t) = 0.025·t², with t = min(1, n / n_max). Each look tests at the increment. By the union bound, all looks together stay ≤ 0.025, whenever they happen.
- Non-binding futility: retire when the 80% upper bound is below a minimum useful edge. Stopping a test can only lower its false-positive rate.
- At n = n_max the budget is spent, and a rule not shown by then retires.
- **The design is fixed per lane, in code** (`design_for`):

  | lanes | n_max | futility floor | why |
  |---|---|---|---|
  | morning, runner_exit, hypotheses | 96 | +0.10R | evidence arrives one trade (or one session) at a time |
  | runner, gapfield (field lanes) | 768 units | +0.05R | 5–20 (session, industry) units a session; small, frequent edges |

- Cohort BH over the run's looks on p scaled to the whole budget (`p_adj = p·α/α_look`). With one rule this is exactly p ≤ α_look.
- Simulated results:

  | case | old | new |
  |---|---|---|
  | morning, +0.5R | 12% | 71% |
  | morning, +1.0R | 50% | ~100% |
  | morning, false positives | 1.5% | 0.6% |
  | morning, flat rule retired after | 15 sessions | ~45 sessions |
  | hypotheses, +0.5R | 21% | 76% |
  | field, +0.12R | 22% (96 / 0.10) | 77% |
  | field, +0.20R | 52% (96 / 0.10) | 99% |
  | field, false positives | — | 0.7% |

  `tests/test_sequential.py` re-runs a null simulation on every test run (FP ≤ 0.048 at 500 runs).
- **Rules the old test retired for want of significance** are re-quarantined once, on fresh evidence only (`reopen_underpowered_retirements`). Their clock restarts that day; reversed and stale retirements stay retired.
- **Hypotheses** use the same machinery: first look at 8 sessions, then weekly. SUPPORTED means significant after cohort BH. REFUTED means futile or out of budget. Otherwise the idea keeps collecting. A mean ≤ 0 no longer refutes on its own.

## Part 2 — every batch survivor goes to the forward test (`research.register_batch_survivors`)

- Every significant mean-R 1-D cell and admitted pair of the morning, runner and gap-field lanes is registered through `register_candidate`, with source `system`, and quarantined the same run.
- Losers are registered too: a validated loser is an exclusion the desk can enforce.
- At most 12 new rules per lane per run, smallest q first.
- Identity is the predicate, so a registered cell keeps its clock and a retired one stays retired.
- The miner is told which rules the system quarantined.

## Part 3 — the agent audits its own production data (`agent/selfcheck.py`, migration v33)

Runs in the evening loop. Each check writes one row to `self_checks`, adds a line to a SELF-CHECK block at the end of the evening email, and feeds a Self-check stage on the dashboard health panel:

| check | bad / warn when |
|---|---|
| plumbing | a lane produced rows today and none were graded/priced (bad) |
| lanes | backlog_probe silent ≥ 5 sessions (names the cause: never tagged vs never admitted); broker cap clipping a plan |
| morning budget | a run ≥ 270 s of 300; a session with no morning run on record (a 300 s kill logs nothing) |
| hypotheses | any pick tagged to an idea before it opened (bad); lane on 3+ sessions with no idea written |
| lessons | info: how many active lessons have never been confirmed or contradicted |
| edge pipeline | rules quarantined 2+ weeks with zero forward observations |
| gap field | no forward gap field for the previous session (nightly not running) |
| persona | info: the persona spec's descriptive 3-session check, run by the system once three sessions exist |

- **The persona check** reports pick counts with and without the hypothesis lane. `persona-monitor` gained that split and `--since` (2026-09-29, the daily floor going live, for a like-for-like baseline).
- **This replaces "ask Jason to paste /api/table JSON"** for every check above. `/api/table/self_checks` holds the history.

## Part 4 — the gap field (`agent/gapfield.py`, `agent/loops/nightly.py`, migration v34)

- **What it prices:** every operating company's common stock that opens ≥ 3% from its prior close, with prior close ≥ $5 and 20-day ADV ≥ $10M. On 2026-10-02 that was 5,376 names in the universe, 210 gappers and 139 liquid events. Each gap is traded both ways (momentum and fade) by the population monkeys' rules: entry at the open, 1-ATR stop, exit at the close.
- **Eight dimensions:** gap in ATR, gap %, price, liquidity, 200d trend, prior day, volume ratio and SPY's gap. All are labeled from bars before the gap day.
- **Forward:** the nightly cron (`/cron/nightly`, "0 3 * * *" UTC, 22:00–23:59 ET; no LLM, no orders, its own 300 s budget) prices each closed session, about 11 s for the whole market. It records forward observations for gap-field rules in (session, industry) units. A missed night is caught up, up to 5 sessions.
- **History:** a resumable backfill of 500 sessions (~2 years, ~160k rows; 13 s per 400-symbol chunk measured locally, more on Neon). It is **discovery only**: forward observations come only from `source = 'forward'` rows dated after a rule's quarantine. A chunk is started only if it is expected to finish inside the budget.
- **In the weekly batch:** the batch reads a nightly per-cell summary (strategy × dimension, named as pre-registered pairs), so survivors flow through Part 2 like any other lane's.
- **Commands:** `run.py nightly`, `run.py gapfield`.
- **First real-data read** (one 400-symbol chunk of history, uncorrected and in-sample, NOT a finding): mechanical gap-up longs lose broadly (−0.08 to −0.13R in several cells) and fades win modestly (+0.07 to +0.11R). Those are exactly the kind of small, frequent effects the field-lane design exists to test.

## Configuration changes

- `hypothesis_lane.max_picks` 2 → 4 and `max_open` 6 → 8. Under a test that waits for evidence rather than guessing at n = 8, ideas live longer. Throughput per idea, not a weaker test, is what frees the notebook.
- `live_paper.max_picks_per_day` 22 → 24 (sized to the lane, as before). These are budget choices, not edge claims.

## Trade-offs (flagged, not silently resolved)

- **T1 — slower verdicts on flat morning rules.** They retire in ~45 sessions instead of 15. They sit in quarantine meanwhile and cannot be cited as edge, so the cost is only cohort width. Accepted: the old speed came from killing real edges.
- **T2 — history is biased.** Survivorship (today's listings) biases history up; the daily-bar stop biases every arm down. History may only nominate, and forward rows decide.
- **T3 — the batch is wider.** The gap field adds ~108 cells. BH adapts, and most of these cells are well powered, but each one is a counted look (`research_looks`).
- **T4 — reopened rules get a second, independent test.** For those few rules the per-rule error over both tests is at most 2α. The alternative was leaving real edges in the graveyard on a 10%-power verdict.
- **T5 — more hypothesis-lane trades.** Up to 4 a day at 0.25R, outside the 3R total, on the paper account. The persona check reports picks with the lane split out, so this does not confound it.

## Constraints (unchanged)

The forward-only guard, n ≥ 8 before any look, the ten-day quarantine floor, cohort FDR, the sign requirement, the 2-D cap, fill-validity gates, writer isolation and playbook provenance are all unchanged. No number any test depends on comes from an LLM.

## Operations

- **Deploy:** migrations v32–v34 are additive. The first request after the deploy runs them, so warm the schema before the 10:45 UTC morning cron (`scripts\intraday_cron.ps1 -Once`, or load the logged-in dashboard). The nightly cron is a light first request too.
- **Backfill:** fills over ~3–5 nights. `run.py gapfield` (or the evening email's self-check line) shows progress.
- **Weekly runs:** the first one after the deploy registers the system's batch survivors and takes the first sequential looks. Expect "STILL WAITING — look at n=…" lines where the old test would have retired rules.
