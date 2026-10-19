# FIX SESSION — VERIFICATION REPORT

**Sessions covered:** 2026-10-13, 10-15, 10-16 incidents · **Work done:** 2026-10-16 evening → 10-18
**Repo:** shipped 2026-10-18 as `61b9648`: deployed, both regrades applied. The §8 follow-up is in the working tree, not yet committed or deployed.
**Read with:** `docs/FIX_PHASE0.md` (the evidence behind every "cause" below)
**2026-10-17 update:** you decided the open questions (§6). The out-of-allowlist follow-ups are applied. The fixes now verify themselves in the evening email.
**2026-10-18 update:** shipped. §8 has what the production impact report showed, and the three flaws it exposed in my new tools, now fixed.

What was **not** done, by rule: no order placed, cancelled or closed; no production row written or read; no `--apply`; no config flag flipped. The broker was only ever read with GET requests, through a guard that raises on any other verb. Production was not read: I have no read path to it. Every production number below therefore comes from `scripts/verify_fixes.py`, which you run.

---

## 1. Result at a glance

| | Phase 0 verdict | Fixed by | New tests | Mutations caught |
|---|---|---|---|---|
| **D1** account flat after the close | confirmed, bigger (IOVA carried 16 sessions, opened by a *local* run) | broker-truth EOD backstop + morning orphan sweep, extra 15:57/15:59 ticks | 25 | 22/22 |
| **D2** limit fills on a touch | confirmed in code; RKLB was a 2¢ (3.7 bps) trade-through | sim v8: a resting limit fills on a trade-through of max(1 tick, 5 bps) | 20 + 46 (shared with D3) | 23/23 (with D3) |
| **D3** ledger never reconciled to the broker | confirmed | broker fields beside the sim, `sim_broker_divergence`, broker headline in the review | (in the 46) | (with D2) |
| **D4** shadow ledger double-counts and misprices | confirmed, two causes | lane-traded rejections excluded; shadow priced by the same `simulate_pick` call, time stop included, in planned-risk R | 13 | 17/17 |
| **D5** entry order contradicts the setup | confirmed | `entry_semantics` tag on every pick (tag only; enforcement off) | 19 | 16/16 |
| **Phase 5** time-stop counterfactual | (reference only) | `r_if_held_to_flatten` on every triggered outcome | 16 | 12/12 |
| **D6** grader misreads its own orders | confirmed | `ORDER MECHANICS` section in the grader bundle, grader prompt v11, `mechanics_contradiction` audit | 41 | 30/30 |
| **D7** gap-field stats contaminated | **not reproduced as stated** (forward counters; evening/nightly timing) | no code; finding in §4.8; verify check encodes the timing | — | — |
| **D8** ETF levels ungrounded | confirmed | `ETF live prices` line in the bundle, gate reads the same quotes, analyst prompt v14 | 12 | 16/16 |
| **D9** same-direction market orders | **not reproduced** (bracket-leg timestamps misread) | no code; finding in §4.10 | — | — |
| **Phase 8** `agent/fix_verification.py` + `scripts/verify_fixes.py` | | the per-session checks (the evening self-check runs them itself) + `--impact` | 38 | 25/25 |
| **follow-ups** (applied 10-17) | | the trader's log, weekly totals, ablation pool and chart replay treat lane trades as taken, not forgone | 16 | 12/12 |
| **self-verification** (10-17) | | SELF-CHECK "Fix verification (5 sessions)" and "Entry semantics (D5)" | 16 | 14/14 |
| **precision** (10-18, after the first production run) | | the audit spares an order already through its level at go-live and a remark about the setup label; the impact report quotes what fired and finds the ETF kills where they were recorded (§8) | 29 | 26/26 |

## 2. The suite

```
baseline (HEAD 7f8053d, before any change):  1232 passed
before shipping (my last full run, 10-17):   1494 passed in 209.71s     <- full run, DATABASE_URL unset
now (61b9648 + the §8 follow-up):            1528 passed in 212.57s     <- same; +29 mine, +5 the P&L breakdown's (§8)
```

The +296 are all in new or extended test files (collected counts, HEAD export vs working tree):

| file | HEAD | now | for |
|---|---|---|---|
| `tests/test_eod_backstop.py` | — | 25 | D1 |
| `tests/test_grading.py` | 81 | 117 | D2 (20 tests appended) + Phase 5 (16 appended); **no existing line changed** (`git diff` shows additions only) |
| `tests/test_fill_reconcile.py` | — | 46 | D2 (28 real broker entries replayed) + D3 |
| `tests/test_shadow.py` | — | 13 | D4 |
| `tests/test_entry_semantics.py` | — | 19 | D5 |
| `tests/test_mechanics_audit.py` | — | 40 | D6 (41 until one case moved, §8) |
| `tests/test_mechanics_precision.py` | — | 25 | D6 precision (§8) |
| `tests/test_etf_grounding.py` | — | 12 | D8 |
| `tests/test_verify_fixes.py` | — | 43 | Phase 8 (38) + the impact fixes (5, §8) |
| `tests/test_pnl_breakdown.py` | — | 5 | not this session's: the P&L breakdown that came in with `61b9648` (§8) |
| `tests/test_fix_followups.py` | — | 16 | the follow-ups (§6) |
| `tests/test_fix_selfcheck.py` | — | 16 | the self-verification (§6) |

