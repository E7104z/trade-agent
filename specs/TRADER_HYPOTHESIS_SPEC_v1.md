# TRADER HYPOTHESIS SPEC v1 — the trader tests its own ideas

**Status:** proposed and built 2026-10-04; enabled for the 2026-10-05 session at Jason's direction ("make it work tomorrow"), which resolves T3 in favour of shipping now. Phase 0 was not run (no prod read path); its checks stand as post-ship checks. See **As built** at the end for where the build differs from the draft below.
**Goals served:** 1 (primary), 3, 5, 4
**Scope:** new `agent/hypotheses.py`; `agent/loops/morning.py`, `agent/loops/evening.py`, `agent/loops/weekly.py`, `agent/loops/paired.py`, `agent/memory.py`, `agent/ledger.py`, `agent/integrity.py`, `agent/delivery.py`, `prompts/morning_analyst.md`, `prompts/morning_risk.md`, `prompts/evening_grader.md`, `prompts/weekly_miner.md`, `config.json`, `agent/config.py`; migration v31; tests; prompt-render fixtures.
**Depends on:** FORWARD_VALIDATION_SPEC_v1 (rule lifecycle, cohort BH), EVIDENCE_BACKLOG_SPEC_v1 (exploration lane, `probe_for`), the coverage lane (`1c4b5ff`).
**Writer isolation:** unchanged. The register is a ledger table. The morning and evening loops write register rows and pick tags. Neither writes `playbook.md`. Verdicts are applied in code. The weekly miner receives them as facts and remains the sole playbook writer.

---

## 0. The problem

Jason, 2026-10-04: "I really want the trader trying different trades to test things or patterns it thinks it's seeing. Trading to gather more data." And: "We are trying our hardest to mimic real human behavior as it relates to learning and getting better at trading."

The desk already trades to collect data, but the system chooses every data-gathering trade:

| Lane | Who aims it |
|---|---|
| coverage | the setup taxonomy: the best reject per setup |
| daily_floor | the risk manager's softest kill |
| exploration `rr_below_control` | a fixed R:R band |
| exploration `backlog_probe` | the system's n<8 backlog |

None of them lets the trader say "I think I'm seeing X; I'm going to trade it to find out." The specific gaps:

1. **A belief has no route to a test.** The persona tells the trader it "may believe something it cannot yet act on" (`persona.md:13`). The only outlets for that belief are prose in a thesis or a lesson, and neither aims a trade at it.
2. **Lessons steer trading without being tested.** The grader writes lessons, and the analyst is told to apply them ("do not propose chasing one long — or propose the fade instead and cite the lesson", `morning_analyst.md:87`). A lesson is confirmed or contradicted by the grader's judgment (`lesson_updates`), not by R, and only when a trade happens to land in it. That is an untested idea acting like a rule.
3. **The grader must throw away anecdotes.** "A single ordinary event is NOT a lesson — it is an anecdote" (`evening_grader.md:78`). That rule is correct, and a human trader would follow it. But a human would also write the anecdote down as *worth testing*. Here it is discarded.
4. **Coverage takes one instance per setup** (`_run_coverage`, `morning.py:1074`). A second gap_fade that the trader wants for a specific reason cannot trade.

### The human loop this spec builds

A disciplined trader learns like this, and each step maps to a phase:

1. **Notice** something, in the morning on the tape or in the evening review. (Phases 2 and 3)
2. **Write it down** as a claim, with what would prove it wrong, before trading it. (Phase 1)
3. **Commit to a sample size** before looking at results. (Phase 1)
4. **Trade it small and deliberately**, on purpose, labeled. (Phase 2)
5. **Review each trade honestly**, including whether it actually expressed the idea. (Phase 3)
6. **Judge at the committed sample, not before**, and let the idea die. (Phase 4)
7. **Remember what was tested**, so a dead idea is not quietly retried. (Phase 5)
8. **Graduate a survivor into a real rule** only through the existing forward test. (Phase 4)

The integrity layer stays intact throughout. The design is the existing rule lifecycle with a new source of candidates: the trader.

---

## Phase 0 — production verification (hard blocker; a self-check, not a sign-off)

