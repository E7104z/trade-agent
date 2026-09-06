# Trading Agent Playbook
<!-- version: 4 -->

> Rewritten by the weekly miner. Rules carry sample counts AND a last_confirmed
> session date. n<8 is provisional and WR/avgR are suppressed below n=8 (raw
> trade list only — see Ledger Reference). A rule with no confirming observation
> in >= 30 sessions is auto-demoted to UNVALIDATED regardless of prior n.
> UNVALIDATED = prior, not edge. Retired rules live at the bottom and must not
> be re-proposed without new evidence that post-dates their retirement.

## Reading This File (for the morning agent)

- **n** = number of REALIZED trades supporting the rule. This is ground truth.
- **last_confirmed** = session date of the most recent confirming observation.
  If it is >= 30 sessions ago, treat the rule as UNVALIDATED even if n is high.
- **WR / avgR are shown ONLY at n >= 8.** Below that you get the raw trade list.
  Do not infer a win rate from a short list. A 1-for-1 rule is noise, not skill.
- **[UNVALIDATED prior]** = a belief carried from inception with zero or
  sub-threshold evidence. You may use it as a weak default; you may not cite it
  as edge, and you must not size up on it.
- Confidence tiers: none (n=0) < provisional (1 <= n < 8) < low (8 <= n < 20)
  < moderate (20 <= n < 40) < established (n >= 40). No rule is "high."
- **Current Best Edge** is the desk's one-paragraph standing answer to "where is
  our edge right now" — hunt it first. If it says NO VALIDATED EDGE YET, the
  hunt is the provisional candidates it names.

## Current Best Edge

> The desk's standing answer to "where is our edge right now?" — rewritten from
> scratch by the weekly miner every week. Pure synthesis: nothing may appear
> here that isn't already in Setup Rules / Regime Rules / the ledger. The
> morning agent hunts THIS first; everything else is exploration.

**NO VALIDATED EDGE YET.** Zero graded picks means the desk cannot distinguish
edge from variance anywhere — long vs short, setup vs setup, regime vs regime.
The track record is too young to say anything.

The bar for calling anything edge: (a) n >= 8, (b) avgR beats the naive
same-name open->close baseline, and (c) statistically distinguishable from
zero (t >= 1.5). Positive R that a monkey holding the same names would also
have made is NOT edge.

Leading provisional candidates (candidate, not edge):

- catalyst_momentum longs in uptrend/risk_on — n=1 realized; needs n >= 8 to validate.
- gap_fade against dilution-driven gaps — n=1 realized; entry mechanics under
  Open Question #1 (1/8 progress); needs n >= 8 to validate.

Until a candidate validates, the edge IS the process: defined risk, fresh
catalysts, regime alignment, and ruthless collection of the evidence that will
reveal where the real edge lives.

## Core Principles

1. Risk is defined before entry, always. A trade without a numeric stop does not exist.
2. Process over outcome. Grades and lessons attach to decision quality, not P&L luck.
3. No-trade is a position. Chop, event-pinned mornings, and data vacuums are stand-down conditions.
4. Fresh catalyst > stale story. If the news is three sessions old, the easy money already left.
5. Regime alignment beats chart quality.
6. The ledger is the referee. When intuition and the ledger disagree, the ledger wins.
7. Rejected trades are data. What the playbook declines is tracked in the Shadow Ledger and judged as rigorously as what it takes. A filter that never costs anything is untested, not correct.
8. Edge decays by default. A rule is provisional until re-confirmed, and re-confirmation expires. Silence is not confirmation.
9. The desk is loyal to edge, not to a market view. Long and short are equally honorable; hunt whichever side the evidence favors. Current Best Edge is the standing statement of where that hunt starts.

## Sizing Policy

- Base unit: 1R = configured dollars_per_r.
- Conviction 5 + fresh verifiable catalyst may take max size (1.5R). Conviction <= 3 takes minimum (0.25-0.5R).
- Losing streak >= 3: cap 0.5R, minimum conviction 4. [UNVALIDATED prior]
- Drawdown circuit breaker: trailing 10 triggered trades <= -4.0 weighted R ->
  capital preservation mode (max 1 pick, 0.25R, conviction >= 4). [mechanical —
  enforced in code, not by judgment]
