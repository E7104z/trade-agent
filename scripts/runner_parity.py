"""
RUNNER PARITY HARNESS - the gate that proves the DTO port (RUNNER_PORT_SPEC 0.4).

Loads each fixture under tests/fixtures/runner_parity/, runs the Python scan
over the IDENTICAL bar arrays the DTO JavaScript was run over, and diffs:

  1. the set of passing tickers          - any difference is a hard fail
  2. per-ticker price, vwap, vwap_pct_above, ema9, ema9_slope, rvol
  3. the rejection-reason counts

Tolerances (spec 0.4 step 3):
  prices / VWAP / rvol / trend_consistency   abs(py - js) <= 1e-6
  ema9_slope                                 abs(py - js) <= 1e-9

Exits non-zero on any mismatch so it works as a CI/task gate, matching the
scripts/db_preflight.py pattern.

Usage:
  .venv\\Scripts\\python scripts/runner_parity.py [fixture.json ...]

With no arguments, runs every fixture in tests/fixtures/runner_parity/.

The `expected` block in each fixture is produced by
scripts/runner_parity_golden.mjs, which executes DTO's real modules under
Node. Regenerate fixtures with scripts/runner_parity_fixtures.py.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import runner_scan  # noqa: E402  (path must be set up first)

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "fixtures", "runner_parity",
)

# Field -> tolerance. Slope is held tighter because it is a difference of two
# nearly equal EMAs: a loose bound there would hide exactly the seeding
# divergence this harness exists to catch.
TOLERANCES = {
    "price": 1e-6,
    "vwap": 1e-6,
    "vwap_pct_above": 1e-6,
    "ema9": 1e-6,
    "rvol": 1e-6,
    "trend_consistency": 1e-6,
    "ema9_slope": 1e-9,
}


FIXTURE_SUFFIXES = (".json.gz", ".json")


def load_fixture(path: str) -> dict:
    """Read a fixture, gzipped or plain.

    Fixtures are stored gzipped: they are raw 5-minute bar arrays for ~95
    tickers a session, which pretty-printed run about 1 MB each and compress
    better than 10:1. Ten of them uncompressed came to 6.5 MB — five times the
    size of the entire codebase — which is too much weight for evidence files
    to carry in a repo that gets indexed elsewhere. Plain .json is still
    accepted so an older or hand-written fixture keeps working.
    """
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_fixture(path: str, fixture: dict) -> None:
    """Write a fixture, gzipping when the path says so.

    mtime=0 so regenerating an unchanged fixture produces byte-identical
    output instead of a spurious diff from the gzip header timestamp.
    """
    text = json.dumps(fixture, indent=2) + "\n"
    if path.endswith(".gz"):
        with gzip.GzipFile(path, "wb", compresslevel=9, mtime=0) as gz:
            gz.write(text.encode("utf-8"))
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


def fixture_paths(directory: str = None) -> list[str]:
    """Every fixture in `directory`, gzipped or plain, sorted."""
    directory = directory or FIXTURE_DIR
    if not os.path.isdir(directory):
        return []
    names = [f for f in os.listdir(directory) if f.endswith(FIXTURE_SUFFIXES)]
    return sorted(os.path.join(directory, f) for f in names)


def _parse_bar(b: dict) -> dict:
    return {
        "t": datetime.fromisoformat(b["t"]),
        "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"],
    }


def run_python_scan(fixture: dict) -> dict:
    """Reproduce the fixture's scan in Python, in DTO-compatible gate mode.

    null_gates="dto" is REQUIRED here and only here: the JS passes a null
    float/earnings silently, while Phase 1 must reject it (spec R-4). Running
    the harness in reject mode would produce ticker-set differences that are
    intended divergence, not port error, and would make the gate useless for
    its actual purpose.
    """
    min_vwap_pct = fixture.get("min_vwap_pct", 0.5)
    passing: dict[str, dict] = {}
    reject_reasons: dict[str, str] = {}
    counts = {"no_bars": 0, "too_few_bars": 0, "price": 0, "rvol": 0, "conditions": 0}

    for ticker in sorted(fixture.get("tickers", {})):
        row = fixture["tickers"][ticker]
        bars = [_parse_bar(b) for b in (row.get("bars") or [])]

        res = runner_scan.evaluate_ticker(
            ticker, bars,
            prev_volume=row.get("prev_volume"),
            prev_close=row.get("prev_close"),
            float_m=row.get("float_m"),
            earnings_days_away=row.get("earnings_days_away"),
            min_vwap_pct=min_vwap_pct,
            null_gates="dto",
            end_et_minutes=None,
        )
        if not res["passes"]:
            counts[res["counter"]] += 1
            reject_reasons[ticker] = res["counter"]
            continue

        mkt = runner_scan.market_hours_bars(bars)
        check = res["check"]
        fp_idx = runner_scan.first_pass_index(mkt, min_vwap_pct)
        passing[ticker] = {
            "price": check["price"],
            "vwap": check["vwap"],
            "vwap_pct_above": check["vwap_pct_above"],
            "ema9": check["ema9"],
            "ema9_slope": check["ema9_slope"],
            "rvol": res["rvol"],
            "trend_consistency": res["trend_consistency"],
            "first_pass_index": fp_idx,
            "reason": check["reason"],
        }

    return {"passing": passing, "rejections": counts, "reject_reasons": reject_reasons}


def compare(fixture: dict, actual: dict) -> list[str]:
    """Return a list of failure strings. Empty means parity."""
    failures: list[str] = []
    expected = fixture.get("expected")
    if not expected:
        return [f"fixture has no `expected` block - run scripts/runner_parity_golden.mjs first"]

    exp_pass = expected.get("passing", {})
    act_pass = actual["passing"]

    # 1. Ticker-set identity. A hard fail regardless of tolerance.
    only_js = sorted(set(exp_pass) - set(act_pass))
    only_py = sorted(set(act_pass) - set(exp_pass))
    if only_js:
        failures.append(f"TICKER SET: passing in JS but not Python: {only_js}")
    if only_py:
        failures.append(f"TICKER SET: passing in Python but not JS: {only_py}")

    # 2. Per-ticker numerics on the intersection.
    for ticker in sorted(set(exp_pass) & set(act_pass)):
        e, a = exp_pass[ticker], act_pass[ticker]
        for field, tol in TOLERANCES.items():
            ev, av = e.get(field), a.get(field)
            if ev is None and av is None:
                continue
            if ev is None or av is None:
                failures.append(f"{ticker}.{field}: js={ev} py={av} (one is null)")
                continue
            if abs(av - ev) > tol:
                failures.append(
                    f"{ticker}.{field}: js={ev!r} py={av!r} "
                    f"diff={abs(av - ev):.3e} > tol={tol:.0e}"
                )
        if e.get("first_pass_index") != a.get("first_pass_index"):
            failures.append(
                f"{ticker}.first_pass_index: js={e.get('first_pass_index')} "
                f"py={a.get('first_pass_index')}"
            )

    # 3. Rejection-reason counts.
    exp_rej = expected.get("rejections", {})
    for key in ("no_bars", "too_few_bars", "price", "rvol", "conditions"):
        ev, av = exp_rej.get(key), actual["rejections"].get(key)
        if ev != av:
            failures.append(f"REJECTIONS.{key}: js={ev} py={av}")

    # Per-ticker rejection attribution - not required by the spec, but it
    # turns an aggregate count mismatch into a named ticker, which is the
    # difference between a five-minute fix and an afternoon of bisecting.
    exp_rr = expected.get("reject_reasons", {})
    act_rr = actual["reject_reasons"]
    for ticker in sorted(set(exp_rr) | set(act_rr)):
        if exp_rr.get(ticker) != act_rr.get(ticker):
            failures.append(
                f"{ticker}: rejected as js={exp_rr.get(ticker)} py={act_rr.get(ticker)}"
            )

    # 4. EXCLUDED_TICKERS set size (spec 0.6).
    js_size = expected.get("excluded_tickers_size")
    if js_size is not None and js_size != len(runner_scan.EXCLUDED_TICKERS):
        failures.append(
            f"EXCLUDED_TICKERS size: js={js_size} py={len(runner_scan.EXCLUDED_TICKERS)}"
        )

    return failures


def run_fixture(path: str) -> list[str]:
    fixture = load_fixture(path)
    return compare(fixture, run_python_scan(fixture))


def main(argv: list[str]) -> int:
    paths = argv[1:]
    if not paths:
        if not os.path.isdir(FIXTURE_DIR):
            print(f"FAIL  no fixture directory at {FIXTURE_DIR}")
            print("      generate fixtures with scripts/runner_parity_fixtures.py")
            return 1
        paths = fixture_paths()
    if not paths:
        print(f"FAIL  no fixtures found in {FIXTURE_DIR}")
        print("      generate fixtures with scripts/runner_parity_fixtures.py")
        return 1

    total_failures = 0
    for path in paths:
        failures = run_fixture(path)
        name = os.path.basename(path)
        if failures:
            total_failures += len(failures)
            print(f"FAIL  {name}  ({len(failures)} difference(s))")
            for f in failures:
                print(f"        {f}")
        else:
            n = len(load_fixture(path).get("expected", {}).get("passing", {}))
            print(f"ok    {name}  ({n} passing tickers matched)")

    print()
    if total_failures:
        print(f"PARITY FAILED - {total_failures} difference(s) across {len(paths)} fixture(s)")
        return 1
    if len(paths) < 5:
        print(f"PARITY OK on {len(paths)} fixture(s) - spec 0.6 requires at least 5 "
              f"distinct session dates before Phase 0 can be signed off")
        return 1
    print(f"PARITY OK - {len(paths)} fixtures, zero differences")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
