# EVIDENCE BACKLOG SPEC v1 — work the n<8 list every day

**Status:** implemented 2026-09-24 (`a63fba5`), live in production; production acceptance pending. Proposed 2026-09-23 ([SELF_ASSESSMENT_2026-09-23.md](SELF_ASSESSMENT_2026-09-23.md) P7, G3.1–G3.4, G4.4)
**Goals served:** 3 (primary), 4, 2
**Scope:** new `agent/backlog.py`; `agent/loops/morning.py`, `agent/loops/evening.py`, `agent/memory.py`, `prompts/morning_analyst.md`, `prompts/morning_risk.md`, `config.json`, `agent/integrity.py`, `agent/delivery.py`, `api/index.py`; the next free migration; tests.
**Depends on:** S2 (QUARANTINED rules with predicates) for the rule items; S4 (coverage) for Phase 4. Phases 1–3 can ship on open questions and cells alone.
**Writer isolation:** unchanged. The backlog is a ledger table. The morning loop *reads* it; nothing here writes `playbook.md`.

---

## 0. The problem

The directive: "Treat under-n<8 setups as the top backlog priority, not background noise." Today the system reports under-powered cells and does nothing about them.

- **The under-powered list reaches one reader, once a week.** `integrity._underpowered_md` (`integrity.py:358-380`) lists only cells with MDE > 1.0R, top 25, inside the weekly miner prompt. The morning analyst's context (`agent/memory.py`) contains nothing from the research layer: no under-powered cells, no quarantined rules, no open-question needs. The loop that chooses what to trade never learns what evidence is missing.
- **The one deliberate evidence purchase is tiny.** `_add_exploration_pick` (`morning.py:721-803`) fires only when the plan has **zero** picks (`:729`), only for a candidate whose sole kill-check was R:R, and only in the 1.8–2.0 band. That is at most one probe, only on otherwise-empty days, along one axis.
- **The risk manager is paid to not trade.**
  - "You are rewarded for what you reject and for the days you send the desk home flat" (`morning_risk.md:4`).
  - "If fewer than 2 candidates survive honest review, strongly consider no_trade" (`:79`). That is a conjunction rule with no evidence behind it.
  - The local ledger (not production) shows 5 of 6 morning plans as no-trade. The production rate is Phase 0's first check.
- **Open theses advance only by accident.** Open-question counters (Q1–Q14) move when the desk happens to trade the setup. Nothing aims collection at the question.
- **Disconfirming evidence is left on the table.** The shadow ledger prices every rejected candidate with the same simulator, but none of it reaches a quarantined rule. S2 counts taken trades only for the morning lane, which is correct for graduation, but the counterfactual evidence is never shown at all.

---

## Phase 0 — production verification (hard blocker; a self-check, not a sign-off)

Data access means an authenticated `/api/table/...` session or a local `DATABASE_URL`. If neither is available, record Phase 0 as blocked and stop. Never search the machine for credentials.

1. Pull `/api/table/daily_plans?since=2026-08-19&limit=100`. Report:
   - the no-trade rate, excluding `session_degraded` days;
   - `no_trade_category` counts;
   - `exploration_pick_count` per day.
2. Pull `/api/table/rejections?since=2026-08-19&limit=1000`. Report:
   - `primary_kill_check` counts;
   - how many were `rr`-only and inside the 1.8–2.0 band;
   - how many of those the exploration lane actually admitted.
3. Shadow cost by kill-check category over the same window. Is the risk manager's typical kill net-costly or net-saving? This decides how urgent Phase 3 is. **If kills are clearly net-saving (ex-tail), say so, and ship Phase 3 as a wording fix only.**
4. The backlog baseline:
   - cells with 1 ≤ n < 8 per lane, from the latest `research_looks` session;
   - open-question progress against the registry thresholds (`agent/open_questions.py`);
   - for each, the trailing 10-session observation rate.

---

## Phase 1 — a deterministic backlog, computed nightly

1. `agent/backlog.py::compute(con, d)` runs at the end of the evening loop's deterministic stage (S4 ordering), with no LLM involved. It writes `evidence_backlog(session_date, item_type, item_id, lane, n, target, rate_10s, eta_sessions, stalled, what_counts)`.

   | item_type | source | n / target | what_counts |
   |---|---|---|---|
   | `rule` | QUARANTINED `research_rules` (S2) | `forward_n` / `forward_n_required` | its predicate, rendered |
   | `question` | `open_question_progress` vs the `open_questions` registry | progress / threshold | the registry's observation definition |
   | `cell` | every 1-D cell of the mined dimensions with 1 ≤ n < 8 | n / 8 | `dimension=level`, lane |

2. **Integrity rule, enforced by a test:** the backlog stores **counts, targets and rates only**. It never computes or stores a mean, win rate, p-value or effect sign for any item. A daily table of effect sizes would be an unregistered look taken every day. A table of counts is an allocation of collection effort and cannot bias a test.
3. **Ordering** is deterministic and uses no new tuning constants:
   1. rules, fewest forward observations remaining first;
   2. questions, largest remaining fraction of threshold first;
   3. cells, highest n first (closest to the existing n = 8 floor).

   `stalled` = zero increments in the trailing 10 sessions.
