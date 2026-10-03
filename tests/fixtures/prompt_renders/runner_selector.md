<!-- prompt-version: 3 -->
You are selecting which intraday Runner candidates a trading desk should take today.

Every candidate below has ALREADY passed the same deterministic screen. None of
them is there by accident, and none of them is there because it scored well on
anything. Your job is not to re-check the screen — it is to choose among names
that all cleared it.

## What the screen already guarantees

Each row satisfied all of these on completed 5-minute regular-session bars:

- Price at least `min_vwap_pct` above session VWAP (typical-price basis)
- 9 EMA ascending — current 9 EMA above the 9 EMA three bars back
- Price above the 9 EMA
- Price inside the configured band, and a previous close of at least $4
- At least $5M of dollar volume in the previous session
- An operating company's stock: ETFs, ETNs, closed-end funds, notes and
  preferreds are screened out before you see the list
- No earnings inside the exclusion window, and a float inside the configured
  band — WHEN those are known. A blank `float_m` means the float could not be
  read, not that it passed

Relative volume is computed and shown, but there is no minimum: a low `rvol`
passed the screen. So "it is above VWAP" or "the EMA is rising" is not a reason
to prefer one row over another. It is true of every row.

## The columns

| Column | Meaning |
|---|---|
| `ticker` | Symbol |
| `industry` | The company's industry. Blank means unknown |
| `detection_time_et` / `detection_minutes` | When the VWAP/EMA conditions FIRST held. Earlier means the move has been running longer by now. |
| `price` / `price_tier` | The plan price: the close of the last completed 5-minute bar when the scan recorded the name, and its band |
| `vwap_pct_above` | How far above session VWAP at that bar |
| `ema9_slope` | Absolute 9-EMA slope, in dollars, over three bars |
| `rvol` | Session volume so far vs. the previous session's volume scaled to this time of day |
| `trend_consistency` | Percent of session bars that closed green |
| `change_pct` | Move from the previous close to the plan price |
| `float_m` | Free float, millions of shares |
| `stop_dist` / `stop_dist_pct` | Distance from the plan price to the 9-EMA stop |
| `target_price` | Plan price plus twice the stop distance |

`ema9_slope` is in dollars, not percent, so it is not comparable across price
tiers on its own — a $0.04 slope means something different on a $6 stock than
on a $45 one. `stop_dist_pct` is the normalized read.

## How to choose

Pick at most 10. You may pick fewer. You may pick none, and doing so
is a real answer — if the field looks uniformly poor, say so by returning an
empty list rather than filling the slots.

At most 2 picks may share one `industry`. Names in one
industry on one morning move on the same tape and the same news; several of
them are one bet wearing several tickers. The desk enforces this limit after
you answer by dropping your lowest-ranked excess picks, so choose the ones you
would keep.

Reason from the trade geometry in front of you:

- A stop so tight that normal noise takes it out is a worse plan than the R
  multiple suggests, because the R is measured against a distance the stock
  covers routinely.
- A stop so wide that the 2R target implies an implausible move is the same
  problem inverted.
- A name detected very early has had longer to extend; a name detected late has
  less session left to work.

Do NOT invent or estimate any number that is not in the table. Do not compute
indicators. Do not reason about news, filings, or anything you believe you know
about these companies from memory — you cannot verify it here, and the desk
grades on what was knowable at decision time.

Nothing in this prompt tells you which feature values have historically worked,
because that has not been established on this desk's own evidence yet. Do not
import a prior from anywhere else.

## Context

Regime today: uptrend/risk_on/lowvol

## Runner Lane

(no validated runner rules yet — do not assume any)

## Candidates

Rows are sorted alphabetically by ticker. The ordering carries no information
and is deliberately not a ranking.

ticker | industry
AAA | Banks - Regional

## Output

Reply with ONLY this JSON object. No prose, no markdown fences, no preamble.

{"picks": [{"ticker": "ABC", "rank": 1, "conviction": 3, "rationale": "one sentence"}]}

- `rank` — 1 is your strongest, ascending, no ties, no gaps
- `conviction` — integer 1 to 5. It is RECORDED for research and does NOT change
  position size; every pick is sized identically until this setup has cleared
  the integrity layer on this desk's own forward evidence.
- `rationale` — one sentence, naming the specific values that decided it
- Every ticker must appear in the table above. A ticker that does not is dropped.
- To pick nothing, return {"picks": []}
