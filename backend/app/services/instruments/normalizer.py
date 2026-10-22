"""
Pure equity normalisation (R4a). Never guesses: missing, malformed or conflicting metadata gives UNMAPPED with a
reason, and an ISIN that is present but invalid is NOT silently downgraded to a symbol-only identity.

FUT / OPT (and every non-NSE/BSE exchange) are explicitly unmapped: their authoritative metadata fields (expiry,
strike, lot, tick) are not confirmed for any broker yet.
"""
import re
from typing import Optional, Tuple

from app.services.instruments.model import CanonicalInstrument, Confidence, Normalization

EQUITY_EXCHANGES = ("NSE", "BSE")
DERIVATIVE_EXCHANGES = frozenset({"NFO", "BFO", "MCX", "CDS", "BCD", "NCDEX"})

# Indices have no ISIN and are not tradable equity; treating "NIFTY" as an equity symbol would be a guess.
INDEX_NAMES = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"})

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&_\-]*$")
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_ABSENT_ISIN = {"", "NAN", "NONE", "NULL", "-"}


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_valid_isin(value: object) -> bool:
    """Format (2 letters, 9 alphanumerics, 1 digit) and the ISO 6166 check digit."""
    isin = str(value or "").strip().upper()
    if not _ISIN_RE.match(isin):
        return False
    return _luhn_ok("".join(str(int(c, 36)) for c in isin))


def normalize_exchange(raw: object) -> str:
    return str(raw or "").strip().upper()


def normalize_symbol(raw: object) -> Tuple[Optional[str], str]:
    """(symbol, "") or (None, reason). Strips surrounding space and ONE trailing "-EQ"; nothing else is rewritten."""
    sym = str(raw or "").strip().upper()
    if not sym:
        return None, "MISSING_SYMBOL"
    if sym.endswith("-EQ"):
        sym = sym[:-3]
    if not sym:
        return None, "MISSING_SYMBOL"
    if not _SYMBOL_RE.match(sym):
        return None, "MALFORMED_SYMBOL"
    return sym, ""


def normalize_isin(raw: object) -> Tuple[Optional[str], str]:
    """(isin, "") valid; (None, "") absent; (None, "MALFORMED_ISIN") present but invalid."""
    text = str(raw if raw is not None else "").strip().upper()
    if text in _ABSENT_ISIN:
        return None, ""
    return (text, "") if is_valid_isin(text) else (None, "MALFORMED_ISIN")


def _unmapped(reason: str) -> Normalization:
    return Normalization(None, Confidence.UNMAPPED, reason)


def normalize_equity(exchange: object, symbol: object, isin: object = None) -> Normalization:
    exch = normalize_exchange(exchange)
    if not exch:
        return _unmapped("MISSING_EXCHANGE")
    if exch in DERIVATIVE_EXCHANGES:
        return _unmapped("DERIVATIVES_NOT_SUPPORTED_YET")
    if exch not in EQUITY_EXCHANGES:
        return _unmapped("UNSUPPORTED_EXCHANGE")
    sym, reason = normalize_symbol(symbol)
    if sym is None:
        return _unmapped(reason)
    if sym.endswith("-FUT"):
        return _unmapped("DERIVATIVES_NOT_SUPPORTED_YET")     # Alice's futures trading-symbol form, even on a wrong exchange
    if sym in INDEX_NAMES:
        return _unmapped("NOT_AN_EQUITY_INDEX")
    clean_isin, isin_reason = normalize_isin(isin)
    if isin_reason:
        return _unmapped(isin_reason)
    inst = CanonicalInstrument(exchange=exch, symbol=sym, isin=clean_isin)
    return Normalization(inst, inst.confidence)
