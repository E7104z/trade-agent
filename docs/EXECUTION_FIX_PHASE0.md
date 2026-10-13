# Execution Fix — Phase 0 Verification Report

**Session date:** 2026-10-12
**Author:** Cline (agent)
**Status:** ⏳ Awaiting Jason sign-off (Gate 0) — no Phase 1 work may begin before this is signed.
**Constraint honored:** Read-only throughout. No file under `Trade Agent` was modified during verification. All DB access was SELECT-only; all Alpaca access was read-only (`GET /v2/orders/{id}`, `GET` snapshots).

## Purpose

Before touching execution/reporting code (Phase 1: **15:55 flatten reliability**), verify the specific incidents and metrics the fix is predicated on, against the live ledger and the Alpaca paper account. This document is the Gate 0 evidence pack.

## Method

- SELECT-only queries against the production SQLite ledger: `heartbeats`, `runs`, `picks`, `live_orders`, `rejections`, `deferred_placements`, `outcomes`.
- Read-only Alpaca paper API: IONS order JSON, BMY/XLE/DAL snapshots.
- Config inspection: `config.json`, `agent/config.py` (`flatten_at`, `EOD_FLATTEN_MINUTES`).
- Raw transcript (source of all rows below): `C:\Users\Quantum Lab\Desktop\_phase0_final.txt`.

---

## 0.1 — Heartbeats & runs: did the 15:55 flatten tick run?

**Answer:** Yes — the intraday monitor's 15:55 tick ran on 2026-10-12, was **not** skipped, and updated nothing.

`heartbeats`:

| name | last_at | detail |
|---|---|---|
| intraday_check | 2026-10-12 15:55:05.405550 | `{"monitor": {"skipped": false, "reason": null, "updated": 0}}` |
| morning_run | 2026-10-12 07:16:21.806047 | — |
| nightly | 2026-10-11 23:11:28.076487 | — |

Interpretation:
- `monitor.skipped = false` → the 15:55 tick executed inside market hours.
- `monitor.updated = 0` → it updated zero live orders at that tick. On 10-12 there was nothing open at 15:55 (IONS had already closed 13:32 via `overnight_carry`), so this is consistent — but it proves only that the tick ran and did nothing, not that the flatten closed anything.
- `heartbeats` stores only the **last** tick per name, so the 10-09 tick cannot be read directly; the 10-09 miss must be inferred from the IONS order lifecycle (see 0.2).

`runs` for 2026-10-12 (all `ok`):

| run_id | loop | started_at | finished_at |
|---|---|---|---|
| 130 | morning | 07:11:09 | 07:16:14 |
| 131 | evening | 16:37:30 | 16:38:35 |
| 132 | traders_log | 20:38:36 | 16:38:55 * |

\* run 132's `finished_at` precedes its `started_at` — a timestamp anomaly, noted, out of scope.

The intraday monitor is a cron (`/cron/intraday-check`), not a `runs` row — consistent with the `heartbeats.intraday_check` record above.

---

## 0.2 — IOVA/IONS lane ownership & order JSON

**Answer:** `IOVA` does not exist anywhere in the ledger (0 picks / 0 live_orders / 0 orders). The real symbol is **IONS**, and it belongs to the **runner lane** (`setup_type = dto_runner`), not the morning desk.

`picks` (IONS):

| pick_id | plan_date | ticker | direction | setup_type | entry_type | entry | stop | target | size_r | exploration_lane |
|---|---|---|---|---|---|---|---|---|---|---|
| 141 | 2026-10-09 | IONS | long | dto_runner | open | 45.05 | 44.631974 | 45.886051 | 0.25 | null |

`live_orders` (IONS):

| live_order_id | session_date | side | qty | order_class | entry_order_type | status | entry_fill | exit_reason | exit_fill | exit_fill_at | live_r | reconciled | exit_legs_armed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4317f738… | 2026-10-09 | buy | 59 | bracket | market | closed | 45.25 | overnight_carry | 44.982203 | 2026-10-12 13:32:55 | -0.6406 | true | false |

Order JSON (`GET /v2/orders/4317f738…`):
- parent: market buy 59 IONS, filled 45.25 @ 2026-10-09T14:31:24Z, `order_class=bracket`, `time_in_force=day`.
- leg 1 (take-profit): limit sell 59 @ 45.89, `time_in_force=day`, **canceled 2026-10-09T19:55:12Z** (filled_qty 0).
- leg 2 (stop): same structure, canceled at close.

Interpretation:
- IONS is a **runner-lane** pick. Its bracket legs are DAY orders that auto-cancel at 19:55 on entry day.
- Entered Friday 2026-10-09 14:31, **carried over the weekend**, closed Monday 2026-10-12 13:32 via `overnight_carry` at −0.64R live result.
- The runner lane exits at **15:00 ET** (`specs/RUNNER_TRADE_CONSTRUCTION_v1.md`) — a different rule from the morning desk's 15:55 flatten — and has since been **retired** (`specs/RUNNER_REMOVAL_v1.md`; `grading.py` v6 note). → Phase 1's 15:55 flatten scope is the **morning lane**; IONS/runner carry is a separate (retired) path.