4. Add `evidence_backlog` to `_QUERYABLE_TABLES`.

Tests:
- The backlog row set has no numeric column except counts, targets, rates and the ETA; assert on the schema.
- The ordering is stable.
- An item disappears when it reaches its target.

## Phase 2 — the morning desk sees the backlog

1. `memory.build_morning_memory` adds a `## DATA-STARVED (system backlog — top 8)` block: item, `what_counts`, n / target, stalled flag. Counts only, as stored.
2. `morning_analyst.md` (prompt-version bump):
   - "A candidate that would produce an observation for a DATA-STARVED item may be proposed as a labeled probe: set `probe_for` to the item id and say which observation it produces."
   - "A probe must still be a real setup with a real catalyst and a real stop. The backlog tells you where to look, never what to conclude."
3. **The exploration lane gains a second type, `backlog_probe`** (config `exploration_lane.backlog_probe`):
   - a candidate tagged `probe_for` a top-8 item, rejected by the risk manager with `primary_kill_check` in `{rr, conviction}` only, is admitted at `size_r` = 0.25R;
   - it is tagged `exploration_lane: "backlog_probe"` and `probe_for: <id>`;
   - **every other kill-check stays binding**: staleness, grounding, event_risk, chase, correlation, regime_fit, stop_quality, entry_type, crowding.
4. `exploration_lane.allow_with_picks: true` lets exploration run on days that already have picks. Today it only runs on empty days.
5. The cap is `exploration_lane.max_picks: 2` per day in total. That is 0.5R, inside the existing 3R open-risk cap. **It is a budget choice, not an edge claim.** It is recorded in Sizing Policy as `[UNVALIDATED prior]` and flagged under T3.

Tests:
- A probe killed on `staleness` is never admitted.
- A probe killed on `rr` for a top-8 item is admitted at 0.25R with both tags.
- The cap holds across both exploration types.
- The existing `rr_below_control` behaviour is unchanged when `backlog_probe` is off.

## Phase 3 — stop paying the risk manager to stand down

`morning_risk.md` (prompt-version bump):
1. Replace "You are rewarded for what you reject and for the days you send the desk home flat" with: "You are judged on whether your kills were right. Every rejection is priced by the shadow ledger with the same simulator as the taken trades. A no-trade day is correct only when the shadow ledger agrees."
2. Delete "If fewer than 2 candidates survive honest review, strongly consider no_trade". Each candidate is decided on its own kill-checks.
3. **R:R ≥ 2.0 stays exactly as written.** It is loosened only by the pre-registered Q13/Q14 test (`open_questions.py:117-123`), never from shadow data or from this spec. The playbook's own rule is "never widen a filter from shadow data alone".

Test: a prompt snapshot test pins the new wording and asserts the R:R line is unchanged.

## Phase 4 — the daily diagnostics digest (Goal 4)

1. At the end of every evening run, write one `daily_diagnostics(session_date, payload_json)` row containing:
   - health stages (`health.loop_health`);
   - the coverage summary (S4);
   - forward observations recorded today (S2);
   - backlog items advanced, and items newly stalled;
   - grader rejections (S5);
   - block-severity violations in the current playbook (S3).
2. The digest is rendered into the evening email, and as a dashboard panel.
3. **Same integrity rule as Phase 1:** no effect sizes or significance. It says what was collected and what broke, never what worked.

## Phase 5 — counterfactual evidence on every open thesis (confirming and disconfirming)

1. `research.observe_session` (S2) also records observations from **shadow-ledger rejections** and **population-monkey rows** that match a QUARANTINED morning-lane rule's predicate, with `source = 'shadow' | 'population'`.
2. These rows **never count toward graduation**. `forward_n` stays taken-trades-only for the morning lane, as S2 defines. They are reported beside each rule in `provenance_md` as "counterfactual forward: n, sign", suppressed below n = 8 like everything else.
3. That gives every open thesis a disconfirmation channel at no trading cost. The labelling keeps simulated, not-taken evidence out of the validation decision.

Test: a shadow row matching a quarantined predicate increments the counterfactual count and leaves `forward_n` unchanged.

---

## Trade-off T3 (flagged, not silently resolved)

Phases 2 and 3 buy data with paper exposure. They also make the desk's aggregate record noisier, because some trades are probes. The resolution:
- every probe is tagged;
- `exploration_lane` becomes a mined dimension (S7), so its effect is measured rather than pooled;
- sizing stays at the minimum;
- the account is paper only.

No gate that protects the validity of a trade (grounding, fill validity, event risk) is relaxed. Only the risk manager's *preference* against trading is removed.

## Acceptance (production evidence)

- `/api/table/evidence_backlog` has rows every session.
- `daily_plans` shows exploration picks on days that also have regular picks.
- At least one backlog item's n rises in the first week because of a `probe_for` pick; cite the pick id.
- The no-trade rate from Phase 0 check 1 is re-measured 20 sessions after shipping; the before and after are recorded in the self-assessment under G3.3.
