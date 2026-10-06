# VERIFICATION PRACTICE v1: rehearse before shipping, audit before saving, fixes only for ten sessions

**Status:** built 2026-10-06. Jason asked why every review of the results has found errors, then said: "make those changes and send to production."
**Goals served:** 1 and 2 (clean data every session), 4 (the system checks itself daily).
**Writer isolation:** unchanged.

---

## Why every review found errors

- **Something new shipped almost every session.** The schema went from v28 to v35 between 10/02 and 10/05, and 10/05 was the first day all of it ran together.
- **The test suite cannot see where the errors live.** It runs on fake model replies, fake prices and a throwaway database. The code that ran on 10/05 passed all 1,528 tests, and the review still found 12 problems:
  - 3 came from real model latency against the 300s limit;
  - 5 were the grader, an LLM, misreading numbers;
  - 2 came from real opening prints;
  - 1 was display;
  - 1 was two correct features colliding.
- **Production was never read before shipping.** The owner's email was the test.

## Part 1: rehearse before shipping (`agent/rehearsal.py`, `run.py rehearse`)

`run.py rehearse` runs the changed code end to end on real market data and real model calls (about $2-3), in a scratch copy of the local ledger. That copy carries real lessons, playbook and history.

- **Morning:** today's session, through the hosted path: analyst half, checkpoint, hand-off, risk half. Each half is timed against the 300s budget it gets in production.
- **Evening:** the latest past session in the local ledger, graded again: simulator, grader, audit, trader's log, self-check. It is timed the same way.
- **Pass criteria:**
  - Each invocation finishes under 270s (the self-check's margin).
  - The plan is saved, and the checkpoint and run lock both end complete.
  - Arm B records its plan.
  - The evening run ends `ok` or `degraded` with the grader complete.
- **Reports:** `reports/rehearsals/<commit>-<stamp>/report.md` (with the full log), and a summary at `reports/rehearsals/<commit>.json`.
- **Isolation:**
  - `DATABASE_URL` is removed for the run, because this PC's user-level value is production.
  - Email and live paper orders are off.
  - The hand-off is recorded, not sent.

`scripts/deploy.ps1` ships only if a passing rehearsal covers HEAD's runtime files (`agent/`, `api/`, `prompts/`, `config.json`, `vercel.json`, `requirements.txt`). It then runs the suite with production unreachable, pushes, runs `vercel --prod` (retried), and warms the schema with one intraday tick. A docs-only commit needs no new rehearsal.

## Part 2: audit the grader before anything is saved (`agent/review_audit.py`)

The grounding layer checks that a quoted number exists in the input. It does not check that the sentence around the number agrees with the rest of the input. The audit runs after the grader and before grades, lessons or ideas are stored. Each check names the 10/05 claim that prompted it:

| check | fires when | 10/05 example |
|---|---|---|
| `volume_contradiction` | A claim that participation stopped early is made about a ticker that traded at least one average day of volume after 10:00. Or a "weak volume" claim about a day at or above its 20-day average. | ALEC "one-and-done", 2.49x → 6.88x |
| `scratch_as_result` | A scratch (\|R\| ≤ 0.05) is called a winner or a loser. | ALEC +0.01R |
| `test_lane_penalized` | A test-lane pick is graded C-F "for having been taken". | NVDA (coverage) |
| `forgone_conclusion` | One session's forgone R is read as proof about the filters. | "-0.14R confirms filters are working" |

**What happens to a finding:**
- A lesson or a new idea with one is **dropped**: it never becomes memory.
- A pick review or the plan notes keep the grader's words (grades are never rewritten), with an `[AUDIT: ...]` note appended.
- Findings go to `runs.stages.audit`, to an AUDIT block in the evening email, and to a new self-check, "Grader audit".

The rules fire only on a claim the numbers plainly contradict, never on a judgment call.

## Part 3: fixes only for ten sessions

From 2026-10-06 through 2026-10-19 (10 sessions; NYSE is open on Columbus Day, 10/12), changes are limited to:
- fixing what exists;
- verification: rehearsal, audit, self-checks.

During that window:
- no new lanes, data families or prompt sections;
- no new LLM roles;
- no new config treatments.

**Why:** an error contaminates that session's data, and every new feature has produced errors on its first live day. Ten clean sessions are worth more toward a validated edge than another lane. The window ends early only if Jason asks for a feature.

**Acceptance:** at least 8 of the 10 sessions close with "Grader audit" at `ok`, and "Morning time budget" shows no invocation at or above 270s.
