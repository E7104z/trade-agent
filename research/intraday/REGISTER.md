# Intraday strategy search: pre-registration



Written 2026-10-07 before any day-trade strategy below was run on intraday data. Jason asked

for day trades only (flat by 15:55, nothing overnight), tested "this same way until you find

an edge", and then a scanner. The way to search many strategies without fooling ourselves is

to fix in advance what counts. That is this file.



## Data



* Daily bars 2021-03 → 2026-10, split-adjusted, SIP: every active common stock, the 1,048

  delisted common stocks Alpaca still serves (a partial survivorship repair), and 18 ETFs.

* `open15`: the 09:30/09:35/09:40 five-minute bars of every stock liquid the day before

  (price ≥ $2, 20-day dollar volume ≥ $3M).

* `day5`: full-session five-minute bars of each day's in-play set, which is chosen only from

  what was known by 09:35. It holds liquid gappers ≥ 2%, the top 80 by opening-bar RVOL,

  the ORB study's own top 40, the top 100 by dollar volume, yesterday's ±8% movers, and ETFs.



## Periods



* **Discovery:** 2022-01-03 → 2024-06-28. Every test below is run here.

* **Hold-out:** 2024-07-01 → 2026-10-06. It is touched once, only by a strategy that passes

  discovery. The hold-out cannot rescue or tune anything.



## Simulation conventions (all tests)



* 5-minute bars. Bar k starts at 09:30 + 5k min. Every position is closed at the 15:55 bar's

  open, which is the desk's flatten.

* A market entry fills at the bar open. A stop-entry fills at the trigger, or at the open if

  the bar gapped through it. A limit fills at the limit, or at the open if the bar opened

  through it.

* When the stop and target are both inside one bar, the stop wins. On the bar a stop-entry or

  limit fills, the stop is checked and the target is not, because the order inside the bar

  is unknown.

* Cost per side is max(5 bps, $0.01/share), charged on entry and exit. A ×3 stress is reported.

* R is the profit on planned prices less costs, divided by |entry − planned stop|.

* The unit is the session: a test's daily value is the mean R of that day's trades. The

  t-statistic is taken across sessions, so 30 names on one day count as one day, not 30.



## Acceptance (fixed now)



1. **Discovery:** |t| ≥ 3.18. This is Bonferroni for the 34 tests below at two-sided 0.05.

   A significantly negative gross result nominates the reverse trade, priced as

   −gross − cost.

2. Same sign in each discovery year (2022, 2023, H1 2024). It must also stay above zero at

   ×3 cost.

3. **Hold-out:** one look. Net mean > 0 with t ≥ 1.65 (one-sided 0.05).

4. Any refinement tried after round one is logged below. It raises N, and with it the

   discovery threshold, for everything that comes after.



## The 34 tests



| # | Family | Why it might work | Variants (tests) |

|---|---|---|---|

| S1 | Opening-range breakout on stocks in play (Zarattini, Barbon & Aziz 2024) | Unusual opening volume marks real information, and price follows the side that wins the open. Filter: price ≥ $5, ≥ 1M shares, ATR ≥ $0.50, RVOL5 ≥ 1, top 20 by RVOL5. Direction comes from the first candle. | OR 5 min / 15 min × stop 10% ATR / opposite end of the range (4) |

| S2 | Gap fade | Overreaction to news overnight, with liquidity providers fading the open. Universe: price ≥ $5, ADV ≥ $10M, gap ≥ 3%. | Enter at the open with a 1-ATR stop / enter at 09:35 after a first bar against the gap with the stop at its extreme × fade-up (short) / fade-down (long) (4) |

| S3 | Gap-and-go | News gaps continue once the open confirms them. The first bar is in the gap's direction; break its extreme; stop at the other end. | All / RVOL5 ≥ 2 × long / short (4) |

| S4 | Index intraday momentum | (a) Gao, Han, Li & Zhou 2018: the return from the prior close to 10:00 predicts the last half hour (15:30→15:55 here). (b) Zarattini, Aziz & Barbon 2024: a "noise area" breakout at half-hour marks, trailed by VWAP. | (a), (b) × SPY, QQQ (4) |

| S5 | Late-day momentum | Trend days finish at the high, through institutional completion and short covering. At 14:30: ≥ 1 ATR from the open, above VWAP, within 0.25 ATR of the high. | Stop 0.5 ATR / VWAP × long / short (4) |

| S6 | First VWAP pullback on a trend day | Buyers defend VWAP on trend days. Every bar from 09:45 to 10:25 closes on one side of VWAP, the move is ≥ 0.75 ATR by 10:25, and the first touch of VWAP between 10:30 and 14:30 fills a limit there. Stop at VWAP ∓ 0.3 ATR. | Exit at the close / 2R target × long / short (4) |

| S7 | Large-cap intraday reversal | Liquidity-driven moves in large caps revert within the day. Top 100 by ADV: the 5 weakest and 5 strongest against SPY since the open are traded against the move, with a 1-ATR stop. | Ranked at 10:00 / 11:00 × long / short (4) |

| S8 | Opening-drive fade (no gap) | A ≥ 1 ATR move in the first 15 minutes without news (gap < 2%) overshoots. Enter at 09:45; stop beyond the 15-minute extreme + 0.1 ATR. | Long / short (2) |

| S9 | Day-2 continuation | Yesterday's ±8% mover on ≥ 3× volume keeps institutional follow-through. Break of today's first-bar extreme in yesterday's direction; stop at the other end. | Long / short (2) |

