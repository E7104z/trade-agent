# Execution Fix — Phase 0 Verification Report

**Session date:** 2026-10-12
**Author:** Claude Code (agent)
**Status:** ⏳ Awaiting Jason sign-off (Gate 0); the Addendum (2026-10-13) supersedes the first-pass text below wherever they differ (0.1, 0.2, Phase 3) — no Phase 1 work may begin before this is signed.
**Constraint honored:** Read-only throughout. No file under `Trade Agent` was modified during verification. All DB access was SELECT-only; all Alpaca access was read-only (`GET /v2/orders/{id}`, `GET` snapshots).

## Purpose

Before touching execution/reporting code (Phase 1: **15:55 flatten reliability**), verify the specific incidents and metrics the fix is predicated on, against the live ledger and the Alpaca paper account. This document is the Gate 0 evidence pack.

## Method

- SELECT-only queries against the ledger (DSN not recorded by the first pass; production is Postgres/Neon, see Addendum 0.7): `heartbeats`, `runs`, `picks`, `live_orders`, `rejections`, `deferred_placements`, `outcomes`.
- Read-only Alpaca paper API: IONS order JSON, BMY/XLE/DAL snapshots.
- Config inspection: `config.json`, `agent/config.py` (`flatten_at`, `EOD_FLATTEN_MINUTES`).
- Raw transcript (source of all rows below): `C:\Users\Quantum Lab\Desktop\_phase0_final.txt`.

---

## 0.1 — Heartbeats & runs: did the 15:55 flatten tick run?

**Answer:** Yes — the intraday monitor's 15:55 tick ran on 2026-10-12, was **not** skipped, and updated nothing. (The 10-09 tick also ran; see Addendum 0.1.)

`heartbeats`:

| name | last_at | detail |
|---|---|---|
| intraday_check | 2026-10-12 15:55:05.405550 | `{"monitor": {"skipped": false, "reason": null, "updated": 0}}` |
| morning_run | 2026-10-12 07:16:21.806047 | — |
| nightly | 2026-10-11 23:11:28.076487 | — |

Interpretation:
- `monitor.skipped = false` → the 15:55 tick executed inside market hours.
- `monitor.updated = 0` → it updated zero live orders at that tick. On 10-12 there was nothing open at 15:55 (IONS had already closed 13:32 via `overnight_carry`), so this is consistent — but it proves only that the tick ran and did nothing, not that the flatten closed anything.
- `heartbeats` stores only the **last** tick per name, so the 10-09 tick cannot be read from it; the local `intraday.log` does show it (Addendum 0.1).

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

**Answer:** `IOVA` is in no ledger table (0 picks / 0 live_orders) and is **unresolved** — it may be an unledgered broker position (Addendum 0.2). **IONS** is a separate, ledgered **runner-lane** pick (`setup_type = dto_runner`); do not assume IOVA is a typo for it.

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
- The runner lane exits at **15:00 ET** (`specs/RUNNER_TRADE_CONSTRUCTION_v1.md`) — a different rule from the morning desk's 15:55 flatten — and has since been **retired** (`specs/RUNNER_REMOVAL_v1.md`; `grading.py` v6 note). However, IONS's legs were canceled at 15:55:12 by the flatten path and the close was then refused (Addendum 0.1), so IONS is evidence of a flatten failure, not just a runner-lane artifact. Phase 1 scope (morning lane only?) remains Jason's call.

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

1. **The 15:55 flatten tick runs** (`intraday_check` heartbeat 10-12 15:55:05, `skipped=false`), but on 10-12 it updated 0 orders. The 10-09 miss is not readable from `heartbeats` (only the last tick is stored); it is evidenced by the local log plus IONS's `overnight_carry` exit: the tick ran and the close was refused (class b).
2. **IONS overnight carry was a refused close at 15:55** (runner-lane position, but the same flatten code path the morning lane uses). IOVA is unresolved and may be an orphan broker position.
3. **BMY rejection + deferred placement** are two separate mechanisms (risk reject vs second-best floor pick). Confirm with Jason whether the dual-record behavior is intended.
4. **R-reporting split is real and quantified**: BMY = live_r −0.16R vs r_planned −1.01R vs r_multiple/forgone −0.71R. `r_planned` ("worse of plan vs fill") scores BMY further from `live_r`, not closer; **Phase 3 is on hold** (Addendum 0.5).
5. **ATR14 / stop-distance / snapshots verified** — no data-quality issue there.

---

## Addendum (Claude, 2026-10-13) — corrections and the items the first pass skipped

### 0.1 corrected — the local log classifies both days
`%USERPROFILE%\.trade-agent\intraday.log` (this machine), raw lines:

