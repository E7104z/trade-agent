# PLAN INTEGRITY v1: what the 10/06 plan review found, fixed

**Status:** built 2026-10-06 night. Jason forwarded a review of the 10/06 plan as the dashboard showed it ("Does this look like a real result"), discussed it, and said "please fix everything".
**Goals served:** 2 (a day that produces no fill produces no data), 1, 5.
**Window:** VERIFICATION_PRACTICE_v1 allows fixes only through 10/19. Every item below repairs a defect observed on 10/06; none adds a lane, data family or prompt section.
**Prompts:** unchanged.

---

## What 10/06 showed (checked against the code and the real bars)

1. **No order filled.** The plan's four picks and the six measurable shadow rows never reached their entries after 09:45. Each shadow row's "scratch (+0.00R)" was the real untriggered result, not a default.
   - Four of the five long limits sat 0.41–0.53 ATR below the computed premarket low; PTC's sat 0.04 ATR below it.
   - Three theses *named* that level as their entry and priced it from memory:

     | Ticker | Thesis | Computed premarket low |
     |---|---|---|
     | CEG | "premarket low area ($268-270)" | 275.25 |
     | GOOG | "overnight low area ~$340" | 344.16 |
     | NVDA | "premarket low/VWAP ~$236-238" | 239.42 |

   - At the named levels, GOOG and NVDA would have filled.
2. **CEG, the "cleanest catalyst" the stance said it was taking, vanished.** Its shadow row read "not selected by risk manager (no explicit reason)". The code writes that when the risk manager did NOT reject a ticker, so the validator most likely removed an approval. The streak/drawdown conviction bar is the likeliest cause: it drops approvals below 4, and CEG was a 3. Coverage then filled the setup with GOOG, the weakest version of the same idea. The validator's reasons existed only in a log Vercel discards after an hour.
3. **PTC disowned its label and traded anyway.**
   - Its thesis: "NOT a momentum trade — it's M&A arbitrage, which is outside my setup taxonomy".
   - The risk manager's reason: "Wrong time horizon".
   - `setup_fit` blocked a test lane only when it was the *primary* kill.
4. **Correlation.** The review called GOOG and NVDA one AI bet; their prior 20-day correlation was
   +0.06, so by the numbers they were not. But coverage ignored correlation entirely, so a real
   duplicate (two share classes, an ETF and its top holding) could go on twice. And the plain
   20-day correlation the risk manager has been shown for kill-check 17 is fragile on this desk:
   in the rehearsal of the first build it called CEG and HTZ one bet (+0.81), on the strength of
   one shared gap day (+12.3% and +15.1% on 10/06); without that day it was +0.49.
5. **The dashboard showed no lane,** so a test pick appeared both "taken" and "killed".

## The fixes (`agent/loops/morning.py` unless noted)

- **Level grounding (`_level_fix`, `_level_map`).** Applies to a limit or breakout whose ENTRY clause names a computed level: premarket low/high (also "overnight low/high"), prior-day low/high, or premarket VWAP.
  - If the order sits more than `anchor_tol_atr` (0.25) ATR from that level's computed value, it moves onto the level. Stop and target shift by the same amount, so risk, reward and R:R are unchanged.
  - Only a level on the side of the price the order waits on can be its anchor. A "stop below the overnight low" clause is never read as the entry.
  - The plan, the email and the dashboard show "moved from X to the computed Y".
- **Validator record.** `plan["validator_drops"]` holds {ticker, kind, reason}, where kind is `discipline`, `trim` or `validity`. `plan["validation_warnings"]` holds every validator and lane line. Both are stored in plan_json.
  - The shadow ledger now says "approved by the risk manager, then dropped by the validator: …" (stage `validator_dropped`), or "the risk manager neither approved nor rejected it" (stage `risk_unaddressed`).
- **A dropped approval comes before a weaker sibling.** In every test lane, an approval removed only for discipline or count ranks first in its setup and goes on at minimum size. The discipline rule still holds, since it is never traded at approval size, but it no longer swaps the desk's best idea for its worst.
- **Labels.**
  - A `setup_fit` kill blocks every test lane wherever it appears in `kill_checks`.
  - So does a thesis or kill reason that disowns the label: "outside my setup taxonomy", "label is a stretch", "wrong horizon", "not intraday", "doesn't fit the setup".
  - The probe lane follows the same rules.
- **One bet.** A test-lane candidate that would double a pick already on is skipped and logged ("the same bet as XLE (20d corr +0.84)").
  - Doubling means one risk unit: |20d corr| above `CORR_PAIR` (0.7, the risk manager's kill-check 17 line) **with every single day left out**, and pointing the same way.
  - A link that rests on one day is a shared news day, not a shared bet. On real data, GOOG/GOOGL (+0.99) and XLE/XOM (+0.84, +0.77 at worst) pass; CEG/HTZ (+0.81, +0.49 without 10/06) does not.
  - A pair is measured only when both series cover the same days; unmeasured is never a pair.
  - The risk manager's kill-check 17 list uses the same rule, so it no longer lists unrelated names as one risk unit.
  - One correlation pass covers both arms' candidates and travels in the checkpoint.
- **Dashboard (`agent/webui.py`).**
  - Each plan row carries its lane.
  - A note explains test picks.
  - A shadow row traded anyway says "traded anyway: coverage".
- **Self-check "Entries and fills" (`agent/selfcheck.py`).** Over the last 10 sessions it reports:
  - limit/breakout orders filled out of placed;
  - entries moved onto a computed level, and how many of those filled;
  - approvals the validator dropped, by kind.

  It warns when fewer than half of 8 or more orders fill.

- **No premarket fetch before 04:00 ET (`agent/providers.py`).** Rehearsals run after midnight logged "premarket bars fetch failed" (HTTP 400).
  - I first blamed rights and warrants in the batch (NMCO.RT, …W) and shipped a retry without them (ecf2346).
  - A direct test disproved that. Those symbols return bars fine. CEG alone gets the same 400 when the window starts at 04:00 today and ends at 00:18: the window ends before it starts.
  - The retry is removed. Before 04:00 ET the fetch is skipped and logged as "no premarket session yet".
  - Production's morning (~07:11 ET) never hit this.

## Kept as designed

- **Coverage admits R:R from 1.0;** the 2.0 bar is for the risk manager's approvals. The lane exists to test a setup at minimum size.
- **Prices from memory for tickers the bundle never quoted** (CWH $16.50 vs $4.58, XLE $91.50 vs $63.05) stay dropped by the grounding gate, as designed. Level grounding covers the case where the ticker IS quoted but its level was guessed.

## Trade-offs

- **T1 (fills vs the analyst's price).** A corrected long limit buys higher than the analyst wrote. It is the level the analyst said it wanted, and it is reachable: on 10/06 it would have turned two non-fills into trades. "Entries and fills" measures whether corrected entries fill and how they do; if they lose more than they add, the tolerance is the knob.
- **T2 (coverage of setups vs concentration).** A setup whose only candidate is the same bet as a pick already on goes untested that day. Its observation would have been a duplicate.
- **T3 (risk manager input).** Kill-check 17's pair list now shows only robust pairs. It used to show any |corr| > 0.7, which on gap days included names linked only by one shared day.

## Acceptance (read by the system)

- **"Entries and fills"** shows a fill rate, any level corrections, and any validator drops by kind.
- **No shadow row** reads "no explicit reason".
- **No disowned label** reaches a test lane.