---

## 0.4 — BMY rejection / deferred placement

**Answer:** BMY was **rejected by the morning risk stage** (reward:risk below threshold) **and** placed anyway as a deferred limit order via the "second-best floor pick" path. It filled and stopped out.

`rejections` (BMY):

| rejection_id | plan_date | setup_type | reason | eod_outcome | forgone_r | rejection_category | planned_rr | source_stage | primary_kill_check |
|---|---|---|---|---|---|---|---|---|---|
| 272 | 2026-10-12 | catalyst_momentum | "R:R 1.39 (entry $61.80, stop $59.50, target $65.00)… reward geometry fails threshold. Second-best floor pick candidate." | would_have_lost | -0.712 | rr_below_control | 1.3913 | risk_rejected | rr |

`deferred_placements` (BMY):

| plan_date | pick_id | ticker | status | detail | attempted_at |
|---|---|---|---|---|---|
| 2026-10-12 | 150 | BMY | placed | "limit; placed at 09:45:04 ET, last 59.84" | 2026-10-12 13:45:05 |

`outcomes` (BMY):

| pick_id | ticker | triggered | entry_fill | exit_price | exit_reason | r_multiple | r_planned | live_r | atr14 | stop_dist_atr | target_dist_atr | gap_atr |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 150 | BMY | true | 59.8799 | 59.4703 | stop | -0.712 | -1.013 | -0.1648 | 1.3989 | 1.64 | 2.29 | 0.71 |

Interpretation:
- The `rejections` row is the **risk** decision (rr_below_control); the `deferred_placements` row is the **separate** "second-best floor pick" mechanism that still sent the limit at 09:45 (`live_monitor.place_deferred_orders`). This is likely by design (reject the main-desk R:R but still catch a pullback), but the two records make it read like a rejected trade was placed — **confirm with Jason**.
- **The R-reporting split (target of the reporting fix), one trade, three numbers:**
  - `live_r = -0.1648` → actual fill 59.88 → live exit ≈ 59.50, over planned risk 2.30 (`live_monitor._live_r`).
  - `r_planned = -1.013` → scored from the **planned** entry 61.80 ("worse of plan vs fill", `grading.py:61-67`), over planned risk 2.30.
  - `r_multiple = -0.712` → diagnostic, denominator floored at 25% of plan (0.575).
  - `forgone_r = -0.712` → shadow ledger uses `r_multiple` (`classify_shadow`, `grading.py:967`).

---

## 0.7 — BMY/XLE/DAL ATR14 & snapshot verification

**Answer:** BMY's stored ATR14 and stop distance match an independent recompute. XLE/DAL have no outcome on 10-12 (reference only).

ATR14 (stored `outcomes.atr14` vs independent recompute from minute bars, current day's partial bars dropped):

| ticker | stored atr14 | recomputed atr14 | last_close | atr_pct |
|---|---|---|---|---|
| BMY | 1.3989 | 1.39886 | 59.12 | 2.366% |
| XLE | — | 1.29936 | 65.08 | 1.997% |
| DAL | — | 2.40734 | 82.17 | 2.930% |

BMY stop distance:
- stored `stop_dist_atr = 1.64`
- recompute |61.80 − 59.50| / 1.3989 = **1.6442** → matches stored 1.64 (rounded). ✅

Snapshots (Alpaca, read-only):

| ticker | latest_trade | latest_quote_ap |
|---|---|---|
| BMY | 57.58 | 57.76 |
| XLE | 66.01 | 66.07 |
| DAL | 80.0225 | 80.27 |

---

## Consolidated findings (drives Phase 1)

1. **The 15:55 flatten tick runs** (`intraday_check` heartbeat 10-12 15:55:05, `skipped=false`), but on 10-12 it updated 0 orders. The 10-09 miss is not readable from `heartbeats` (only the last tick is stored); it is evidenced by IONS's `overnight_carry` exit.
2. **IONS overnight carry is a runner-lane artifact** (`dto_runner`), not a morning-desk 15:55 flatten miss. Scope Phase 1 flatten work to the **morning lane**; the runner lane is retired.
3. **BMY rejection + deferred placement** are two separate mechanisms (risk reject vs second-best floor pick). Confirm with Jason whether the dual-record behavior is intended.
4. **R-reporting split is real and quantified**: BMY = live_r −0.16R vs r_planned −1.01R vs r_multiple/forgone −0.71R. The grader's `r_planned` ("worse of plan vs fill") inflates the loss on a favorably-filled stop-out.
5. **ATR14 / stop-distance / snapshots verified** — no data-quality issue there.

---

## Gate 0 sign-off

- [x] Evidence collected read-only (SELECT-only + read-only Alpaca GET).
- [x] No production code modified.
- [ ] Findings reviewed by Jason.
- [ ] Approval to begin Phase 1 (15:55 flatten reliability).

**Jason sign-off:** ______________________   **Date:** ______________