**Existing tests I had to edit (one, plus two fixtures), and why.** `tests/test_persona.py` pins every system prompt byte-for-byte to `tests/fixtures/prompt_renders/*.md` and pins each prompt's version. Phase 6 and Phase 7 required prompt changes with a version bump, so:
- `PROMPT_VERSIONS`: `evening_grader` 10 → 11, `morning_analyst` 13 → 14 (one line, with a comment saying why);
- `tests/fixtures/prompt_renders/evening_grader.md` and `morning_analyst.md` regenerated with `scripts/capture_prompt_renders.py`, the repo's own procedure for a deliberate prompt change. The other three fixtures came out byte-identical.

No other pre-existing test was touched. One caught a wording change I had made in the evening's shadow line (`test_the_shadow_line_counts_what_its_net_covers_and_claims_nothing`). I changed **the code back**, not the test (§4.4). Three of this session's own new tests were later adjusted on purpose:
- `tests/test_shadow.py`, for the follow-ups: a day whose only rejection was traded now has no forgone-R slot at all, and lists the lane trade.
- `tests/test_grading.py`'s guard on who may name `r_if_held_to_flatten` now also allows `fix_verification.py`, the reference report.
- `tests/test_mechanics_audit.py` (10-18): "The fill came 97 minutes after the open." left the cases that must be flagged. A fill time stated as a fact is true by the numbers, and the audit fires only on claims they contradict. The fix-it prompt's own phrase, "entered N minutes after the open", is still flagged (§8).

## 3. Mutation table

Each mutation is a deliberate bug put into the fixed code, then the named tests run, then the code restored and its hash verified. "Caught" means at least one test failed. Every mutation ran when its phase closed, and **all of them were re-run against the final code**; the counts are from that re-run.

| item | tests that catch it | fails without the fix | passes with the fix | mutations caught |
|---|---|---|---|---|
| D1 | `tests/test_eod_backstop.py` | yes | yes | **22/22** |
| D2 + D3 | `tests/test_grading.py` (D2 block), `tests/test_fill_reconcile.py` | yes | yes | **23/23** |
| D4 | `tests/test_shadow.py` | yes | yes | **17/17** |
| D5 | `tests/test_entry_semantics.py` | yes | yes | **16/16** |
| Phase 5 | `tests/test_grading.py` (Phase 5 block) | yes | yes | **12/12** |
| D6 | `tests/test_mechanics_audit.py` | yes | yes | **30/30** |
| D8 | `tests/test_etf_grounding.py` | yes | yes | **16/16** |
| verify_fixes.py | `tests/test_verify_fixes.py` | yes | yes | **25/25** |
| follow-ups | `tests/test_fix_followups.py` | (new readers) | yes | **12/12** |
| self-verification | `tests/test_fix_selfcheck.py` | (new check) | yes | **14/14** |
| precision (10-18) | `tests/test_mechanics_precision.py`, `tests/test_verify_fixes.py` | yes: 13 of the first 24 precision tests, and all 5 impact tests (the other 11 are guards that pass both ways) | yes | **26/26** |
| **total** | | | | **213/213** |

Some examples of what "caught" covers:
- D1: a second close that would flip long 250 into short 250; an unreadable account read as flat; the allow-list ignored.
- D2: the gap-through-at-open lost; a stop entry given the limit cushion.
- D4: forgone R back in `r_multiple`; the analyst's time stop dropped; a lane trade summed again; the pick's levels replaced by a stale rejection row.
- D6: each timing phrase; a sentence that says the timing was the tape's still flagged; a market entry protected.

