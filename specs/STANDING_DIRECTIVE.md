# Trade Agent — Standing Directive

**Type:** Standing mandate (not a sprint-scoped spec — persists across future specs)
**Audience:** Cline, operating directly against the Trade Agent codebase
**Execution mode:** Autonomous. This directive is pre-authorized — do not pause to ask Jason for permission before acting on any of the five goals below. The only checkpoints that apply are the ones built into the system itself (Phase 0 production verification, the integrity layer, writer isolation) — those are self-checks, not requests for Jason's sign-off, and they stay hard blockers regardless of how much autonomy this directive grants.

---

## Before doing anything else

Re-read, from the actual codebase and recent run history — not from memory of past sessions:
- The integrity layer: small-sample suppression (n<8), rule decay after 30 sessions without reconfirmation, FDR correction, confidence tiers.
- The three-loop architecture: morning analyst/risk manager, evening grader, weekly miner with writer isolation on `playbook.md`.
- Current diagnostic state via `/api/table/runs?limit=40` and `/api/status`.

This re-read *is* Phase 0 for the self-assessment below — it's a verification step against production data, not a wait-for-Jason step.

---

## The five goals (permanent — every future spec is in service of these)

1. **Trade, learn, refine, update toward edge.** Every trading day is a learning event, not just a P&L event. Convert live market exposure into validated, re-derived edge — never inherited constants, never unvalidated hunches.

2. **Pull data on trades, tickers, and strategies every day.** No-trade days and outages are not acceptable substitutes for real data collection. Distinguish genuine market stand-downs from pipeline failures (simultaneous nulls across all tickers = plumbing, not signal) and fix the plumbing aggressively. The system is data-volume-bottlenecked right now — every lost day is a lost learning opportunity.

3. **Be relentless about seeking relevant data.** Don't wait for data to arrive — actively look for it. Widen the search for confirming and disconfirming signal on every open thesis. Treat under-n<8 setups as the top backlog priority, not background noise.

4. **Update itself every day.** The learning loop runs daily, not just weekly. Respect existing isolation rules — only the weekly miner writes `playbook.md`. "Update itself" means tightening the diagnostic, grading, and data-collection layers daily so the miner has better raw material weekly — it does *not* mean bypassing the write-isolation guardrail.

5. **Search exhaustively for patterns, similarities, and common attributes of winners — long or short.** Cross-reference winning trades across setups, tickers, timeframes, and market regimes for shared *structural* attributes, not just outcome correlation. Always run through the integrity layer — no false edge from small samples or multiple-comparison artifacts.

---

## Constraints that apply to all five goals

- Never inherit numbers without re-deriving them from this system's own corpus (see: DTO constants were explicitly not ported).
- Fill-validity gates before grading — a structurally invalid trade must never be graded or mined as if it were a real signal.
- Automation enforces discovered rules and moves judgment earlier (kill switches, sizing limits, exposure caps) — it never removes judgment entirely.
- Phase 0 verification against production data (`runs` table, `/api/status`) is a hard blocker before any code ships, on every change connected to these goals.

---

## Deliverable: self-assessment against the five goals

Using what you actually know of the current codebase and recent run history:

1. **Per goal, state current status:** fully met / partially met / not met — with the specific evidence (run IDs, table rows, code paths) behind the call.
2. **Per gap, propose the specific next change** — spec-ready, phased, with an explicit Phase 0 production-verification step, in the same Cline-ready `.md` format this directive uses.
3. **Flag anything currently working against these goals** as a priority fix — e.g. gates producing too many no-trade days, diagnostic blind spots, confabulation risk in the grader.
4. **Do not propose changes that violate the integrity layer or the writer-isolation rule** to close a gap faster.

Treat this deliverable as the baseline "done" definition: every future task gets evaluated against whether it moves the system closer to all five goals, and any task that would trade one goal off against another gets flagged explicitly rather than silently resolved.

The first assessment against this directive is [SELF_ASSESSMENT_2026-09-23.md](SELF_ASSESSMENT_2026-09-23.md).
