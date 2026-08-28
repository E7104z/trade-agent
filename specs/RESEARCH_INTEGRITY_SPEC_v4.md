# RESEARCH INTEGRITY SPEC v4 — MULTIPLICITY / QUARANTINE / POPULATION / FRICTION

Status: implemented, 189 tests passing
Scope: `agent/stats.py`, `agent/friction.py`, `agent/research.py`,
`agent/population.py`, `agent/ablation.py`, `agent/integrity.py`,
`agent/migrations_v4.py`, `agent/loops/paired.py`, `prompts/weekly_miner.md`,
`prompts/morning_analyst.md`, ledger schema, tests.
Builds on: INDICATOR_LAYER_SPEC v1/v2, WEEKLY_MINER_SPEC v3. Supersedes nothing.

## 0. The problem this layer addresses

v1 and v2 gave the desk better inputs. v3 gave it a disciplined weekly mining
process. Neither addressed the question that decides whether any of it means
anything:

> Is the desk discovering edge, or discovering noise and describing it well?

The system already has real defenses — deterministic computation, n>=8
suppression, the shadow ledger, pre-registered open questions, `sim_version`,
lesson holdout tiers. This spec closes the six that remain.

**Design rule inherited from v1 §0 and extended:** deterministic code computes
every number and the LLM only interprets. v4 adds a corollary — **deterministic
code also decides what counts as a finding.** The miner is never asked to judge
significance, to decide whether a rule graduated, or to weigh its own search
width. Those are computed and handed to it as facts.

**New design rule (v4-1): measurement apparatus must not be able to break the
experiment.** Every entry point into this layer is exception-guarded and
degrades to a valid, reduced result. A research layer that can halt a trading
loop is worse than no research layer.

**New design rule (v4-2): additive only.** No existing function is rewritten, no
existing column changes meaning, no historical row is recomputed. Everything
that costs money or changes a number is config-gated and defaults off.

---

## 1. Gap 1 — the significance bar is a false-positive generator

### The problem

`ledger._stats_rows` computes a t-statistic per cell and the playbook's standing
bar is `|t| >= 1.5`. That is p ≈ 0.13 two-sided. The miner is shown ~14
dimensions × 2–12 levels = **60–90 comparisons every run**. At that width, with
zero true edge, roughly nine cells clear |t| >= 1.5 by chance per week.

The bar is not a filter. It is a random-finding generator, and it runs weekly.

### The arithmetic that constrains everything

At 4 picks/day with no-trade days and non-triggers, the desk books roughly
**450 triggered trades a year**. To detect +0.2R against ~1.2R per-trade
dispersion at 80% power requires n ≈ 283 — about eight months for **one**
marginal hypothesis. Meanwhile 12 setups × ~9 regime tags × 3 buckets = 324
cells averaging 1.4 trades per year each.

**This ledger can power one-dimensional splits on its common setups and
essentially nothing else, ever.** That is arithmetic, not opinion, and it is why
the dimension cap below is structural rather than advisory.

### The fix

`agent/stats.py` — dependency-free (stdlib `math` + `statistics` only; no scipy):

- `student_t_sf(t, df)` — exact two-sided tail via regularized incomplete beta
  (Lentz continued fraction). Verified against published critical values at
  df = 1, 2, 10, 20, 30 and the normal limit.
- `one_sample_t`, `welch_t` — Welch for A/B because bucket variances are not
  equal (a >2-ATR stop bucket disperses far wider than a <0.5 one).
- `benjamini_hochberg(pvals, q)` — FDR across the **entire batch**, not per
  dimension. Untestable cells pass through as `None` and are excluded from the
  multiplicity denominator `m`; counting them would inflate `m` and make the
  correction spuriously lenient.
- `bootstrap_ci_mean` — seeded, so a moving CI means the ledger changed, never
  the dice.
- `min_detectable_effect(n, std)` — **the most useful number on a young
  ledger.** A cell reporting +0.9R at n=6 with std=1.3 has an MDE of 1.49R: it
  cannot detect anything smaller than 1.49R, so its point estimate carries no
  information. Reporting MDE converts "looks promising" into "cannot see effects
  this small."