Bookkeeping:
- Five early definitions whose target text later changed inside their own phase are counted once, in their re-defined form: D1's M3, M3b and M13, and D6's B1 and B2.
- Two (D2's G4 and G5) first ran against a test list that predates the test that catches them. They were re-checked against the phase's whole test files: both caught.

"Fails without the fix" is shown three ways: (a) the mutation table; (b) every phase's new tests run against an untouched copy of `HEAD`, with only the new *names* stubbed in so the module imports: D4 10 of 11 failed (the 11th guards legacy behaviour and should pass), D5 12 of 18, Phase 5 13 of 13, D6 17 of 27, D8 11 of 11, each on the behaviour, not on an import; (c) the incident reproductions below, run on the unfixed code.

## 4. Per item

### 4.1 D1 — the account is flat after the close

**Cause.** The 15:55 flatten and `carry_cleanup` only ever look at *ledger rows* (`live_orders` for today, open). IOVA (250 @ 14.36) was bought 2026-09-30 by a **local** `run.py` morning with `DATABASE_URL` unset. That run traded the shared paper account and recorded its rows in the local DuckDB, which production never reads. So no production row ever existed, and nothing closed it for 16 sessions. There was also only one flatten tick (15:55:02) with ~5 s of in-tick retries.

**Fix** (`agent/live_monitor.py`, `agent/live_broker.py`, `agent/ledger.py` + `agent/migrations_v36.py`, `agent/loops/morning.py`, `api/index.py`, `agent/health.py`, `agent/delivery.py`, `agent/config.py`, `scripts/intraday_cron.ps1`):
- `eod_backstop()`: from close − 5 min, **every broker position** not in `live_paper.allow_overnight_symbols` (default empty) is closed, whatever any ledger says. When a position was partly opened today, the morning sweep closes only the carried shares, counted from the broker's own fills.
- `orphan_sweep()` in the morning and on every tick: a position carried from a **prior** session is closed even if a ledger row claims it; only a position opened today is protected by a claim. The claiming row is settled `exit_reason='overnight_carry'` with the real fill.
- Idempotency: a tick lease, an in-flight map, and a check for a close already working at the broker (yours included). A second tick never re-sends; flipping long 250 to short 250 is tested.
- Every sweep close is a row in `eod_backstop_closes` (schema v36). It produces the **FLATTEN MISSED** line in the morning email, `/api/status` and the health warnings.
- `intraday_cron.ps1` now ticks **15:55:02, 15:57:02, 15:59:02**: three flatten attempts.

**Tests** (`tests/test_eod_backstop.py`, 25) use IOVA's real numbers: a claimed carry closed by the morning sweep and settled; a position opened today left alone before 15:55; everything closed at 15:55, claimed or not; `list_positions() is None` closes nothing; two backstop calls never double-close; FLATTEN MISSED appears on a seeded carry and is silent on a clean day. Before the fix, the same intraday route on the IOVA state returned `{"updated":0,"carry":{"stale":0}}` and never called close.

**Live check (read-only, 10-16 night).** The real broker held IOVA long 250 with your queued `sell_to_close` (accepted 21:11 ET). With the broker exactly as it was, the new sweep recognised that close as working, sent nothing, and still reported the carry. With your order hidden, it would have closed 250 at market.

### 4.2 D2 — the simulator fills only what a broker would

**Cause.** `grading.py` filled a resting limit on a touch. That is in the code, but it is **not** what happened to RKLB: the 11:15 bar printed 54.02, 2¢ (3.7 bps) *through* 54.00, and Alpaca still did not fill. A strict `>` rule would also have filled it.

**Fix** (`agent/grading.py`, `CURRENT_SIM_VERSION` 7 → **8**). A resting limit fills only when a bar trades through it by **max(1 tick, 5 bps)**, or opens through it (marketable at the open, unchanged). Stops, targets and breakout entries keep touch semantics; the broker's data supports that (67/68 stop legs filled within a minute of the first touch). Touches that did not fill are counted in `outcomes.touch_only_bars` (schema v37).

**Calibration**, from the account's own history: 35 limit entries and 10 stop entries × real minute bars. Every unfilled limit had negative penetration except RKLB (+3.7 bps). The smallest filled limit was NVDA 10-08 at 5.07 bps. So 5 bps is the only round threshold consistent with all 45, and it equals the configured `slippage_bps`.

**One literal test from the prompt is deliberately different:** "high 54.01 → fills" became "54.01 / 54.02 do not fill, 54.03+ fills" (§5).

**Tests:** 20 in `tests/test_grading.py`: RKLB touch, 54.01, 54.02 (the real bar) do not fill; 54.03/54.05/54.40 fill at the limit; the CMC buy-limit mirror; gap-through at the open; a stop entry still triggers on a touch; touch-after-cutoff; v7 ≠ v8 semantics. Plus `tests/test_fill_reconcile.py::test_the_simulator_enters_exactly_when_the_broker_did`, **28 real broker entries replayed, 28/28 agree**.

**Impact on the account's history** (real bars, the 35 limit + 10 stop entries 9/10 → 10/16): the v7 rule agreed with the broker on 33/35 limit entries, v8 on 34/35. Deferred 09:45 entries: 19/20 → **20/20**. Of the 26 fills the v7 simulator made on those 45 entries, v8 removes exactly one: RKLB 10-15, worth +0.207R that was never real. The remaining miss is INKT 9/30, a broker fill the sim misses either way. Production history: `verify_fixes.py --impact` prints the same comparison for every graded limit pick.

### 4.3 D3 — the review shows the broker

**Cause.** The broker's fills were stored (`live_orders`, `outcomes.live_r`) but never classified, never named per pick, and never shown to the grader, the review or the trader's log. On 10-15 the review said −1.56R with one winner (RKLB), while the paper account lost $222 with no winner.

**Fix** (`agent/loops/evening.py`, `agent/grading.py`, `agent/ledger.py` schema v37). The broker is reconciled **before** the review is written. For every live pick, `outcomes` gets `broker_entry_fill`, `broker_exit_fill`, `broker_exit_reason`, `broker_status`, `live_r` and `sim_broker_divergence` (`none` | `sim_only_fill` | `broker_only_fill` | `price_gap`). The review's outcomes block opens with the broker's result as the headline, with one line per divergence. The simulated R stays the research number; nothing is deleted.

**Tests** (in `tests/test_fill_reconcile.py`): divergence classes, record-and-idempotence, the headline text, persistence, and an end-to-end evening with a stub grader. **The trader's log half** needed `agent/traders_log.py`, outside the allowlist. It is applied since your approval on 10-17 (§6): `day.broker_net_r` and `day.divergences` slots.

### 4.4 D4 — the shadow ledger measures only what the filters stopped

**Two causes**, both reproduced on the unfixed code with the real bars:
1. The shadow priced every rejection with **no time stop**, in `r_multiple` (the floored diagnostic), not the planned-risk R a taken pick is reported in. INTC 10-15: taken −0.51R at its 14:30 time stop; shadow −1.01R at the 15:42 stop.
2. A rejection the desk **traded anyway** (coverage, daily floor, hypothesis) was still summed into forgone R.

Unfixed code on the incident days: **10-15 net forgone −1.41R** and **10-16 +0.62R**, the exact figures the two reviews read as "the filters saved money" and "the filters cost the desk". Fixed: **0.00R both days**. INTC (−0.51R), CMC (−0.39R) and AMAT (+0.06R) are listed on their own "traded via a lane" line with the R they were taken at.

**Fix** (`agent/grading.py`, `agent/loops/evening.py`):
- `classify_shadow` prices in `realized_r` (planned-risk R).
- `shadow_pick()` is the one definition of a shadow trade, handed to the same `simulate_pick` a taken pick goes through.
- `shadow_time_stop()` recovers the analyst's time stop from `daily_plans.analyst_json`, which also works for history.
- A traded rejection is priced with the **pick's own** parameters (so shadow R == taken R exactly), stored `eod_outcome='traded_via_lane'` with **no** `forgone_r`, and listed apart.
- The review's sign legend ("positive = the filters cost the desk") prints only beside a non-zero net.

I first reworded the evening's shadow line. An existing test (`test_the_shadow_line_counts_what_its_net_covers_and_claims_nothing`) pinned the old sentence, and the old sentence was right, so I restored it and only made the legend conditional.

**Tests** (`tests/test_shadow.py`, 13): the prompt's four, on real bars:
- 10-15 INTC + CMC + five untriggered rejections give 0.00R and the lane line;
- 10-16 AMAT excluded, with no "cost the desk";
- a rejected-only INTC is priced with its 14:30 time stop;
- an 8-pick seeded invariant: shadow R == taken R exactly.

Plus: the pick's own levels win over a stale rejection row; the opposite direction is not a lane trade; the same entry window; regrading is idempotent; the line formats.

**Dry run of history.** The real thing is `verify_fixes.py --impact`, which re-prices production in memory through the evening's own `_grade_rejections`. On the seeded 10-15 day it reproduces **−1.41R → +0.00R**, with the whole −1.41 coming from the two lane rows (test: `test_impact_reprices_1015_from_minus_1_41_to_zero`). On the **local** ledger (dev-era sessions 8/19–9/30, not production), the stored net was +14.58R and re-priced is +0.06R. The mean per scored rejection went +0.429 → +0.002, and the weekly verdict stayed "NOT distinguishable from zero" (p 0.139 → 0.986). That change is mostly old sim versions; the local data predates the lanes.

**Phase 0 said** writing `forgone_r = NULL` excludes a lane row "from every total, mean, t-test and verdict without touching those files". That holds for sums, means, t-tests and verdicts. It does **not** hold for row *counts* (the weekly "total rejected and graded" and the category n), for the random-pick ablation pool (a lane row entered as a 0.0R twin of the taken trade), or for the chart drawer's replay guard. Those fixes are applied since 10-17 (§6).

### 4.5 D5 — entry type must express the setup (measured, not enforced)

**Cause.** Nothing linked `setup_type` to `entry_type`; only prompt prose did.

**Fix** (`agent/loops/morning.py` = the validator, `agent/config.py`, `agent/ledger.py` + `agent/migrations_v38.py`). `_entry_semantics()` is a pure check that runs in `_validate_plan` on the final levels, against the premarket reference the gates already use. It stamps every pick with `entry_semantics` and `entry_ref_price`. The tags:
- `strength_buy_limit_below_ref` (catalyst_momentum / sector_rotation long);
- `weakness_sell_limit_above_ref` (the short mirror);
- `breakdown_short_with_limit`;
- `breakout_long_with_limit`;
- `gap_fade_limit_without_expiry`: no entry expiry field exists today, so every gap-fade limit carries this tag until one does. That is honest, and visible in the table.

**`validator.enforce_entry_semantics` defaults to false.** With it off no pick is dropped; this is tested both with the key missing and with it explicitly false.

**Tests** (`tests/test_entry_semantics.py`, 19), with real references, the last premarket print at or before 07:10 ET:
- CMC 66.50 vs **67.97** → mismatch;
- USO 155.50 vs **156.80** (the figure the incident report quotes) → mismatch;
- RKLB breakdown_short + limit → mismatch;
- INTC 92.00 vs 93.82 → ok;
- AMAT 418 vs 426 → ok;
- enforcement off drops nothing; on, it drops exactly CMC, USO and RKLB.

**The table you decide enforcement from** (count by tag, fill rate, mean R) comes from `verify_fixes.py --impact`. History before the tag is tagged there from the stored levels, with the premarket proxy as reference.

### 4.6 Phase 5 — `r_if_held_to_flatten`

**Fix** (`agent/grading.py`; the column rode in v37). For every triggered outcome: the same fill, held with only its initial stop to the 15:55 flatten (exit at that bar's open, same friction), in planned-risk R. It never enters `r_multiple`, `r_planned` or any total. A test enforces that only the simulator and the ledger columns name it.

**AMAT 10-16:** the prompt's illustrative bar gives time stop +0.06 / held **+0.55**. The prompt said ≈ +0.57; the gap is the 5 bps exit friction. On the **real** bars: time stop **+0.056R**, held **+0.626R** (the 15:55 bar opened 408.20). The 433 stop was never touched, so held equals the hold-to-close number.

**Tests:** 16 in `tests/test_grading.py`, both AMAT cases plus stop, fill-bar stop, flatten, target-then-reversal, time-stop-saved, short gap-through, friction, thin bars, bars ending early, no fill / invalid fill, persistence. History (sum and mean of `r_if_held_to_flatten − r_multiple` over time-stopped trades, and − `r_planned`) comes from `--impact`.

### 4.7 D6 — the grader is told how its orders work

**Cause.** The grader saw `entry breakout @ 418` and a fill time, nothing about what the order does. Its prompt (v10) even listed "entry slightly chased" in the B grade. The numeric audit passed AMAT and INTC because no number was wrong.

**Fix** (`prompts/evening_grader.md` **v10 → v11**, `agent/loops/evening.py`, `agent/review_audit.py`):
- every pick in the bundle gets a computed `ORDER MECHANICS` line (AMAT's is exactly the spec's sentence);
- the prompt says a resting/stop fill time is the tape's, and restricts "chased" to `open` entries;
- the audit flags `mechanics_contradiction`: a filled limit/breakout fill called late, chasing or "N minutes after the open", or a breakout order called senseless. It is checked sentence by sentence, with a sentence that itself says the timing was the tape's exempt, market entries exempt, and execution anomalies exempt. A flagged pick review or plan note keeps its words with an `[AUDIT: …]` note; **a lesson or idea built on it is dropped**;
- the evening records `grader_prompt_version` and the audit counts in `runs.stages`, so `verify_fixes` can check both;
- (10-18, §8) an order already through its level when it went live was a market order in effect: its mechanics line says so, and its timing may be criticised. "Breakout ... makes no sense" counts only when the words are about the order, not the setup label. A fill time stated as a fact is not flagged.

**Tests** (`tests/test_mechanics_audit.py`, 41): the AMAT sentence ("97 minutes late … chasing") flagged and its lesson dropped; the INTC sentence ("breakout … makes no sense") flagged; correct sentences not flagged; each phrasing; the bundle end to end.

Mutation testing found a real bug in my first version: only the first timing phrase of a sentence was judged, so "Chased it, then 97 minutes late." passed. Now every match is judged.

### 4.8 D7 — not reproduced as stated (finding; no code)

The quoted n/mean are the **forward** counters, not in-sample figures. The guard the prompt asks for already exists (`record_rule_observation` refuses anything on or before `quarantined_at`). "Forward observations recorded: 0" is a timing artifact: the evening (~16:30) books morning-lane observations, but the gap field is priced and booked by the **nightly** (~23:00), after the evening has printed. The real weakness is that one night's units share one tape. That is reported, not changed, because it is a design choice with a separate sequential horizon.

`verify_fixes.py` D7 encodes the timing. A gap-field rule is PENDING until 23:45 ET on the session date, then PASS only if observations were booked for every rule whose cell had matching rows. Run it again the next morning for that item.

### 4.9 D8 — every fund the analyst can name has a live price

**Cause.** The bundle gave the sector ETFs as **percent moves only** (`Sectors: XLE +1.2% …`), and USO not at all. The analyst priced from memory: USO 68.50 (traded ~153.6), XLV 148 (176.70), XLE 87 (66.85). The 15% gate killed each pick afterwards; the gate's quote was right every time.

**Fix** (`agent/providers.py`, `agent/loops/morning.py` `_reference_prices`, `prompts/morning_analyst.md` **v13 → v14**). Phase 7 has no allowlist; Phase 0 named these files.
- `NAMEABLE_FUNDS` (index, sector, industry, commodity, rates, international ETFs; none leveraged) is priced by one batched snapshot into an **`ETF live prices`** line.
- The Sectors line now says it is percent moves, not prices.
- A failed fetch is recorded in `missing` and tells the analyst to name no ETF.
- The grounding gate judges against the same quotes the analyst saw.
- The prompt says an ETF off that line may be named only with a quote it retrieved itself.

Before / after, the analyst's view of the same morning:
```
UNFIXED: **Sectors:** XLE +1.2% | XLK +0.9% | XLV -0.4%           mentions a USO price: False | an XLE price: False
FIXED:   **Sectors (percent moves, NOT prices):** XLE +1.2% | ...
         **ETF live prices (anchor EVERY ETF level on these; ...):** XLE 66.85 (+1.0%) | ... USO 153.6 ...
```
**Tests** (`tests/test_etf_grounding.py`, 12): the three incident funds priced in one call, the gate agrees, levels from memory are still killed, levels on the quote survive, and failure is visible.

### 4.10 D9 — not reproduced (finding; no code)

MU 10-13 was a **short** (sell stop 940, filled 10:50:50), so its market *buy* at 13:00:05 was `buy_to_close`, the time-stop exit. VFC was a **long**, so its market *sell* at 14:00:04 was `sell_to_close`. The "canceled limit at 10:50 / 09:46" were the take-profit **legs**: a leg's `submitted_at` is its parent's fill time. Both market orders are exits on the correct side. The complete list of code paths that can send an entry-direction market order (open entries; a crossed breakout) is idempotent per pick, so a cancelled entry cannot be re-sent.

## 5. Where I departed from the prompt (with the evidence)

1. **D2's literal test.** `high 54.01 → fills` would make the simulator fill RKLB, which the broker did not. The calibrated rule fills at 54.03 (§4.2).
2. **D7 and D9** were not reproduced, so no code (§4.8, §4.10).
3. **Phase 5's ≈ +0.57.** Exact on the prompt's own illustrative bar is +0.55 (friction); the real AMAT bars give +0.626 (§4.6).
4. **No dry-run regrade for taken picks exists** (`run.py regrade` writes). The history numbers come from `verify_fixes.py --impact`, which re-prices in memory.
5. **The trader's-log halves of D3 and D4** needed files outside every allowlist. They were held back as a tested patch until you approved them on 10-17 (§6).
6. **Pullback lane / `EXECUTION_FIX_SPEC_v1.md`** do not exist in the repo. Phase 1 built from the prompt's bullets; claims are `live_orders` rows behind an interface a future lane can use.

## 6. Decided, automated, shipped

**Your decisions (2026-10-17):**

| question | decision |
|---|---|
| the out-of-allowlist follow-ups | **applied** (below) |
| re-price shadow-ledger history | **yes, all history**: the old numbers double-count trades taken anyway and ignore time stops, so they are wrong whatever `--impact` shows |
| re-grade taken picks under sim v8 | **yes, from 2026-10-02**, the current execution regime (09:45 window, 15:55 flatten); grades are kept |
| `validator.enforce_entry_semantics` | **off**; the evening email says DECISION DUE when the evidence is in |

**Applied follow-ups** (files outside the phase allowlists, so they waited for you):

| file | change |
|---|---|
| `agent/traders_log.py` | "the filters saved/cost" only with a non-zero forgone R, quoting `{{shadow.forgone_r}}`. Enforced in the validator: a violation sends the log to its facts-only fallback. Lane trades are listed with their taken R. The broker result (`day.broker_net_r`) and divergences are the headline for live picks. |
| `agent/ledger.py` `shadow_summary` | lane-traded rows counted apart (`traded_via_lane`), out of every total and count; captured R in planned-risk R like forgone R |
| `agent/loops/weekly.py` | the miner's total line says how many were excluded |
| `agent/ablation.py` | the random-pick pool stops taking a 0.0R twin of a taken trade |
| `agent/chart_payload.py` | the drawer's shadow replay prices the way the evening does (time stop; lane rows checked against the taken R) |

**Nothing to remember after shipping: the fixes verify themselves.** The evening SELF-CHECK, at the end of the evening email and on the dashboard's health panel when flagged, now carries two lines:
- **Fix verification (5 sessions).** Each evening it judges every *finished* session on the fixed code, using the six checks in `agent/fix_verification.py`:
  - a finished session is one whose evening, that night's gap-field booking (D7) and the next morning's sweep (D1) have all run;
  - a session counts when every item passes;
  - it shows the streak ("3/5 sessions in a row pass"), and turns **bad** with the reason when the newest finished session fails;
  - it reads the account right after today's close, and turns bad if a position is still open;
  - after 5 in a row it says **VERIFIED**, and from the next evening it is one retired line.
- **Entry semantics (D5).** Fill rate and mean R per tag. Once any mismatch tag has 8 graded picks it says **DECISION DUE**, every evening, with the numbers beside the `ok` picks, until you decide on enforcement.

**Shipped 2026-10-18 with one script** (marker: `%USERPROFILE%\.trade-agent\fixes_shipped.txt`; §8). It does, in order:
1. the test suite;
2. commit;
3. rehearse, skipped if a passing rehearsal already covers the commit;
4. `deploy.ps1`, which pushes, runs `vercel --prod` and warms schema v36–v38 (that warm-up tick also runs the orphan sweep, so it closes any carry);
5. the read-only impact report, saved to `reports\fix_impact_<date>.txt`;
6. the shadow regrade;
7. the taken-pick regrade.

It stops at the first failure and is safe to re-run. Outside a cron window it takes one go. A marker file stops it from running twice.

```powershell
cd "C:\Users\Quantum Lab\Desktop\Trade Agent"
powershell -ExecutionPolicy Bypass -File scripts\ship_fixes.ps1 -DryRun    # optional: see the steps
powershell -ExecutionPolicy Bypass -File scripts\ship_fixes.ps1
```

**IOVA.** Your queued `sell_to_close` (250, accepted 10-16 21:11 ET) fills at Monday's open. If it does not, the deploy's warm-up tick, or the next morning run, closes it as a carry and prints FLATTEN MISSED.

`TradeAgent-Intraday` starts `scripts\intraday_cron.ps1` fresh each weekday, so the new 15:57/15:59 ticks need no restart unless you deploy mid-session.

## 7. What the checks test (PASS criteria)

The evening self-check runs these every session. `python scripts/verify_fixes.py --date <session>` runs the same checks by hand, read-only, whenever you want to look yourself.

| item | PASS when |
|---|---|
| D1 | run on the session date after the close: zero broker positions outside the allow-list. Run for a past session: the day's fills net to zero per symbol (an earlier carry that day's morning sweep closed is explained), and the next session's morning sweep found nothing still open. Either way, every `eod_backstop_closes` row went through. |
| D2/D3 | every pick that went to the broker has the broker's fields and a divergence class; every divergence other than `none` is listed, and touch-only limits too |
| D4 | no traded rejection carries forgone R; each is `traded_via_lane`; its shadow re-priced from the real bars equals the taken R |
| D5 | every pick of the session has an `entry_semantics` tag (counts and mismatches listed) |
| D6 | the evening recorded grader prompt v11 (the repo's current); `mechanics_contradiction` flags counted |
| D7 | every rule under forward test whose cell had matching rows that day booked forward observations (gap field: after the nightly) |

It opens production read-only (a Postgres session in read-only mode, with no migration run), reads the broker with GETs only, and re-prices `--impact` history in an in-memory database. It writes nothing and places nothing.

As a tool check, I ran `--impact` once on the **local** ledger, which holds dev-era sessions 8/19–9/30, not production. It completed all six sections against real Alpaca bars, and the ledger file's hash was the same before and after. The local numbers are the ones quoted in §4.4; they say nothing about production.

## 8. Shipped, and what production showed (2026-10-18)

`scripts\ship_fixes.ps1` ran end to end: the suite, commit `61b9648`, the rehearsal, the deploy, the impact report (`reports\fix_impact_2026-10-18.txt`), the shadow regrade of all history and the taken-pick regrade from 2026-10-02.

**Also in `61b9648`.** Step 2 commits the whole working tree, and it held two pieces of another session's uncommitted work:
- the dashboard's P&L breakdown: `agent/webui.py`, `/api/pnl/day` and `/api/pnl/history` in `api/index.py`, `ledger.day_breakdown` and `pnl_breakdown_history`, with `tests/test_pnl_breakdown.py`;
- `specs/PULLBACK_LANE_PHASE0_REPORT.md`.

Both passed the suite and the rehearsal, and the P&L breakdown is live.

### What the impact report showed (production, 2026-09-17 to 10-16, read-only)

| item | before the fixes | after |
|---|---|---|
| D4 shadow ledger, 21 sessions, 141 rejections | net forgone **+8.12R**: "the filters cost the desk" | **−4.12R**. 35 rejections were traded anyway, and they made up +2.84R of the old net. The rest is the time stop, planned-risk R and sim v8. The incident days: 10-15 −1.41R → 0.00, 10-16 +0.62R → 0.00 |
| the weekly verdict | not distinguishable from zero (mean +0.063R per rejection, p 0.275) | still not distinguishable (mean −0.043R, p 0.311). The sign flipped. Neither "the filters work" nor "the filters are too tight" is supported |
| D2: 36 graded limit picks | the touch rule fills 22 | the trade-through rule fills 21. The one difference is RKLB 10-15: +0.21R booked that was never real |
| Phase 5: 22 time-stopped trades | | holding each to the flatten instead would have averaged −0.012R per trade (planned R). The time stops cost nothing on net. AMAT 10-16 is the largest the other way (+0.57R given up); ALEC 10-05's stop saved 0.63R |

D5, graded picks by entry semantics (history tagged from the stored levels):

| tag | n | filled | mean R per filled trade |
|---|---|---|---|
| ok | 121 | 88% | −0.56R |
| strength_buy_limit_below_ref | 13 | 69% | −0.04R |
| breakdown_short_with_limit | 4 | 100% | +0.04R |
| gap_fade_limit_without_expiry | 3 | 100% | −0.46R |

So far the mismatched entries did better than the `ok` ones, on small numbers, and enforcement would have dropped them. It stays off. The evening self-check counts tagged picks from the tag's first production day (10-19) and says DECISION DUE at 8 graded picks per tag. The larger fact in this table is the `ok` row: −0.56R per filled trade over 107 trades. Rows before 10-02 were graded by older simulator versions, and lane trades are included.

### Three flaws in my new tools, now fixed (working tree, not yet deployed)

**1. The audit flagged 7 reviews it should not have.** It flagged 9 of 140 stored reviews: AMAT 10-16 and INTC 10-15, the incidents it was built for, and 7 more. The report printed only each review's first 140 characters, which show two kinds of false flag:
- **An order already through its level when it went live.** CMC 10-15 is the clear case: a buy limit at 66.50 with the market at 66.03 when it went live at 09:45, so it filled at once. That was a market order in effect (`live_monitor` sends a crossed breakout as one). The go-live time set its fill time, so the grader's timing criticism was fair. S 10-13 (09:45) and NKE 10-02 (at the 09:30 open; it had no entry window) also filled on their first live bar, by their own notes.
- **A remark about the setup label.** NVDA 10-05: "... so labeling as breakout is ..." criticised the label, not the stop order.

For C 10-14, ACN 10-14 and CRWV 10-15, what fired is past the 140 characters the report printed.

**The fix** (`agent/review_audit.py`; it runs every evening in production):
- An order that filled on its first live bar (the entry window, else the 09:30 open), more than 0.1% past its level on the side the market already was, was a market order in effect. Its ORDER MECHANICS line now says so: "already through 66.50 when the order went live ... in effect a market order sent at 09:45. The go-live time set this fill time." Neither its timing nor "makes no sense" is flagged.
- "Breakout ... makes no sense" is flagged only when the words name the order ("breakout short", "sell stop", "entering on a breakout") or call the entry itself senseless (INTC's own words: "entry logic makes no sense").
- A fill time stated as a fact ("the fill came 97 minutes after the open") is no longer flagged. "Entered" or "got in N minutes after the open" still is, and so is "late" or "chasing" said of a fill the tape timed.

On the real bars, AMAT (11:07) and INTC (09:49) are still flagged and CMC is not.

**2. The replay could not say what fired.** It now prints the phrase and the sentence for each flag, and lists the reviews it leaves alone and why. It reads each fill time from the evening's stored tape summary, which a regrade keeps, and re-simulates when that is missing.

**3. D8 printed 0, which was wrong.** The incident kills were lane trials. A lane trial's validator line is kept in `plan.validation_warnings`, not in `validator_drops`, so the report never looked there. It now counts both, plus the risk manager's kills of a fund for its price (kill check `grounding`, or a reason saying the price came from memory). Also, the D5 header printed "141 graded pick(s) (155 tagged ...)", because the tagged count included the rows the table leaves out. It now counts only what it tables, and says how many were left out.

**Tests:** `tests/test_mechanics_precision.py` (25: the CMC and INTC fills simulated from the real bars, every boundary of the go-live rule, the label case, the phrases that stay flagged) and 5 more in `tests/test_verify_fixes.py`. One case moved in `tests/test_mechanics_audit.py` (§2). Mutations **26/26**. Suite: 1528 passed in 212.57s, DATABASE_URL unset.

**To ship it.** The audit runs in production, so it needs a deploy. `review_audit.py` is a runtime file, so the deploy gate needs a fresh rehearsal (about $2–3). The last line re-runs the impact report read-only. It then shows the remaining flags with their sentences. It also checks the regrades: D4's stored net should now read about −4.12R, with the lane rows marked traded via lane.

```powershell
cd "C:\Users\Quantum Lab\Desktop\Trade Agent"
git add agent/review_audit.py agent/fix_verification.py tests/test_mechanics_audit.py tests/test_mechanics_precision.py tests/test_verify_fixes.py docs/FIX_VERIFICATION.md
git commit -m "D6 audit precision: orders through their level at go-live, setup-label remarks; impact report fixes" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
.venv\Scripts\python.exe run.py rehearse
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
.venv\Scripts\python.exe scripts\verify_fixes.py --impact > reports\fix_impact_after_precision.txt
```
