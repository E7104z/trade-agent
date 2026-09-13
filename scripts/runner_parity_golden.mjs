/**
 * RUNNER PARITY - GOLDEN VECTOR GENERATOR
 *
 * Runs the DTO Trading App's ACTUAL JavaScript over a fixture's bar arrays and
 * emits the `expected` block that scripts/runner_parity.py diffs against the
 * Python port (RUNNER_PORT_SPEC_v1 0.4).
 *
 * Why this exists: the spec's original plan was to dump the browser scanner's
 * `passing` map out of localStorage. That works, but it is manual, it cannot
 * run in CI, and it only ever covers days somebody remembered to export. The
 * DTO modules are pure ES modules, so Node can import them directly with a
 * small localStorage/DOM shim - which makes the golden vectors reproducible
 * and lets the parity gate run in the normal test suite.
 *
 * WHAT IS DTO'S REAL CODE (the part where port risk actually lives):
 *   - checkRunnerConditions   (js/runner-scanner.js, exported)
 *   - calculateVWAP           (js/indicators.js, exported)
 *   - calculateRVOL           (js/indicators.js, exported)
 *   - expectedVolumeFraction  (js/indicators.js, exported)
 *   - EXCLUDED_TICKERS        (js/constants.js, exported)
 *   ...including, transitively, the SMA-seeded calcEMA the scanner uses.
 *
 * WHAT IS TRANSCRIBED BY HAND (and is therefore the one thing to re-read the
 * source for if a diff looks wrong): the scan loop's gate ORDER and its
 * counter assignment, js/runner-scanner.js:1096-1176. That loop is inline in
 * a non-exported async function that fetches from the network, so it cannot
 * be called directly. The transcription is line-referenced below.
 *
 * Usage:
 *   node scripts/runner_parity_golden.mjs <fixture.json> [--dto-root <path>]
 *
 * Reads the fixture, fills in `expected`, writes it back in place.
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

// ── Browser shims ────────────────────────────────────────────────
// js/alpaca.js and friends touch localStorage/document at module scope. None
// of the functions used below read them, but the imports must not throw.
globalThis.localStorage = {
  _d: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
};
globalThis.window = globalThis;
globalThis.document = {
  addEventListener() {},
  querySelector() { return null; },
  querySelectorAll() { return []; },
  getElementById() { return null; },
  createElement() { return { style: {}, appendChild() {}, setAttribute() {} }; },
};

// ── Args ─────────────────────────────────────────────────────────
const argv = process.argv.slice(2);
const fixturePath = argv.find(a => !a.startsWith('--'));
if (!fixturePath) {
  console.error('usage: node runner_parity_golden.mjs <fixture.json> [--dto-root <path>]');
  process.exit(2);
}
const rootFlag = argv.indexOf('--dto-root');
const DTO_ROOT = rootFlag !== -1
  ? argv[rootFlag + 1]
  : (process.env.DTO_ROOT || 'C:/Users/Quantum Lab/Desktop/Trading');

const jsUrl = name => pathToFileURL(path.join(DTO_ROOT, 'js', name)).href;

let scanner, indicators, constants;
try {
  scanner    = await import(jsUrl('runner-scanner.js'));
  indicators = await import(jsUrl('indicators.js'));
  constants  = await import(jsUrl('constants.js'));
} catch (e) {
  console.error(`Could not load DTO modules from ${DTO_ROOT}: ${e.message}`);
  console.error('Pass --dto-root <path to E7104z/trading checkout> or set DTO_ROOT.');
  process.exit(2);
}

const { checkRunnerConditions } = scanner;
const { calculateVWAP, calculateRVOL } = indicators;
const { EXCLUDED_TICKERS } = constants;

// ── ET minutes, matching the JS idiom used throughout DTO ─────────
function etMinutes(t) {
  const et = new Date(new Date(t).toLocaleString('en-US', { timeZone: 'America/New_York' }));
  return et.getHours() * 60 + et.getMinutes();
}

// ── Fixture ──────────────────────────────────────────────────────
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'));
const minVwapPct = fixture.min_vwap_pct ?? 0.5;
const tickers = fixture.tickers || {};

const passing = {};
const rejectReasons = {};
let noBars = 0, tooFewBars = 0, rejectedPrice = 0, rejectedRvol = 0, rejectedCond = 0;

// Deterministic iteration order so counters are reproducible.
for (const ticker of Object.keys(tickers).sort()) {
  const row = tickers[ticker];
  const tickerBars = row.bars;

  // :1097  if (!tickerBars) { noBars++; continue; }
  if (!tickerBars || tickerBars.length === 0) { noBars++; rejectReasons[ticker] = 'no_bars'; continue; }

  // :1099  if (tickerBars.length < 10) { tooFewBars++; continue; }
  if (tickerBars.length < 10) { tooFewBars++; rejectReasons[ticker] = 'too_few_bars'; continue; }

  // :1102-1108  market-hours filter, et >= 570, NO upper bound
  const marketBars = tickerBars.filter(b => etMinutes(b.t) >= 570);
  if (marketBars.length < 10) { tooFewBars++; rejectReasons[ticker] = 'too_few_bars'; continue; }

  // :1111-1112  price gate reads the last RAW bar close
  const lastPrice = tickerBars[tickerBars.length - 1].c;
  if (lastPrice < 5 || lastPrice > 50) { rejectedPrice++; rejectReasons[ticker] = 'price'; continue; }

  // :1114-1116  prev close < $4 => gapped out of penny territory
  const prevClose = row.prev_close ?? null;
  if (prevClose != null && prevClose < 4) { rejectedPrice++; rejectReasons[ticker] = 'price'; continue; }

  // :1119  excluded ETFs fold into the conditions counter
  if (EXCLUDED_TICKERS.has(ticker)) { rejectedCond++; rejectReasons[ticker] = 'conditions'; continue; }

  // :1124-1131  RVOL on session bars; null is a FAIL
  let rvol = null;
  const prevVol = row.prev_volume ?? null;
  if (prevVol) rvol = calculateRVOL(marketBars, prevVol);
  if (rvol === null || rvol < 1.5) { rejectedRvol++; rejectReasons[ticker] = 'rvol'; continue; }

  // :1136-1137  the condition check itself
  const check = checkRunnerConditions(marketBars, minVwapPct);
  if (!check.passes) { rejectedCond++; rejectReasons[ticker] = 'conditions'; continue; }

  // :1140-1146  trend consistency over session bars, hard gate at 45
  const greenBars = marketBars.filter(b => b.c >= b.o).length;
  const trendConsistency = marketBars.length > 0
    ? parseFloat((greenBars / marketBars.length * 100).toFixed(1))
    : null;
  if (trendConsistency != null && trendConsistency < 45) { rejectedCond++; rejectReasons[ticker] = 'conditions'; continue; }

  // :1158-1166  float gate; null passes silently in the JS
  const floatM = row.float_m ?? null;
  if (floatM !== null && (floatM < 5 || floatM > 200)) { rejectedCond++; rejectReasons[ticker] = 'conditions'; continue; }

  // :1169-1176  earnings gate; null / fetch failure passes silently in the JS
  const eDays = row.earnings_days_away ?? null;
  if (eDays != null && Math.abs(eDays) <= 2) { rejectedCond++; rejectReasons[ticker] = 'conditions'; continue; }

  // :1225-1241  firstSeenAt walkback, floored at index 9, capped at 12 bars
  let firstPassIndex = marketBars.length - 1;
  const walkbackFloor = Math.max(9, marketBars.length - 12);
  for (let wi = marketBars.length - 2; wi >= walkbackFloor; wi--) {
    const wc = checkRunnerConditions(marketBars.slice(0, wi + 1), minVwapPct);
    if (!wc.passes) break;
    firstPassIndex = wi;
  }

  passing[ticker] = {
    price:            check.price,
    vwap:             check.vwap,
    vwap_pct_above:   check.vwapPctAbove ?? null,
    ema9:             check.ema9,
    ema9_slope:       check.ema9Slope,
    rvol,
    trend_consistency: trendConsistency,
    first_pass_index: firstPassIndex,
    first_pass_t:     marketBars[firstPassIndex].t,
    session_vwap:     calculateVWAP(marketBars),
    reason:           check.reason,
  };
}

fixture.expected = {
  generator: 'scripts/runner_parity_golden.mjs',
  dto_root: DTO_ROOT,
  generated_at: new Date().toISOString(),
  excluded_tickers_size: EXCLUDED_TICKERS.size,
  passing,
  reject_reasons: rejectReasons,
  rejections: {
    no_bars: noBars,
    too_few_bars: tooFewBars,
    price: rejectedPrice,
    rvol: rejectedRvol,
    conditions: rejectedCond,
  },
};

writeFileSync(fixturePath, JSON.stringify(fixture, null, 2) + '\n');
console.log(
  `[golden] ${path.basename(fixturePath)} — ${Object.keys(passing).length} passing of ` +
  `${Object.keys(tickers).length} | rejected: ${noBars} no-bars, ${tooFewBars} too-few-bars, ` +
  `${rejectedPrice} price, ${rejectedRvol} rvol, ${rejectedCond} conditions`
);