- `evaluate_search(...)` — scores every comparison, applies one correction, and
  marks `significant` only when FDR-surviving **and** n >= min_n **and**
  n_dimensions <= 2.

### Configuration

`research.fdr_q` (default 0.10), `research.min_n` (default 8).

---

## 2. Gap 2 — the context channel is ungated and un-ablatable

### The problem

There are two learning channels, and only one is defended.

| Channel | Path | Cadence | Gated by |
|---|---|---|---|
| Policy | playbook → analyst | weekly | n>=8, decay, holdout tiers |
| **Context** | **last 20 trades → every morning call** | **daily** | **nothing** |

`build_morning_memory` drops a raw per-trade table into every morning call. The
analyst will pattern-match on it. That is recency-weighted learning happening
entirely outside the machinery that guards the playbook — and it is the
most-read, least-controlled input in the system.

Worse, it could not be measured, because it was hard-wired into memory assembly.

### The fix

`agent/loops/paired.py::apply_context_mode` filters the assembled memory block
post-hoc — no change to `memory.py` — with modes `full`, `aggregate_only`
(strips the trade table), `none` (also strips the lesson bank). `daily_plans`
gains a `context_mode` column so every historical run is attributable.

**Fails loud, not quiet:** if the expected section header is not found, the text
is returned unchanged AND `run_challenger_arm` refuses to run the arm. A
silently un-ablated challenger would be compared against production as though it
differed — an experiment reporting a contrast that never existed is worse than
no experiment.

---

## 3. Gap 3 — `baseline_r` is conditioned on selection

### The problem

`baseline_r` (naive same-name open→close) answers a real question: did the
entry/exit machinery beat holding these names? It cannot answer the question
that decides whether the desk has edge:

> Did our name SELECTION beat the opportunity set we selected from?

If the gap population pays +0.3R mechanically and the desk averages +0.25R, the
desk is destroying value while showing a positive equity curve **and** a positive
`edge_vs_baseline_r`. Nothing in the ledger would catch that.

### The fix

`agent/population.py`:

- **Morning:** snapshot the entire gap-scan population to `population_universe`
  before any selection, flagged with `was_picked`. Index ETFs excluded (context,
  not candidates).
- **Evening:** two mechanical monkeys over every symbol — `momentum` (trade with
  the gap) and `fade` (against it), entry at the open, 1-ATR stop, exit at the
  close. R is denominated in ATRs, so it is directly comparable to desk R.
- **`selection_edge_r`** = desk avg R − the **better** monkey's avg R, with a
  Welch p-value. Compared against the better arm deliberately: comparing against
  the weaker one would let the desk claim credit for merely picking the right
  side of the tape, which the monkeys already reveal for free.

### Stated precision limit

The monkeys use **daily** bars, so within one bar the ordering of low and close
is unknown and a stop touch is booked conservatively. This **handicaps the
monkey** — it books stops on days price dipped and recovered.

That bias direction is deliberate and is the safe one. A desk that fails to beat
a handicapped monkey is a strong finding; a desk that beats it narrowly should
read the margin as an upper bound. Both readings are printed in the rendered
markdown so the miner cannot forget them.

---

## 4. Gap 4 — the risk manager's selection was never controlled

### The problem

Randomizing candidate selection conflates candidate **discovery** with candidate
**evaluation**. The isolating control is narrower: hold the pool fixed at what
the analyst proposed, then pick k at random instead of by the risk manager's
judgment.

### The fix

`agent/ablation.py` — and the data already exists. The shadow ledger stores every
rejected candidate with full plan levels, graded under the same simulator and
friction. Union with `picks` and you have the complete candidate set, already
graded, for the whole ledger history.

**This costs zero API calls and zero market data, and runs over your entire back
history today.**

Reported as the percentile of random portfolios the desk beat, with a one-sided
empirical p (with a +1 correction — a finite Monte Carlo is not entitled to
claim p = 0). Seeded, so the figure is reproducible.