- Total open plan risk <= 3R.
- Correlated picks share a combined 1.5R cap. [n=1 supporting: AMD/NVDA overlap cost -0.5 weighted R; last_confirmed v1]
- **Thematic exposure cap:** if 2+ picks express the same underlying macro thesis
  (e.g. "AI datacenter power," "GLP-1 demand," "rate-cut beneficiaries") they
  share ONE 1.5R risk unit even when statistically uncorrelated. The adversarial
  reviewer must name the shared thesis explicitly or certify there is none. [UNVALIDATED prior]

## Setup Rules

- catalyst_momentum: catalyst must be <24h old and verifiable via filing or primary source. [confidence: provisional, n=1, last_confirmed v1 — WR/avgR suppressed, see ledger]
- gap_fade: only against gaps >5% driven by dilution/offerings WITHOUT fresh operating substance; enter on the FIRST FAILED BOUNCE, never the open print. [confidence: provisional, n=1, last_confirmed v1 — WR/avgR suppressed, see ledger]
- gap_go: only when gap <= 12% and holding above premarket VWAP into 9:45. [confidence: none, n=0, UNVALIDATED prior]
- sympathy: do NOT take the follower when the leader is already held at max conviction — correlation without information. If taken standalone, half size, leader-linked invalidation. [confidence: provisional, n=1, last_confirmed v1 — WR/avgR suppressed, see ledger]
- breakout: require >= 3 prior touches; prefer retest entry. [confidence: none, n=0, UNVALIDATED prior]
- short_squeeze: catalyst + borrow evidence required; 0.25R lottery sizing only. [confidence: none, n=0, UNVALIDATED prior]

## Regime Rules

- uptrend/risk_on: favor catalyst_momentum and gap_go longs; shorts only with idiosyncratic dilution/downgrade catalysts. [n=3 week-one observations, provisional, last_confirmed v2]
- downtrend/risk_off: favor breakdown_short, gap_fade; longs only as oversold_reversal with tight time_stops. [n=0, UNVALIDATED]
- chop: halve sizes; prefer no_trade absent a fresh idiosyncratic catalyst. [n=0, UNVALIDATED]
- VIX > 28: max 2 picks, 0.5R each, no overnight holds. [n=0, UNVALIDATED]

## Do-Not-Trade Conditions

- FOMC days: no new entries after 13:00 ET.
- CPI/NFP mornings: no entries before 09:45 ET.
- First 15 minutes: no entries except playbook-supported open-type plans.
- >= 3 data sources missing: max 1 pick or stand down.

## Calibration (tracked every evening, reported every week)

> Answers "does the agent's confidence mean anything?" Populated by the evening
> grader. Until n >= 20 total graded picks this table is displayed but not acted on.

| Conviction | n | realized WR | realized avgR | verdict |
|-----------:|--:|:-----------:|:-------------:|:--------|
| 5 | 0 | — | — | insufficient data |
| 4 | 0 | — | — | insufficient data |
| 3 | 0 | — | — | insufficient data |
| 2 | 0 | — | — | insufficient data |
| 1 | 0 | — | — | insufficient data |

Calibration verdict: **not yet computable** (0 graded picks; threshold is 20).

## Shadow Ledger Summary (rejected candidates)

> Every idea the two-stage morning call generated and KILLED is logged with its
> rejection reason and EOD outcome. The weekly miner reads this to find filters
> that cost more than they save. This section is the miner's running summary;
> raw rows live in the ledger DB.

- Trailing sessions with shadow data: 2
- Total candidates rejected: 16
- Rejections that would have won (EOD favorable to thesis): 8
- Rejections that would have lost/scratched: 8 (1 loss, 7 scratch)
- Net forgone R (positive = filters cost us): **+16.30R**
- Captured R by taken trades over same window: **+0.00R**
- Most expensive rejection reason: R:R < 2.0 requirement (cited in 16/16 rejections)

**Filters flagged for review** (forgone R > captured R over trailing 20 sessions):

- **R:R >= 2.0 threshold**: +16.30R forgone vs +0.00R captured. However, shadow outcomes assume perfect fills the taken book would not have gotten. Do NOT auto-loosen. See Open Question #7 for pre-registered test.

## Open Questions (pre-registered experiments)