- 2026-10-09: ticks at 15:40:02, 15:45:01, 15:50:01, **15:55:02** all `200`, each `"monitor":{"skipped":false,"updated":0}`. **No tick after 15:55**, and none at 16:00 (the script's `EndEt` stops there).
- 2026-10-12: ticks through **15:55:02**, `200`, `updated:0`, then `[16:00:02 ET] past window - done for today.`

IONS evidence on 10-09: its bracket legs were canceled at 15:55:12 ET (19:55:12Z), ten seconds after the 15:55:02 tick. That is `_cancel_working_legs`, not the DAY expiry (that would be 16:00 ET). `_close_position_after_legs` then ran, the close was refused through all five attempts, `_record_close` returned False, so `updated` stayed 0 and the position was left open with its protective legs gone.

**10-09 = class (b): the tick ran, the close was refused.** Not (a). It is the same race `_CLOSE_RETRY_WAITS` was added for on 10-01, and 5 seconds of retries lost it again. With only one aligned tick at 15:55, nothing retried. This supports Phase 1d (15:57 and 15:59 retries). It does not support the spec's "no tick reached the server".
**10-12:** ticks ran; nothing the ledger knew about was open (IONS had closed at 13:32).

### 0.2 / 0.3 — IOVA is unresolved, and may be an orphan
IOVA is in no ledger table (0 picks, 0 live_orders). Either it is not a position at all, or it is exactly the orphan Phase 1b/1c is for (a broker position with no ledger row). `carry_cleanup` reads `live_orders` only, so **it will not close IOVA at 06:45 if it exists**. I could not read the broker (the auto-mode classifier denied the read). **Jason must check Alpaca positions for IOVA now and close it by hand if present.** IONS, by contrast, was a ledger row and was closed by `carry_cleanup` on 10-12.

### 0.5 — `classify_shadow` and the BMY arithmetic
`classify_shadow` (agent/grading.py:950-972) takes `r = outcome.get("r_multiple")`. Confirmed: `forgone_r` comes from `r_multiple`, not `r_planned`.
BMY: fill 59.8799, exit 59.4703, stop 59.50, planned risk 2.30. Denominator = max(|59.8799 − 59.50| = 0.380, 0.25 × 2.30 = 0.575) = 0.575. (59.4703 − 59.8799) / 0.575 = **−0.712**. Reproduces the stored value.

**The spec's Phase 3 premise is wrong for this trade.** The floor *deflated* r_multiple here (denominator 0.575 rather than 0.380). `r_planned` is scored from the *worse of planned entry (61.80) and fill*: (59.4703 − 61.80) / 2.30 = **−1.013**. So switching `forgone_r` to `r_planned` moves BMY from −0.71R to **−1.01R**, further from the "≈ −0.18R" the spec expects. The −0.16R figure is `live_r` (real fill 59.88 to real exit, over planned risk). The three numbers are three different things; none of them is "r_planned ≈ −0.18". **Do not build Phase 3 as written.** Decision needed from Jason: which unit should the shadow ledger use, given that a shadow rejection has a *simulated* fill (so the comparable live number is the same fill-to-exit over planned risk, which is −0.16R here, not `r_planned`)?

### 0.6 — config facts
- `min_stop_atr` = **0.35** (agent/config.py:86), `atr_gate` = **"warn"** (config.py:84).
- `live_paper.flatten_at`: not a key in `_DEFAULTS["live_paper"]` (config.py:91-98); the monitor reads `live_cfg.get("flatten_at") or cfg.get("flatten_at")`, and top-level `flatten_at` is **null** in config.json:50. The 15:55 flatten is therefore driven solely by `EOD_FLATTEN_MINUTES = 5` (grading.py:143), via `_past_cutoff` returning `"eod"`.
- `live_paper.allow_overnight_symbols`: does not exist yet (Phase 1 adds it).

### 0.7 — corrections to the first pass
- **BMY `atr_pct`:** the first pass recomputed 1.3989 (2.37%/d) from bars, matching `outcomes.atr14`. The spec says the 10-12 card showed 6.1%/d, which is ~2.6x higher. The spec's test fixture `atr14 = 3.78` is that card figure. **If the card really said 6.1%, `daily_context` is wrong** (addendum candidate). Not verified: I did not see the card. Either way, with the true ATR of 1.40 the stop-room threshold is 0.35 × 1.40 = 0.49 and BMY's room was 0.38, so the Phase 2 gate would still skip it. With 3.78 it would skip too (threshold 1.32). Use 1.40 in the BMY replay test.
- **XLE/DAL:** the snapshots in the first pass (XLE 66.01, DAL 80.02) are 10-13 values, not the 10-12 ref price `_validate_plan` used, and its source (bundle vs `cand_quotes`) was not determined. 0.7 stays open.
- **Data source:** the first pass calls the store "the production SQLite ledger". Production is Postgres (Neon); the local `data/ledger.duckdb` is a separate, non-production pipeline. The transcript `_phase0_final.txt` shows `runs` and `heartbeats` rows that match production, but the first pass should say which DSN it used. Unconfirmed.
- **Table/name facts the spec assumed:** the runner lane was removed in c8ede6d (the 10-12 intraday log has no `runner` key, so that is deployed). Spec Phase 1c's "runner order table" no longer exists.

### What this changes in the plan
1. Phase 1 stands, and its root-cause text should be corrected to class (b) (close refused, no retry), with 1d (retries) as the cheapest, most direct fix and 1b (broker-level backstop) covering the orphan case.
2. Phase 2 stands; fix the test fixture to `atr14 = 1.40`.
3. **Phase 3 is on hold** pending the unit decision above.
4. Phase 4 unaffected.

## Gate 0 sign-off

- [x] Evidence collected read-only (SELECT-only + read-only Alpaca GET).
- [x] No production code modified.
- [ ] Findings reviewed by Jason.
- [ ] Approval to begin Phase 1 (15:55 flatten reliability).

**Jason sign-off:** ______________________   **Date:** ______________
