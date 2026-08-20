# Weekly Miner — Spec Changes for Playbook v3

**File:** `weekly_loop.py`
**Model:** Claude Opus
**Trigger:** Sunday, manual or scheduled
**Purpose:** The miner is the ONLY writer of `playbook.md`. It reads the full
ledger + all event tables the evening grader emitted during the week, recomputes
aggregates, applies decay, and rewrites the playbook, bumping the version.

This spec is ADDITIVE to existing mining behavior (lesson promotion, rule
proposal). Below are only the v3 changes.

---

## 1. Apply decay before anything else

For every rule, find its latest `last_confirmed` from the week's
`rule_confirmation` events (or carry forward the prior value). Compute sessions
elapsed since. If `>= 30 sessions`, force the rule's confidence to UNVALIDATED
and annotate `[auto-demoted: stale N sessions]`. Decay runs FIRST so a rule can't
be simultaneously "confirmed this week" and "stale."

A rule auto-demoted twice (tracked via a demotion counter) is moved to the
**Retired Rules** section with the reason `repeatedly stale` and the date. Retired
rules are not re-proposed unless new confirming observations post-date the
retirement date.

## 2. Recompute the calibration table

Aggregate all `calibration` rows by stated conviction. Fill the v3 Calibration
table: n, realized WR, realized avgR per conviction bucket. Then compute the
verdict:
- If total graded picks < 20: verdict = `not yet computable`, sizing-by-conviction
  stays active on its priors.
- If n >= 20 AND conviction 4-5 avgR does NOT exceed conviction 2-3 avgR by a
  meaningful margin (define: >= 0.3R): verdict = `conviction uncalibrated —
  suspend sizing-by-conviction`, and the miner edits the Sizing Policy to flatten
  size across convictions until recalibrated.
- Else: verdict = `conviction calibrated`, note the spread.

Report the table even when not yet actionable — visibility is the point.

## 3. Summarize the shadow ledger and flag expensive filters

From scored rejection rows over the trailing 20 sessions:
- Fill the Shadow Ledger Summary counters (total rejected, would-have-won,
  would-have-lost, net forgone R).
- Group `forgone_r` by rejection `reason`. Any reason whose cumulative forgone R
  is POSITIVE and exceeds the captured R of the corresponding taken rule over the
  same window gets listed under "Filters flagged for review."
- A flagged filter does NOT get auto-loosened. The miner instead adds/updates an
  Open Question to test loosening it deliberately, one variable at a time. Never
  widen a filter from shadow data alone — shadow outcomes assume perfect fills the
  taken book would not have gotten.

## 4. Report progress on every open question

For each open question, sum the week's `open_question_progress` increments into
the running total and rewrite the `Progress: X/threshold` line. If a question hits
threshold, the miner runs the actual comparison and writes a one-line finding,
then either resolves the question (promotes a rule) or extends it with a new
threshold and a stated reason. No open question may be silently dropped.

## 5. WR/avgR suppression is enforced at write time

When rewriting setup rules, the miner must OMIT WR and avgR for any rule with
n < 8 and substitute `WR/avgR suppressed, see ledger`. This is a hard formatting
rule, not a suggestion — the whole point is to deny the morning agent a small-n
number to anchor on.

## 6. Version + changelog discipline

Bump `<!-- version: N -->`. Write a changelog listing every rule whose confidence,
last_confirmed, or section changed, with the triggering evidence (n and event
counts). Rules with no qualifying new evidence are stated as unchanged. The miner
never edits a rule "on judgment" without a ledger event behind it — same standard
as every other experiment: one variable, evidence first, pre-registered where
possible.

## 7. Writer isolation

`weekly_loop.py` is the sole writer of `playbook.md`. The morning and evening
loops read it but never write it. Commit the rewritten playbook to git as part of
the run (the file is version-tracked; the ledger DB is gitignored) so the playbook's
evolution is a readable diff history over time.