### Stated scope limits

- **Unweighted.** Rejections carry no `size_r`. This measures selection, not
  sizing; sizing has its own counterfactual.
- **Time-stop asymmetry.** Rejections are graded without a `time_stop` while
  taken picks may exit on one. That **favors the random arm**, so a positive
  result is conservative.
- **`sim_version` respected.** Comparing across a semantics change would let a
  rule change masquerade as a selection finding.
- **Unmeasurable rows dropped, not zero-filled.** Zero-filling would pull every
  arm toward the mean and manufacture agreement.
- Sessions where every candidate was taken are excluded and counted — no
  alternative existed, so there was no selection to test.

---

## 5. Gap 5 — the playbook comparison is confounded

### The problem

Comparing ledger performance before and after a playbook rewrite is worthless.
Different weeks carry different regimes and opportunity sets. A v7→v8
improvement is indistinguishable from the market having been kinder.

### The fix

`agent/loops/paired.py` — run both arms on the **same morning**, same bundle,
same regime, same tape. Only arm A trades. Arm B is graded by the identical
simulator into `paired_arm_picks`.

Because the arms are paired by day, the comparison is a **paired-difference test
on per-session R**. Every confounder that varies by session cancels in the
difference. This is the only comparison in the entire system from which a causal
read is defensible.

### Safety

Arm B is never written to `picks`, never emailed, never shown as a plan. It
therefore cannot enter the equity curve, the streak calculation, the drawdown
circuit breaker, or any `stats_by` dimension. Arm B passes through the **same**
mechanical validator as arm A — an arm held to weaker standards is a handicap,
not a comparison. Any failure is caught; production is untouched.

### Cost

~2× a normal morning per paired session, throttled by
`paired.every_n_sessions`. Against the documented few-dollars-a-week baseline
this is a rounding error, and measuring the loop is the project.

---

## 6. Gap 6 — flat friction cannot support an edge claim

### The problem

`slippage_bps: 5` per side is reasonable for SPY and fiction for 3%+ premarket
gappers in the $10–50 band with sub-100M floats. If the desk's edge is near
+0.2R, friction is a first-order term. **An edge that exists only at 5bps and
dies at 15bps was never an edge — it was a fee estimate**, and the ledger
currently cannot distinguish the two.

### The fix

`agent/friction.py`:

```
bps = base + spread_k * atr_pct + impact_k * sqrt(participation)
participation = shares / adv20
shares = size_r * dollars_per_r / |entry - stop|
```

Square-root impact is the standard empirical form (Almgren). Shares are derived
from the plan — no new data required.

**Fails open toward the flat rate.** Missing ATR or ADV returns the configured
flat rate; the flat rate is a hard floor. A missing feed must never make a fill
look *cheaper*, which would be a silent, edge-flattering degradation — the exact
failure mode v1 §0.4's null-handling rule exists to prevent.

### Additive stress ladder

Each graded pick is re-simulated at 2× and 3× into `r_multiple_2x` /
`r_multiple_3x`. **The primary `r_multiple` is never recomputed**, so no
historical row changes meaning and no regrade is forced. A rule whose edge does
not survive the 2× rung is flagged.

### Calibration honesty

`spread_k` and `impact_k` are **priors, not measurements** — calibrated against
published equity TCA magnitudes, not this desk's own fills, because a paper
system has none. The stress ladder is the real defense; the point estimate is a
starting guess. Defaults land a 1.2%-ATR mega cap near the legacy 5bps, so the
model degrades toward current behavior on exactly the names 5bps was right for.

---

## 7. The quarantine — closing the promotion hole

`lessons.promoted_at` gives lesson-derived rules a real in-sample/out-of-sample
split. But the miner rewrites Setup Rules and Current Best Edge **wholesale**
from the full ledger each week, so a rule can enter the playbook without ever
passing through that machinery.

`agent/research.py` closes it with an explicit lifecycle:

