"""
What an instrument IS, read from its registered name (Alpaca /v2/assets `name`).

WHY THIS EXISTS
---------------
The runner lane screens for ACCUMULATION in an operating company: price
holding above VWAP on a rising 9 EMA, with float, earnings and borrow as the
research factors. A leveraged single-stock ETF, a closed-end fund, a commodity
trust or an exchange-traded note can pass that screen, and a "runner" reading
on one is an artifact (runner_scan.EXCLUDED_TICKERS says the same of the 117
products it lists). It is also not independent evidence: on 2026-10-01 three
2x Lumentum wrappers from three issuers (LITX, LITU, LITG) and two 2x Coherent
wrappers (COHX, COHH) were five pool rows for two events.

The hand-kept EXCLUDED_TICKERS is a frozen DTO parity constant (its count is
asserted against the JavaScript), so every product launched after the port --
LITG's inception was August 2026 -- walked straight in. A registered name is
not frozen: "Leverage Shares 2X Long LITE Daily ETF" says what it is on the day
it lists, so a rule on the name catches new launches without a list.

The rules are deliberately narrow and were checked against all 12,952 active
Alpaca assets on 2026-10-01 (tests/test_instruments.py pins the real names that
decided each rule). "Trust" alone is NOT a fund marker: most "...Trust" names
are REITs and banks (Kite Realty Group Trust, Washington Trust Bancorp), which
are operating companies and stay in.

A second, independent signal is applied to scan survivors only: yfinance
`quoteType` (ETF, MUTUALFUND, ...), fetched by the same info call that already
supplies float -- see fund_from_profile().
"""
from __future__ import annotations

import re

__all__ = ["COMMON", "FUND", "DEBT", "PREFERRED", "UNIT", "CLASSES", "classify",
           "fund_from_profile"]

COMMON = "common"
FUND = "fund"
DEBT = "debt"
PREFERRED = "preferred"
UNIT = "unit"
CLASSES = (COMMON, FUND, DEBT, PREFERRED, UNIT)

# ETF / ETN / ETP, case-sensitive so a name word like "Etna" never matches.
# No trailing boundary: Alpaca prints "iShares Core 10+ Year USD Bond ETFof
# iShares Trust".
_ETX = re.compile(r"\bET[FNP]")
_FUND_WORDS = re.compile(
    r"\b(?:Exchange[- ]Traded|Funds?|Fd|ProShares|Leveraged|Inverse|Option Income"
    r"|Covered Call|Daily Target)\b", re.I)
# 2X / 3x / -3x / 1.5X: leverage multiples. A digit glued to an x as a whole
# word does not occur in operating-company names on this exchange set.
_LEVERAGE = re.compile(r"(?:^|[\s(])-?\d(?:\.\d+)?[xX]\b")

# A "Trust" is a fund/commodity vehicle only alongside one of these words...
_TRUST = re.compile(r"\bTrust\b", re.I)
_VEHICLE_WORDS = re.compile(
    r"\b(?:Gold|Silver|Platinum|Palladium|Copper|Bitcoin|Ether|Ethereum|Solana|XRP"
    r"|Litecoin|Commodity|Commodities|Currency|CurrencyShares|Physical|Municipal|Muni"
    r"|Income|Dividend|Term|Equity|Opportunit\w*|Allocation|Credit|Duration|Floating"
    r"|Strateg\w*|Securities|Investors|Investment Grade|Small-Cap|Micro-Cap"
    r"|Total Return|Resources|Utility|Utilities|Science|Health Sciences|Wellness"
    r"|Global|Universal|Series \d+)\b", re.I)
# ...and never alongside an operating-company word. REITs, banks and royalty
# trusts are businesses; their names say so.
_OPERATING_WORDS = re.compile(
    r"\b(?:Realty|Property|Properties|Real Estate|REIT|Mortgage|Hotel|Lodging"
    r"|Industrial|Residential|Royalty|Bancorp|Bancshares|Bank|Housing|Healthcare Trust"
    r"|Assets Trust|Office)\b", re.I)

_DEBT_NOUN = re.compile(r"\b(?:Notes?|Debentures?|Bonds?)\b", re.I)
_DEBT_TERMS = re.compile(r"\bdue\b|\d%|\bPerpetual\b|\bSenior\b|\bSubordinated\b", re.I)
_STRUCTURED = re.compile(r"\bStructured Products\b", re.I)

_PREFERRED = re.compile(r"\b(?:Preferred|Preference|Pfd)\b", re.I)
_ADR = re.compile(r"\bAmerican Depositary\b", re.I)

_UNIT = re.compile(r"\b(?:Units?|Warrants?)\b", re.I)
# Units that ARE the equity: MLP common units, royalty-trust units.
_PARTNERSHIP_UNIT = re.compile(
    r"\b(?:Common Units|Limited Partner|Limited Partnership|Beneficial Unit|Trust Units?"
    r"|Units of Beneficial Interest)", re.I)
_RIGHTS = re.compile(r"\bRights?\b(?!\s+to\b)", re.I)


def classify(name: str | None) -> str:
    """The instrument class of a registered name. Unknown or empty -> common:
    a missing name is a data gap, not evidence the security is a fund, and the
    survivor-level quoteType check still applies."""
    n = (name or "").strip()
    if not n:
        return COMMON
    if _ETX.search(n) or _FUND_WORDS.search(n) or _LEVERAGE.search(n):
        return FUND
    if _TRUST.search(n) and _VEHICLE_WORDS.search(n) and not _OPERATING_WORDS.search(n):
        return FUND
    if (_DEBT_NOUN.search(n) and _DEBT_TERMS.search(n)) or _STRUCTURED.search(n):
        return DEBT
    if _PREFERRED.search(n) and not _ADR.search(n):
        return PREFERRED
    if (_UNIT.search(n) and not _PARTNERSHIP_UNIT.search(n)) or _RIGHTS.search(n):
        return UNIT
    return COMMON


def fund_from_profile(quote_type: str | None, industry: str | None,
                      float_shares: float | None) -> str | None:
    """A reason this scan survivor is a fund, from its yfinance profile, or None.

    quoteType is Yahoo's own security type: anything but EQUITY (ETF,
    MUTUALFUND, ...) is not an operating company's stock. A closed-end fund is
    quoteType EQUITY, but Yahoo files it under "Asset Management" and reports
    no float -- an operating asset manager (WisdomTree, Invesco) reports one.
    A null quoteType is unmeasured and decides nothing.
    """
    qt = (quote_type or "").strip().upper()
    if qt and qt != "EQUITY":
        return f"quoteType {qt}"
    if (industry or "").strip().lower() == "asset management" and float_shares is None:
        return "closed-end fund (Asset Management, no float)"
    return None
