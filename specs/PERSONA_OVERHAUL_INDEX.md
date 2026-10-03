# PERSONA OVERHAUL — INDEX AND ORDER OF WORK

Status: ready for Cline
Files: `PERSONA_CHARACTER_SHEET_SPEC_v1.md`, `TRADERS_LOG_SPEC_v1.md`
Drop both into `specs/` in the repo. Do not rewrite them; if scope grows, write an addendum file.

## What this is

The agent should read as one competitive, hyperfocused trader whose aggression points at his own unproven status ("earned hunger"), not at the market. Two deliverables:

1. A shared **character sheet** across five prompts, behind a config flag, measured as a treatment.
2. A **daily trader's log** for the human: first person, template-bound, numbers injected by reference, and firewalled out of every learning path.

## What does not change

The integrity layer is untouched: n<8 suppression, FDR correction, writer isolation on `playbook.md`, quarantine/forward validation, the fill-validity gate, and "deterministic code decides what counts as a finding". Nothing in this work may add a new channel by which prose, narrative or an unregistered effect size reaches a pick-making or rule-writing prompt.

## Order of work (hard)

0. **The runner work (migration v28, arms A/B/C, regrade) ships first, on the owner's explicit go-ahead.** Do not commit, push, deploy, or run `regrade-runner` on your own. If v28 is not live when Phase 0 of either spec runs, stop and report.
1. `PERSONA_CHARACTER_SHEET_SPEC_v1.md`, all phases, in order.
2. `TRADERS_LOG_SPEC_v1.md`, all phases, in order. Its Phase 0 requires v28 live and the persona spec's schema stamp present.

## Standing rules for every phase (apply to both specs)

- **Additive only.** No existing function is rewritten, no column changes meaning, no historical row is recomputed. With every new flag at its default, behavior and rendered prompts are byte-identical to today. A test must prove it.
- **Phase 0 is a hard blocker.** Verify against the real repo and, where the spec says so, production (`/api/table/runs?limit=40`, `/api/status`). Write the findings to the phase report before any code. If a finding contradicts the spec, stop and report; do not improvise around it.
- **The existing suite stays green.** Baseline was 1,297 passing at the time of writing; re-measure at Phase 0 and record the number. Tests that pin exact prompt strings (e.g. `COUNTS toward a threshold, not findings`, `DATA-STARVED`, `STALLED`, `no validated runner rules yet`, `The desk trades every session.`) must pass unmodified. If one must change, stop and ask.
- **Measurement apparatus must not break the experiment.** Every new entry point is exception-guarded and degrades to a valid, reduced result. A failure in anything built here must never block morning, runner, evening or weekly loops.
- **Real data only.** No hardcoded debug data, no mock numbers in production paths. Mocks live in `mocks/` and tests only.
- **Do not inherit numbers.** No threshold, win rate or constant in any new prompt text or code is carried from DTO or invented here. Where a threshold seems needed, the spec says it is registered by the owner, not chosen by you.
- **Phase gates.** At the end of each phase, stop and post a report: what changed (files, line counts), test counts before/after, and the gate checks. Do not start the next phase until the report is posted. Do not skip the gate because the phase seemed trivial.
- **Do not enable anything in production.** Every new flag ships `false`. Enabling is the final phase of each spec and requires the owner's explicit approval in the conversation.

## Things the owner has already decided (do not reopen)

- The winner/loser contrast does **not** go into any morning prompt. Morning agents see counts and pre-registered dimensions only. Descriptive winner/loser comparisons live in the evening email.
- The trader's log is **write-only**: the agent never reads it back. A belief reaches the agent only by being registered as an open question or `probe_for` item through the existing counts-only backlog (future addendum, out of scope here).
- The persona refines the existing analyst persona ("Your mission is singular: win"); it does not stack a second one on top.
- Five prompts, not four: `morning_analyst`, `morning_risk`, `evening_grader`, `weekly_miner`, `runner_selector`.
