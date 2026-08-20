# Trading Agent Playbook
<!-- version: 3 -->

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

## Core Principles

1. Risk is defined before entry, always. A trade without a numeric stop does not exist.
2. Process over outcome. Grades and lessons attach to decision quality, not P&L luck.
3. No-trade is a position. Chop, event-pinned mornings, and data vacuums are stand-down conditions.
4. Fresh catalyst > stale story. If the news is three sessions old, the easy money already left.
5. Regime alignment beats chart quality.
6. The ledger is the referee. When intuition and the ledger disagree, the ledger wins.
7. Rejected trades are data. What the playbook declines is tracked in the Shadow Ledger and judged as rigorously as what it takes. A filter that never costs anything is untested, not correct.
8. Edge decays by default. A rule is provisional until re-confirmed, and re-confirmation expires. Silence is not confirmation.

## Sizing Policy

- Base unit: 1R = configured dollars_per_r.
- Conviction 5 + fresh verifiable catalyst may take max size (1.5R). Conviction <= 3 takes minimum (0.25-0.5R).
- Losing streak >= 3: cap 0.5R, minimum conviction 4. [UNVALIDATED prior]
- Total open plan risk <= 3R.
- Correlated picks share a combined 1.5R cap. [n=1 supporting: AMD/NVDA overlap cost -0.5 weighted R; last_confirmed v1]
- **Thematic exposure cap:** if 2+ picks express the same underlying macro thesis
  (e.g. "AI datacenter power," "GLP-1 demand," "rate-cut beneficiaries") they
  share ONE 1.5R risk unit even when statistically uncorrelated. The adversarial
  reviewer must name the shared thesis explicitly or certify there is none. [UNVALIDATED prior — new in v3]

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

Calibration verdict (weekly miner fills): **not yet computable.**
If conviction 4-5 does not out-R conviction 2-3 at n >= 20, conviction is noise
and sizing-by-conviction must be suspended until recalibrated.

## Shadow Ledger Summary (rejected candidates)

> Every idea the two-stage morning call generated and KILLED is logged with its
> rejection reason and EOD outcome. The weekly miner reads this to find filters
> that cost more than they save. This section is the miner's running summary;
> raw rows live in the ledger DB.

- Total candidates rejected to date: 0
- Rejections that would have won (EOD favorable to thesis): 0
- Rejections that would have lost/scratched: 0
- Most expensive rejection reason (net forgone R): none yet
- Filters flagged for review (forgone R > captured R over trailing 20 sessions): none yet

## Open Questions (pre-registered experiments)

> Each carries a threshold and the exact data being collected. The weekly miner
> MUST report progress against every open item, even if progress is zero.

1. **gap_fade entry mechanics** — does failed-bounce entry beat open-print entry?
   Collecting BOTH prices on every gap_fade candidate. Threshold: n >= 8. Progress: 0/8.
2. **sympathy standalone** — salvageable, or edge-free? Collecting standalone-only observations. Threshold: n >= 8. Progress: 0/8.
3. **conviction calibration** — see Calibration table. Threshold: n >= 20 graded picks. Progress: 0/20.
4. **gap-size chase threshold** — is 15% the right cutoff? Collecting gap-size buckets on every gap trade. Threshold: n >= 20 gap trades. Progress: 0/20.
5. **shadow-ledger filter cost** — which rejection reasons forgo the most R? Collecting all rejections. Threshold: 20 sessions of shadow data. Progress: 0/20 sessions.

## Retired Rules (graveyard — do not re-propose without post-retirement evidence)

> A rule lands here when it is falsified or auto-demoted twice. Re-proposal
> requires new confirming observations dated AFTER the retirement date.

_None retired yet._

---

## Changelog (v3)

- Added Core Principles 7 (rejected trades are data) and 8 (edge decays by default).
- Added **last_confirmed** dating to every rule; rules stale >= 30 sessions auto-demote to UNVALIDATED.
- **Suppressed WR/avgR display below n=8** across all setup rules to stop the morning agent anchoring on small-sample win rates; raw ledger list substitutes.
- Added **Thematic exposure cap** to Sizing Policy — shared-thesis picks share one risk unit even when uncorrelated; adversarial reviewer must name the thesis or certify none.
- Added **Calibration** section — conviction vs realized R, populated by evening grader, promoted from Open Question to permanent tracked metric.
- Added **Shadow Ledger Summary** — rejected candidates tracked and judged; feeds a new Open Question on filter cost.
- Converted **Open Questions** into pre-registered experiments with explicit thresholds and running progress the miner must report against.
- Added **Retired Rules** graveyard to prevent re-proposal of falsified rules.
- No setup-rule edits and no new setup types: existing rules remain sub-threshold; widening the surface before n>=8 spreads thin data thinner.
