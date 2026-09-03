"""
Phase 1d (Addendum A §A3) — universe hygiene.

Pins the symbol-shape filter (is_common_stock_symbol), the EXCLUDED_PRODUCTS
single source of truth, and the bundle-render tags. The filter is deliberately
allowlist-first and narrow: a filter that silently drops a real candidate is
worse than one that lets a warrant through, because the shadow ledger can
price the warrant and cannot price the absence.
"""
from __future__ import annotations

from agent.providers import (EXCLUDED_PRODUCTS, SCAN_UNIVERSE,
                             is_common_stock_symbol, render_bundle_md)


# ------------------------------------------------------------- shape filter

def test_production_observed_warrants_are_rejected():
    # The 2026-08/09 production error flood: warrants/units that reached the
    # earnings lookup via movers/most-actives.
    for sym in ("GFAIW", "GIPRW", "PSQH.WS", "SCAGW", "RMSGW", "RCKTW",
                "BRLSW", "DAICW", "MRNOW", "NRSNW", "KIDZW", "ARQQW"):
        assert is_common_stock_symbol(sym) is False, sym


def test_explicit_suffixes_are_rejected():
    for sym in ("AB.WS", "ABC.U", "ABCD.R", "AB.RT", "ABC-WS", "AB.PRA"):
        assert is_common_stock_symbol(sym) is False, sym


def test_digits_and_overlong_symbols_are_rejected():
    assert is_common_stock_symbol("ABC123") is False
    assert is_common_stock_symbol("S000") is False
    assert is_common_stock_symbol("TOOLONG") is False


def test_legitimate_tickers_survive():
    # W-terminal and five-char names that are real common stock must pass —
    # the trailing-letter rule only applies to 5-char symbols per the NASDAQ
    # 5th-letter convention.
    for sym in ("GLW", "SNOW", "PLOW", "WOW", "AAPL", "GOOGL", "BRK.B",
                "TCEHY", "MARA", "A"):
        assert is_common_stock_symbol(sym) is True, sym


def test_scan_universe_is_always_common_stock():
    for sym in SCAN_UNIVERSE:
        assert is_common_stock_symbol(sym) is True, sym


def test_blank_symbol_is_rejected():
    assert is_common_stock_symbol("") is False
    assert is_common_stock_symbol(None) is False


# ------------------------------------------------------- excluded products

def test_excluded_products_do_not_overlap_the_scan_universe():
    # A name in both lists would be simultaneously "always common stock" and
    # "never a candidate" — a drift bug of exactly the kind DTO hit.
    assert not (EXCLUDED_PRODUCTS & set(SCAN_UNIVERSE))


def test_excluded_products_covers_the_known_complexes():
    for sym in ("TQQQ", "SQQQ", "SOXL", "UVXY", "NVDL", "TSLL", "MSTX"):
        assert sym in EXCLUDED_PRODUCTS, sym


# ------------------------------------------------------------ bundle render

def test_bundle_renders_leveraged_and_warrant_tags():
    b = {
        "generated_at": "06:00",
        "premarket_gappers": [
            {"symbol": "SOXL", "gap_pct": 6.1, "last": 30.0, "prev_close": 28.3,
             "in_scan_universe": False, "is_leveraged_product": True,
             "is_common_stock": True},
            {"symbol": "GFAIW", "gap_pct": 33.0, "last": 1.2, "prev_close": 0.9,
             "in_scan_universe": False, "is_leveraged_product": False,
             "is_common_stock": False},
            {"symbol": "AAPL", "gap_pct": 3.2, "last": 210.0, "prev_close": 203.5,
             "in_scan_universe": True, "is_leveraged_product": False,
             "is_common_stock": True},
        ],
    }
    md = render_bundle_md(b)
    soxl_line = next(l for l in md.splitlines() if l.startswith("- SOXL"))
    gfaiw_line = next(l for l in md.splitlines() if l.startswith("- GFAIW"))
    aapl_line = next(l for l in md.splitlines() if l.startswith("- AAPL"))
    assert "[leveraged]" in soxl_line
    assert "[warrant/unit]" in gfaiw_line
    assert "[leveraged]" not in aapl_line and "[warrant/unit]" not in aapl_line