| S10 | VWAP overextension fade | ≥ 1.5 ATR from VWAP at 11:00 is stretched. Stop beyond the day's extreme + 0.1 ATR. | Long / short (2) |



Total N = 34 → discovery threshold |t| ≥ 3.18.



## Refinement log



Every entry here raises N, except pure measurement fixes, which are marked as such.



* **2026-10-07, measurement fixes before the discovery run (N unchanged).** A smoke run on

  Q1 2022 checked the engine and exposed three ways the *measurement* was wrong. None of them

  changes a rule or a parameter.

  1. **S1 runs on 1-minute bars.** On 5-minute bars the fill bar's range almost always covers

     a 10%-of-ATR stop, and the stop-first convention then books nearly every trade as a loss

     (8% wins). That is a resolution artifact. The opening range is the first 5 (or 15)

     one-minute bars, and the rest of S1 is exactly as registered.

  2. **S4 R unit.** S4's trailing exit has no fixed stop, and the boundary can sit a hair from

     the fill, which made R explode (+10R means). R for S4a and S4b is now return / (0.25 ×

     ATR14). This is a unit only; the trading rule is unchanged.

  3. **ETF cost.** For SPY and QQQ the cost per side is max(1 bp, $0.01). Their spread is one

     cent on $400+, so 5 bps overstated cost about tenfold. Stocks keep max(5 bps, $0.01).


* **2026-10-07, after the discovery run: refinement R1 (N 34 → 35, threshold |t| ≥ 3.19).**
  S4b_QQQ was the one test to pass. The day-mean statistic gave t 3.67; the daily *sum*,
  which is what a trader taking every signal earns, gave t 2.39. Both are reported from here
  on, and the register's day-mean statistic stays the decision rule.
  R1 applies the registered S4b rule unchanged to the basket of all 18 ETFs as ONE test (the
  day's value = the mean R of every ETF trade that day). It is declared before it is run.
  The motive is throughput: a single-instrument rule trades about 1.5 times a day.
  **R1 result: FAILS.** The basket day-mean was −0.073R, t −3.09 (daily sum t −1.79), negative
  in every year. The rule is positive only on QQQ, XLK and SPY, the large-cap tech complex, and
  loses on IWM, DIA and most sectors. No ETF other than QQQ is taken forward.

## Discovery verdicts (2026-10-07)

* **Finalist:** S4b_QQQ. Day-mean net +0.235R (0.25-ATR units), t 3.67; daily-sum t 2.39;
  per-trade net +0.115R; positive in 2022, 2023 and H1 2024; +0.143 at ×3 cost. Longs carry it,
  and they were positive in the 2022 bear market (+0.163 a trade); shorts are about zero.
  Parameter neighbours (lookback 10/20, checks every 15/60 min, band or VWAP trail, σ ×0.8/1.2)
  are all positive, with daily-sum t between 1.5 and 2.5.
* **Every other test failed.** S4b_SPY got t 3.08. S2_conf_short got gross t 3.25 but costs
  ate it (net t 0.5). The S1 ATR-stop ORB's gross loss was significant (t −8), but reversing
  it is still negative after costs.

## Hold-out verdict (2026-10-07, one look)

* **S4b_QQQ: PASSES the registered criterion.** Day-mean net +0.192R, t 2.38 (bar 1.65); positive
  in H2 2024 (+0.392), 2025 (+0.135) and 2026 (+0.127); +0.084 at ×3 cost.
* **Economically marginal.** A trader taking every signal earns the daily sum: +0.068R per
  trade, which is about +2.8 bps of QQQ after costs; t 1.14, not significant. The best 10 days
  made +59.8R; the other 493 trades made −25.6R. Longs +0.084, shorts +0.054 a trade. Most of
  the profit came in H2 2024 (+23R of +34R).
* **Decision:** the evidence earns a forward test at small size, not confidence. The rule goes
  to the live system unchanged (the registered parameters) as a paper lane, and the system's
  sequential forward test decides. The hold-out is spent: no variant of this rule may be
  chosen on 2024-07 → 2026-10 data from here on.

## Correction (2026-10-07, after an outside review)

**The registered decision statistic was the wrong one for a single-instrument rule.** The
day-mean of trade R gives a day with two whipsaw losses the weight of one, so it flatters a rule
that trades several times on choppy days. The right primary unit is the **daily net return on
capital, with zero-trade days included**. On that unit:

| QQQ noise rule | t | Sharpe | total (1× notional, not compounded) | max DD | ex best 5 days | ex best 10 days |
|---|---|---|---|---|---|---|
| Discovery 2022-01 → 2024-06 | 2.16 | 1.37 | +29.8% | −3.9% | +17.1% | +7.3% |
| Hold-out 2024-07 → 2026-10 | **1.20** | 0.80 | +16.3% | −7.5% | **−1.5%** | −9.0% |
| All 4.75 years | 2.38 | 1.09 | +46.1% | −7.5% | +26.2% | +15.2% |

The status is therefore **positive in the hold-out but NOT confirmed**. The hold-out profit
depends entirely on its best 5 days. Every test from here on uses the daily-capital statistic,
including flat days, with a moving-block bootstrap.

Data check: the last session is 2026-10-06, with 78 of 78 bars. There is no partial session.

Where the profit comes from (all years; explanation diagnostics, which never change the rule):
* Trades held to the 15:55 flatten made +194%; trades exited earlier lost −148%.
* High-volatility tercile +25.6%, mid +9.0%, low +11.6%.
* Longs +38.6%, shorts +7.5%.