```
DISCOVERED ──> QUARANTINED ──> VALIDATED
                    │
                    └────────> RETIRED
REJECTED_STRUCTURAL  (> 2 dimensions; recorded, never promotable)
```

**The load-bearing guard:** `record_rule_observation` refuses any observation
that does not strictly post-date `quarantined_at`. A rule discovered *because* it
looked good in a sample cannot be validated by that sample. This single rule is
the difference between "the rule worked" and "we found the rule because it
worked in the data we then tested it on."

Graduation requires **all** of: forward n >= target; minimum quarantine duration
(a burst of ten trades in three days is one regime, not a forward test); forward
p < alpha; **and the forward effect must carry the same sign as the discovery
effect** — a rule discovered as a winner that forward-tests as a significant
loser is falsified, not validated.

A rule that reaches its forward target without significance is **retired**, not
left waiting. Rules must be able to fail; a quarantine you can only graduate
from is a delay, not a test.

**Untestable ≠ failed.** A degenerate forward sample (zero variance, n<2) holds
the rule in quarantine rather than retiring it — retirement is effectively
permanent under the graveyard rule, so that would be an unrecoverable false
negative.

`rule_id_for` normalizes whitespace, case, and numeric literals before hashing,
so re-wording a rule with a fresh sample count does not reset its quarantine
clock. Laundering a stale rule through a rewrite is the most obvious way to
defeat a quarantine; it is closed at the identity layer.

### Write-time enforcement

`check_playbook_provenance` extends the existing `_check_wr_suppression`
contract. Any rule line making an edge claim must carry `[rule: Rxxxxxxxxxxxx]`,
and the referenced rule must be VALIDATED. Warnings, never exceptions — the
weekly loop must not crash after its expensive LLM call has completed.

---

## 8. The looks registry

A pre-registration registry (Q1–Q12) that ignores the unregistered search
running alongside it is documentation, not control. `research_looks` records
every comparison shown per run, and `looks_summary` reports cumulative width
across the ledger's life. The miner is told, in its own prompt, how wide its
search was and how many spurious findings that width would produce
uncorrected — because any single week's finding was selected from the entire
history, not just this week's batch.

---

## 9. Rollout order

Each step is independently shippable and revertable.

1. Migration + config — schema only, zero behavior change
2. **`run.py research` — the selection ablation over your whole back history,
   free, no hooks required. Do this first: highest information per unit of risk
   in the entire spec.**
3. Morning hook — population capture begins
4. Evening hook — population null accumulates
5. Weekly hooks + miner v4 — FDR and quarantine go live
6. Friction stress columns
7. `friction.model = "participation"` — **the only step that changes a number**
8. `paired.enabled = true` — the loop becomes measurable

Step 7 changes simulation semantics and therefore deserves a `sim_version` bump
and an explicit `run.py regrade`, per existing doctrine. Do not flip it silently
mid-experiment.

---

## 10. Deliberately not addressed

Stated so nothing here is mistaken for a completed claim:

- **Survivorship / delisting bias.** The gap scan sees only currently listed
  names. Real, unaddressed.
- **Data revisions and corporate actions.** Split/dividend adjustment is
  whatever the provider returns; no reconciliation.
- **Capacity.** The friction model prices impact but nothing tests whether the
  strategy's capacity is meaningful at scale.
- **Regime leakage.** The regime tag is computed from data through the prior
  close, but regime *labels* are assigned by thresholds chosen a priori and
  never validated.
- **`sim_version` auto-bumping.** A deliberate human decision, by design.

---

## 11. Failure handling

v1 §9 and v2 §10 apply verbatim, extended by design rule v4-1: **every** entry
point in this layer is exception-guarded and returns a degraded-but-valid
result. The population arm cannot fail the evening loop. The challenger arm
cannot fail the morning loop. A broken stats query drops one dimension rather
than the report. Provenance checking returns warnings, never raises.

The one place this layer is deliberately *strict* rather than forgiving: a
requested context ablation that did not apply refuses to run the arm. Everywhere
else, degrading is safe. There, degrading would silently corrupt the experiment.