> Each carries a threshold and the exact data being collected. The weekly miner
> MUST report progress against every open item, even if progress is zero.

1. **gap_fade entry mechanics** — does failed-bounce entry beat open-print entry?
   Collecting BOTH prices on every gap_fade candidate. Threshold: n >= 8. **Progress: 1/8.** (NBIS: planned-entry R=0.27, open-print R=-0.028)
2. **sympathy standalone** — salvageable, or edge-free? Collecting standalone-only observations. Threshold: n >= 8. **Progress: 0/8.**
3. **conviction calibration** — see Calibration table. Threshold: n >= 20 graded picks. **Progress: 0/20.**
4. **gap-size chase threshold** — is 15% the right cutoff? Collecting gap-size buckets on every gap trade. Threshold: n >= 20 gap trades. **Progress: 0/20.**
5. **shadow-ledger filter cost** — which rejection reasons forgo the most R? Collecting all rejections. Threshold: 20 sessions of shadow data. **Progress: 2/20 sessions.** (16 rejections scored; net forgone +16.30R after risk-floor correction)
6. **stop placement cost** — of stopped-out trades, how often does the target print later the same session (stops inside the noise)? Collecting target_hit_after_stop + eod_r on every stopped trade (deterministic). Threshold: n >= 10 stopped trades. **Progress: 0/10.**
7. **R:R threshold test** — NEW. Does 1.8 R:R outperform 2.0 R:R on a risk-adjusted basis? Shadow ledger shows 16/16 rejections cite R:R < 2.0; 8 would have won. Collecting: (a) taken trades at R:R >= 2.0, (b) rejected trades at 1.8 <= R:R < 2.0 with shadow outcomes. Compare avgR at n >= 20 each bucket. Threshold: 20 observations per bucket. **Progress: 0/20 taken at >= 2.0; 16/20 rejected at 1.8-2.0 (shadow).** Do NOT loosen filter until forward evidence supports it.

## Retired Rules (graveyard — do not re-propose without post-retirement evidence)

> A rule lands here when it is falsified or auto-demoted twice. Re-proposal
> requires new confirming observations dated AFTER the retirement date.

_None retired yet._

---

## Changelog (v4)

- **Decay check:** No rules auto-demoted (< 30 sessions since last confirmation for all rules).
- **Shadow Ledger Summary:** Populated with first real data — 16 rejections over 2 sessions, 8 would-have-won, net forgone +16.30R vs +0.00R captured. R:R >= 2.0 filter flagged for review.
- **Open Question #7 added:** Pre-registered test of R:R 1.8 vs 2.0 threshold. The filter is expensive but shadow outcomes assume perfect fills; test deliberately, one variable at a time.
- **Open Question progress updated:** Q1 at 1/8 (one gap_fade observation); Q5 at 2/20 sessions (16 rejections scored).
- **L9 contradiction noted:** Lesson claimed no-trade saved 0.60R; corrected data shows +16.90R forgone that session. Lesson premise under review but not retired (single session, data correction may have been the error).
- **No setup-rule changes:** All rules remain at their prior confidence tiers; zero graded picks means no new confirming or disconfirming evidence.
- **Current Best Edge:** Unchanged — NO VALIDATED EDGE YET. Leading candidates remain catalyst_momentum and gap_fade at n=1 each.

---

## Changelog (v4)

- Decay check: All rules < 30 sessions since last_confirmed; no auto-demotions.
- catalyst_momentum: unchanged (n=1, last_confirmed v1, no new observations)
- gap_fade: unchanged (n=1, last_confirmed v1, no new observations)
- gap_go: unchanged (n=0, UNVALIDATED)
- sympathy: unchanged (n=1, last_confirmed v1, no new observations)
- breakout: unchanged (n=0, UNVALIDATED)
- short_squeeze: unchanged (n=0, UNVALIDATED)
- uptrend/risk_on regime rule: unchanged (n=3, last_confirmed v2, no new observations)
- Shadow Ledger Summary: NEW — populated from 2 sessions of data (16 rejections, +16.30R forgone)
- Open Question #7: NEW — pre-registered R:R threshold test
- Open Question #1: Progress 0/8 -> 1/8 (one gap_fade observation logged)
- Open Question #5: Progress 0/20 -> 2/20 sessions (16 rejections scored)