Claude has no prod read path. Jason pastes `/api/table` JSON (see `production-data-access`). If the data isn't available, record Phase 0 as blocked and stop.

1. `/api/table/picks?since=2026-09-29&limit=500`: count by `exploration_lane`, and count non-null `probe_for`. **Question:** does `backlog_probe` actually fire? If it is near zero, say why (the analyst doesn't tag, or the risk manager's kill isn't `rr`/`conviction`). That failure mode applies to this spec too.
2. Picks per day since 9/29, against `live_paper.max_picks_per_day` (20). **Question:** how much broker headroom exists for two more picks?
3. `/api/table/lessons?limit=300`: count active lessons, how many carry a predicate, and the confirmation/contradiction distribution. **Question:** how many lessons are steering the analyst untested?
4. `/api/table/runs?limit=40`: morning run durations by stage, including the first sessions on `d15a2dd` (thinking 4000 tokens, web searches 3). **Question:** how much headroom under 300s exists for a longer prompt (T2)?
5. `/api/table/daily_plans?since=2026-09-29`: in `analyst_json`, count candidates whose thesis says "exploration", "off-playbook" or "test". **Question:** is the trader already trying to express hypotheses in prose?

---

## Phase 1 — the hypothesis register (migration v31)

1. New table:

   ```
   trader_hypotheses(
     hyp_id          VARCHAR PRIMARY KEY,  -- 'H-' + hash of the normalized statement
     created_date    DATE,
     source          VARCHAR,     -- 'analyst' | 'grader' | 'lesson'
     from_lesson_id  INTEGER,     -- when nominated from the lesson bank
     statement       VARCHAR,     -- the tradable claim
     refutation      VARCHAR,     -- what result would prove it wrong, written up front
     direction       VARCHAR,     -- 'long' | 'short' | NULL (either)
     setup_type      VARCHAR,     -- taxonomy value, 'novel', or NULL
     predicate_json  VARCHAR,     -- optional 1-2 condition predicate, validated as in research.py
     target_n        INTEGER,     -- set by the system from config, never by the LLM
     max_n           INTEGER,     -- the one pre-registered extension
     min_sessions    INTEGER,
     state           VARCHAR,     -- OPEN | SUPPORTED | REFUTED | INCONCLUSIVE | ABANDONED | EXPIRED | REJECTED
     opened_at       DATE,
     closed_at       DATE,
     last_tagged_at  DATE,
     verdict_json    VARCHAR,     -- written once, at the verdict
     note            VARCHAR
   )
   ```

   `pick_hypotheses(pick_id, hyp_id, session_date, tagged_by)` holds the many-to-one tag, plus a `hypothesis_id` column on `picks` for the primary tag. Add both tables to `_QUERYABLE_TABLES`.
2. **Pre-registration is frozen at insert.** `statement`, `refutation`, `target_n`, `max_n` and `min_sessions` are never updated. Changing an idea means registering a new hypothesis. A test asserts that no `UPDATE` statement names those columns.
3. **The system sets the bar, not the LLM.** `target_n` = `research.DEFAULT_FORWARD_N` (8), `max_n` = 2 × target (16), and `min_sessions` = `DEFAULT_MIN_QUARANTINE_SESSIONS` (10). These are existing constants. A target the LLM wrote in its output is ignored and logged.
4. **The claim is always tradable.** Every hypothesis is tested as one claim: *trading this idea, as tagged, has positive expectancy in R.* The `statement` says what the idea is. The test does not depend on how the statement is worded. This matches the runner-arm live-eligibility rule ("its own expectancy is positive").
5. **Registration is refused** (state `REJECTED`, with the reason recorded) when:
   - the statement is empty, or the refutation is missing;
   - the predicate is invalid (3+ conditions, or an unknown dimension or level). The hypothesis still registers without a predicate; only the predicate is dropped, and the drop is logged;
   - it near-duplicates an OPEN hypothesis. This reuses `ledger.find_duplicate_lesson`'s normalization and its Jaccard ≥ 0.8 test, generalized to a shared helper;
   - it near-duplicates one REFUTED or EXPIRED within the last 30 market sessions. This is the graveyard rule: a dead idea comes back only after the tape has changed;
   - `max_open` is already reached.

Tests:
- Insert, then attempt to update a frozen column: the attempt fails.
- An LLM-supplied `target_n` of 3 is ignored, and the row stores 8.
- A reworded near-duplicate of an open hypothesis is REJECTED.
- A refuted idea re-proposed after 5 sessions is REJECTED; after 31 sessions it registers.
- A 3-condition predicate is dropped and the hypothesis still registers.

## Phase 2 — the morning trader proposes and trades hypotheses

1. **Memory.** `build_morning_memory` adds a `## YOUR OPEN HYPOTHESES` block after DATA-STARVED. Each line has the id, statement, refutation, n / target, sessions elapsed, and the last tagged date. **Counts only.** It shows no running mean, win rate or sign. The block explains why: "You committed to judging at the target. A running average before then is the look that turns noise into a finding." The last-20 trades table still shows individual trades, as it does today.
   - It also adds a `## TESTED AND CLOSED (last 10)` block with each hypothesis's id, statement, verdict, and its n and mean R *at the verdict*. That number is the registered look, so showing it is legitimate. This is the trader's memory of what it tried.
2. **Analyst output** (`morning_analyst.md`, version 12 → 13):
   - A candidate may carry `"hypothesis_id": "H-…"` to test an open hypothesis.
   - The top-level field `"new_hypotheses"`, up to `max_new_per_day`, holds `{"statement", "refutation", "direction", "setup_type", "predicate"?}`, and a candidate may reference one as `"hypothesis_id": "new:0"`.
   - The top-level field `"abandon"` lists `[{"hyp_id", "why"}]`.
   - Beyond the one-per-setup list, the analyst may bring up to `max_picks` extra candidates that exist only to test a hypothesis. They may be a second instance of a setup.
   - `setup_type: "novel"` is allowed **only** with a `hypothesis_id`. It is for an idea that fits no taxonomy value. Without a tag, the validator drops the candidate.
   - The prompt wording: "A hypothesis is a belief you are willing to pay a small amount to test. It never raises conviction, never sizes a trade, never ranks a candidate. Write it down before you trade it, say what would prove it wrong, and let the system judge it at the sample you committed to." This is consistent with `persona.md:13`. **`persona.md` is not edited**, so the persona version stays 1 (see T3).
3. **Risk manager** (`morning_risk.md`, version 13 → 14): add the kill-check `hypothesis_fit` to `_KILL_CHECK_ENUM`, meaning "this trade does not test the stated hypothesis." The risk manager attacks the design of the test as well as the trade. A hypothesis candidate otherwise faces every kill-check like any candidate.
4. **The lane** (`_run_hypothesis_lane`, config `hypothesis_lane`) runs inside `_run_test_lanes` **after coverage and before the daily floor**, so coverage's selection is byte-identical to today.
   - **A tagged candidate already in the plan** (an approval, or a coverage pick): stamp `hypothesis_id` and add no new pick. An approved trade that expresses the idea is a valid observation, and R is in units of risk, so size doesn't matter.
   - **A tagged candidate not in the plan**: admit it at `size_r`, conviction capped at 3, with `exploration_lane = "hypothesis"`, unless its `primary_kill_check` is in `blocking_kills` (grounding, entry_type, event_risk, hypothesis_fit). Every candidate must pass `validate_one`, at up to `max_picks` per day. These picks sit outside `max_picks` and the 3R total, as coverage picks do.
   - Add `"hypothesis"` to `ledger.TEST_LANES` and `morning._TEST_LANES`. Hypothesis picks keep their shadow row, and they stay out of the streak and drawdown breakers and out of the judgment-only stats.
5. **Registration happens in the morning loop, before any order.** New hypotheses are inserted at plan time, with `opened_at` = the plan date. Tags are written to `pick_hypotheses` before `_place_premarket_orders`. **The tag is forward by construction:** it exists before the outcome does. Nothing in the evening or weekly loop may write `pick_hypotheses` (enforced by a test: a grep allowlist, in the style of `test_traders_log_firewall.py`).
6. **The paired arm B** runs the same lane, so the comparison stays unconfounded (as `_run_test_lanes` already requires). Arm B may tag existing hypotheses but never registers new ones. Its `new_hypotheses` are logged and discarded. Arm B rows go to `paired_arms`, as today.
7. **The broker cap.** `live_paper.max_picks_per_day` goes from 20 to 22, sized to `hypothesis_lane.max_picks` (T4).

Tests:
- A tagged candidate killed on `rr` is admitted at 0.25R with lane `hypothesis` and its tag.
- A candidate killed on `grounding` or `hypothesis_fit` is never admitted.
- A tagged approval gets a stamp and no extra pick.
- Coverage's admitted set is identical with the lane on and off (a fixture day).
- `setup_type: novel` without a tag is dropped.
- Arm B registers nothing.
- The cap holds.
- With the lane disabled, the prompt renders are byte-identical to the current fixtures.

## Phase 3 — the evening review: anecdotes become hypotheses, and each test trade is reviewed

`evening_grader.md`, version 7 → 8:

1. **Each hypothesis-tagged pick** gets `"hypothesis_review": {"expressed": true | false, "note": "…"}`, which says whether the trade, as it actually traded, was a fair test of the idea. **It is an annotation only.** It never removes a trade from the count: only fill validity excludes a trade, as everywhere else. The count of `expressed: false` is printed beside the verdict in Phase 4, so a sloppy test shows up as one instead of being edited out.
2. **The grader may register hypotheses**, under `"hypotheses"`, up to `max_new_per_day`, in two forms:
   - `{"statement", "refutation", …}`, from what today showed. **This is where an anecdote goes now.** A single ordinary event still may not become a lesson; it may become a hypothesis worth testing. The existing numbers check applies: every number in the statement must appear in the input.
   - `{"from_lesson_id": N}` nominates an existing lesson for a deliberate test. That turns problem 2 into a test. The lesson stays in the bank.
3. Grader proposals are registered by the evening loop, with `opened_at` = the next market session. **They can only be tested on trades tagged from the next morning onward**, so the evening loop never gets to write a tag (Phase 2.5).

Tests:
- `expressed: false` leaves n unchanged.
- A grader hypothesis quoting a number not in the input is REJECTED.
- A nominated lesson registers with `from_lesson_id` and `source = 'lesson'`.

## Phase 4 — verdicts, computed in code, weekly

`hypotheses.evaluate(con, session_date)` runs in `integrity.weekly_block`, before the miner call, next to `eligible_for_production`.

1. **The sample** is every pick tagged to the hypothesis that triggered and is fill-valid. Realized R = `COALESCE(r_planned, r_multiple)`, as in `question_eval`.
2. **The look schedule is pre-registered as two looks:** at `target_n` and at `max_n`, each only after `min_sessions` market sessions since `opened_at`. Each look tests at α/2 (Bonferroni over the two looks). The test is one-sample t of mean R > 0, using `stats.one_sample_t`.
3. **Cohort BH.** Every hypothesis reaching a look in the same run, plus every hypothesis ABANDONED since the last run counted at p = 1, is BH-corrected together at α/2. Abandoning is allowed, but it cannot be used to hide a loser and shrink the family.
4. **Outcomes:**

   | At the look | Mean R | BH-significant | State |
   |---|---|---|---|
   | target_n | > 0 | yes | SUPPORTED |
   | target_n | > 0 | no | stays OPEN until max_n |
   | target_n | ≤ 0 | — | REFUTED |
   | max_n | > 0 | yes | SUPPORTED |
   | max_n | any | no | REFUTED ("not shown at n = 16") |
   | p is None (degenerate sample) | — | — | stays OPEN, as rules do |

   - **EXPIRED:** no tagged trade in 30 market sessions (the existing decay constant). The hypothesis enters the graveyard with its counts.
   - **ABANDONED:** the analyst withdrew it. It counts at p = 1 in that week's cohort.
   - `verdict_json` stores n, mean, std, t, p, q, the cohort size, the `expressed: false` count, and the look number. It is written once.
5. **SUPPORTED is not edge.** A supported hypothesis says *the trader's selection within this idea worked on n trades*. A rule claims something about every trade in a cell. They are different claims.
   - **With a predicate:** a SUPPORTED hypothesis is registered as a research rule (`source = 'trader'`, discovery numbers = the hypothesis sample) and quarantined the same run. Its forward clock starts then. It becomes edge only through `eligible_for_production`, as any rule does.
   - **Without a predicate:** it stays SUPPORTED in the register. The miner is asked to express it as a `rule_candidate`. Until a predicate exists, it cannot be cited for conviction or size, and it can keep collecting under the lane at minimum size.
   - **Check:** `check_playbook_provenance` is unchanged. A hypothesis id cited as edge in the playbook fails provenance the same way a quarantined rule does.
6. **The miner** receives the register as applied fact (`_hypotheses_md`, the same posture as `_graduation_md`: "do not re-decide them; state them").

Tests (exact BH arithmetic, as in FORWARD_VALIDATION_SPEC_v1):
- m = 1, p = 0.02, look 1: the bound is 0.025, so SUPPORTED.
- m = 1, p = 0.04, look 1, mean > 0: it stays OPEN (it would have passed at an uncorrected 0.05).
- m = 2, with one ABANDONED: p = (0.02, 1.0), the rank-1 bound is 0.0125, so nothing is supported.
- The mean ≤ 0 at target is REFUTED.
- At max_n with no significance, REFUTED.
- Thirty silent sessions, crossing a holiday, gives EXPIRED.
- A SUPPORTED hypothesis with a predicate creates a QUARANTINED rule with `quarantined_at` = the run date.
- `expressed: false` trades are counted.

## Phase 5 — the trader remembers, and Jason can see it

1. The **evening email** gains one line per open hypothesis: the id, a short statement, n / target, and today's tagged trades. Counts only.
2. The **weekly email** lists the week's verdicts with their numbers.
3. **`run.py hypotheses`** prints the register; `--show H-…` prints one hypothesis with its tagged trades and its review notes.
4. The **dashboard** shows the register as a table (`/api/table/trader_hypotheses`) and needs no new panel in v1.
5. The **trader's log** is not touched. Its firewall test stays green, and a hypothesis is not a slot it can name.

---

## Configuration (it ships disabled; enabling it is a separate config commit, as the persona was)

```json
"hypothesis_lane": {
  "enabled": false,
  "size_r": 0.25,
  "max_picks": 2,
  "max_new_per_day": 1,
  "max_open": 6,
  "blocking_kills": ["grounding", "entry_type", "event_risk", "hypothesis_fit"]
}
```

`max_picks`, `max_new_per_day` and `max_open` are **budget choices, not edge claims.** They are recorded in Sizing Policy as `[UNVALIDATED prior]`. At 2 lane picks a day plus tagged approvals and coverage picks, six open hypotheses at n = 8 clear in roughly 15–25 sessions. That is a planning estimate, not a measured one.

## Trade-offs (flagged, not silently resolved)

- **T1. More hypotheses mean less power per test.** Cohort BH controls false discoveries, but every extra open idea makes each one harder to support. `max_open` = 6 bounds this. The abandonment-at-p = 1 rule stops the cheap way around it.
- **T2. Morning time budget.** The analyst prompt grows by two blocks, and the output grows by a few fields. The morning was at its 300s limit, and `d15a2dd` just halved thinking to buy headroom. Phase 0 check 4 decides whether the blocks fit. If headroom is thin, cap the closed list at 5 and truncate statements to 140 characters.
- **T3. Confound with the persona treatment.** The persona went live 10/03, and its 3-session check (10/05–10/07) is descriptive by design. Adding a lane that raises trade counts during those sessions makes the persona monitor's trade-count shift unattributable twice over. **Recommendation:** build now and enable on or after Thu 10/08, after the 3-session check. Jason decides.
- **T4. The broker cap goes from 20 to 22.** That is more paper exposure, at 0.25R each and outside the 3R total, like coverage. The account is paper only.
- **T5. Cherry-picking.** A trader who wants an idea to work could tag only the trades it expects to win. That is the claim being tested ("the trader's selection within this idea"), and it is why SUPPORTED is not a rule. Every tag is written before the outcome, and every fill-valid tagged trade counts. The LLM excludes nothing.
- **T6. `setup_type: novel` adds a level to the setup dimension.** It is mined like any level, under n < 8 suppression. If it becomes a dumping ground, the Phase 5 register will show it.

## Constraints

- Nothing relaxes: n < 8 suppression, FDR, the forward-only guard, the 2-D cap, fill-validity gates, writer isolation, and the playbook provenance check.
- No number the test depends on comes from the LLM: not the target, not the look schedule, not the verdict, not the discovery numbers.
- The research layer must not break a trading loop. `_run_hypothesis_lane`, registration and `evaluate` run under `integrity._safe`. A failure logs and leaves the plan exactly as coverage left it.

## Acceptance (production evidence)

- Week 1: `trader_hypotheses` has OPEN rows from both `analyst` and `grader` sources; `picks` shows `exploration_lane = 'hypothesis'` on at least one day; `pick_hypotheses` has tags on approvals or coverage picks too.
- No `pick_hypotheses` row has a `session_date` before its hypothesis's `opened_at`. Check this with a query.
- After about 10 sessions: the first weekly run states at least one verdict, or explicit WAITING with counts.
- Phase 0 check 4 is repeated after the first week, with the morning durations before and after recorded.

---

## As built (2026-10-04)

Code: `agent/hypotheses.py`, migration v31 (`agent/migrations_v31.py`), `_run_hypothesis_lane` in `agent/loops/morning.py`, hooks in `evening.py`, `integrity.weekly_block`, `memory.build_morning_memory`, `delivery.py`, `run.py hypotheses`. Tests: `tests/test_hypotheses.py` (22, including a real morning run with both paired arms and a real offline evening run).

Where the build differs from the draft above:

1. **One tag per trade.** There is no `pick_hypotheses` table: `picks.hypothesis_id` holds the one idea a pick tests, written by the morning loop in `save_daily_plan` before any outcome exists. A test greps the codebase so that nothing else writes it and no `UPDATE` touches a frozen register column.
2. **The unit is the session.** Several tagged trades for one idea on one tape are averaged into one observation (the runner lane's "count events, not rows" lesson). The lane admits at most one test per idea per day. `target_n` / `max_n` count sessions with a valid test.
3. **A `hypothesis_fit` kill never counts for the idea.** Coverage may still trade that candidate as a setup, but it is never stamped with the idea it failed to test.
4. **Two human touches.** Each idea records `origin` (what the trader saw) and `belief` (1–5, at registration). The notebook shows "your gut so far": verdicts split by strong versus weak hunches. That is descriptive only and feeds no test.
5. **Abandoned and expired ideas join the next cohort that has a due look**, at p = 1, once (`cohort_counted`). A week with no due look does not spend them.
6. **Ideas the analyst writes down are registered even when not traded that day**, up to the daily cap. Ideas from the grader open on the next market session.
7. **Prompt versions:** analyst 13, risk 14, grader 8. With the lane off, every render differs from the previous fixtures only by its version line (the fixtures were recaptured).
8. **The morning email** labels each test trade with the idea it tests and prints any idea written down that morning with its refutation. The evening email ends with the notebook's counts. The weekly email carries the verdicts.

Shipping: the deploy carries migration v31, so warm the schema right after `vercel --prod` and before the 10:45 UTC morning cron (load the logged-in dashboard, or run `scripts\intraday_cron.ps1 -Once`).

## Revised 2026-10-04 (before any idea was judged): verdicts spend one budget across looks

The two-look schedule above (n = 8 and 16 at α/2, REFUTED on a mean ≤ 0 at the first look) is replaced by `agent/sequential.py` (specs/EDGE_ACCELERATION_SPEC_v1.md Part 1). Simulated, the old schedule supported a real +0.5R idea about one time in five, and it refuted a real +0.3R idea about a third of the time on its first eight sessions alone.

How verdicts work now:
- The first look comes at `target_n` (8) sessions with a valid test, and after that one look a week as tests accrue.
- Every look spends part of one one-sided budget of 0.025 over a 96-session horizon.
- An idea is SUPPORTED when it is significant after cohort BH, and REFUTED when its 80% upper bound rules out +0.10R or the budget is spent. Otherwise it keeps collecting.

The change was made before any idea had been looked at, so it moves no goalpost. Rows registered under the old schedule (max_n = 16) are judged over the new horizon.

Throughput was raised so ideas actually reach their looks: `hypothesis_lane.max_picks` 2 → 4, `max_open` 6 → 8, and the broker cap 22 → 24.
