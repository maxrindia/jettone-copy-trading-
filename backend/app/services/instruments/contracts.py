"""
Canonical cross-broker instrument identity for equity AND derivatives, and per-broker resolution of it.
=======================================================================================================
`CanonicalContract` is the broker-neutral identity of one tradable instrument:

  equity       exchange (NSE|BSE) + symbol, with the ISIN when known                        key  EQ|NSE|ISIN|INE...  /  EQ|NSE|SYM|SBIN
  future       exchange (NFO) + underlying + expiry                                         key  FUT|NFO|NIFTY|2026-11-26
  option       exchange (NFO) + underlying + expiry + strike + CE|PE                        key  CE|NFO|NIFTY|2026-11-26|23500

A symbol string alone is never an identity (brokers spell derivatives differently, and Alice Blue's order book reports a bare underlying
for an option). The exchange is part of the identity; NSE and BSE listings are never equated.

`resolve_for_broker` answers "which instrument does THIS broker call it?" from that broker's OWN catalog, and only ever returns what the
catalog states. Every refusal is a distinct verdict; nothing is guessed, and a broker's token is never carried to another broker:

  RESOLVED_UNVERIFIED  exactly one candidate in a loaded, fresh catalog. Always "unverified" for derivatives (no live evidence of the
                       mapping exists for any broker pair), so a caller may log it but must not trade on it.
  UNMAPPED             the loaded catalog has no such contract.
  AMBIGUOUS            more than one candidate.
  STALE                the catalog snapshot is older than `max_age`, or its completeness cannot be shown.
  CATALOG_NOT_LOADED   the broker's catalog is empty / not loaded - never evidence that the contract does not exist.
  NOT_SUPPORTED        the broker's adapter does not trade that segment (Groww and Angel One: equity cash only here).
  INVALID_INPUT        the canonical contract itself is malformed.

Documented limits that keep derivative mapping UNVERIFIED: Zerodha's `name` column is documented only for equity (so the underlying
match is best effort); Angel One's scrip master fields other than token/symbol/name/exch_seg have no verified reference; Groww's loader
reads CASH/EQ rows only. Alice Blue's derivative terms are keyed by token and hold no underlying name, so Alice is resolved only through
its own token path (Alice -> Alice), never through this module.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional, Tuple

from app.services.alice_blue.instruments import InstrumentResolutionError

MAX_CATALOG_AGE = timedelta(hours=26)
EQUITY_EXCHANGES = frozenset({"NSE", "BSE"})
DERIVATIVE_EXCHANGES = frozenset({"NFO"})
KINDS = frozenset({"EQ", "FUT", "CE", "PE"})


class ResolutionVerdict(str, Enum):
    RESOLVED_UNVERIFIED = "RESOLVED_UNVERIFIED"
    UNMAPPED = "UNMAPPED"
    AMBIGUOUS = "AMBIGUOUS"
    STALE = "STALE"
    CATALOG_NOT_LOADED = "CATALOG_NOT_LOADED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True)
class CanonicalContract:
    kind: str                         # EQ | FUT | CE | PE
    exchange: str
    underlying: str                   # equity symbol, or the derivative's underlying name
    expiry: Optional[date] = None
    strike: Optional[Decimal] = None
    isin: Optional[str] = None

    def problem(self) -> Optional[str]:
        if self.kind not in KINDS:
            return "kind must be EQ, FUT, CE or PE"
        if not str(self.underlying or "").strip():
            return "underlying is empty"
        if self.kind == "EQ":
            if self.exchange not in EQUITY_EXCHANGES:
                return "equity exchange must be NSE or BSE"
            if self.expiry is not None or self.strike is not None:
                return "equity has no expiry or strike"
            return None
        if self.exchange not in DERIVATIVE_EXCHANGES:
            return "derivative exchange must be NFO"
        if self.expiry is None:
            return "derivative needs an expiry"
        if self.kind == "FUT" and self.strike is not None:
            return "future has no strike"
        if self.kind in ("CE", "PE") and (self.strike is None or self.strike <= 0):
            return "option needs a positive strike"
        return None

    @property
    def key(self) -> str:
        if self.kind == "EQ":
            return f"EQ|{self.exchange}|ISIN|{self.isin}" if self.isin else f"EQ|{self.exchange}|SYM|{self.underlying}"
        base = f"{self.kind}|{self.exchange}|{self.underlying}|{self.expiry.isoformat() if self.expiry else ''}"
        return base + (f"|{format(self.strike.normalize(), 'f')}" if self.kind in ("CE", "PE") and self.strike is not None else "")


def make_contract(kind: Any, exchange: Any, underlying: Any, expiry: Any = None, strike: Any = None, isin: Any = None
                  ) -> Optional[CanonicalContract]:
    """A normalized contract, or None when the inputs cannot be read (never a partially guessed identity)."""
    try:
        exp = expiry if isinstance(expiry, date) or expiry is None else date.fromisoformat(str(expiry).strip())
        stk = None if strike in (None, "") else (strike if isinstance(strike, Decimal) else Decimal(str(strike).strip()))
        if stk is not None and not stk.is_finite():
            return None
    except (ValueError, InvalidOperation):
        return None
    c = CanonicalContract(
        kind=str(kind or "").strip().upper(), exchange=str(exchange or "").strip().upper(),
        underlying=str(underlying or "").strip().upper(), expiry=exp, strike=stk,
        isin=(str(isin).strip().upper() or None) if isin else None,
    )
    return c if c.problem() is None else None


@dataclass(frozen=True)
class ContractResolution:
    broker_id: str
    verdict: ResolutionVerdict
    reason: str
    contract_key: Optional[str] = None
    broker_symbol: Optional[str] = None       # the child broker's own trading symbol
    broker_token: Optional[str] = None        # the child broker's own token; meaningful only for that broker
    verified: bool = False                    # True is reserved for a mapping evidenced against a real account (none exists)
    lot_size: Optional[int] = None


def _no(broker_id: str, verdict: ResolutionVerdict, reason: str, contract: Optional[CanonicalContract] = None) -> ContractResolution:
    return ContractResolution(broker_id, verdict, reason, contract.key if contract else None)


def _fresh(loaded_at: Optional[datetime], now: datetime, max_age: timedelta) -> Optional[str]:
    if loaded_at is None:
        return "catalog completeness cannot be shown"
    if loaded_at.tzinfo is None:
        loaded_at = loaded_at.replace(tzinfo=timezone.utc)
    if now - loaded_at > max_age:
        return "catalog snapshot is older than the allowed age"
    return None


def resolve_for_broker(broker_id: str, contract: CanonicalContract, *, now: Optional[datetime] = None,
                       max_age: timedelta = MAX_CATALOG_AGE, zerodha_store: Optional[Any] = None) -> ContractResolution:
    """Resolve a canonical contract in one broker's own catalog. Pure apart from reading that in-memory catalog. Never raises."""
    now = now or datetime.now(timezone.utc)
    broker = str(broker_id or "").strip().lower()
    try:
        problem = contract.problem()
        if problem:
            return _no(broker, ResolutionVerdict.INVALID_INPUT, problem)
        if broker == "zerodha":
            return _resolve_zerodha(contract, now, max_age, zerodha_store)
        if broker in ("groww", "angelone"):
            if contract.kind != "EQ":
                return _no(broker, ResolutionVerdict.NOT_SUPPORTED, "this adapter trades equity cash only", contract)
            return _no(broker, ResolutionVerdict.NOT_SUPPORTED,
                       "equity mapping for this broker is reported by the shadow comparison (instruments/comparator), not by this resolver",
                       contract)
        if broker == "alicebluenew":
            return _no(broker, ResolutionVerdict.NOT_SUPPORTED,
                       "Alice Blue is resolved through its own token path, not by canonical identity", contract)
        return _no(broker, ResolutionVerdict.NOT_SUPPORTED, "unknown broker", contract)
    except Exception as exc:                                    # an error is never a mapping
        return _no(broker, ResolutionVerdict.INVALID_INPUT, f"resolver error: {type(exc).__name__}", contract)


def _resolve_zerodha(contract: CanonicalContract, now: datetime, max_age: timedelta, store: Optional[Any]) -> ContractResolution:
    if store is None:
        from app.services.zerodha import instruments as zi
        store = zi.store
    if not getattr(store, "size", 0):
        return _no("zerodha", ResolutionVerdict.CATALOG_NOT_LOADED, "Zerodha instruments are not loaded", contract)
    stale = _fresh(store.snapshot_loaded_at, now, max_age)
    if stale:
        return _no("zerodha", ResolutionVerdict.STALE, stale, contract)
    try:
        if contract.kind == "EQ":
            inst = store.resolve(contract.exchange, contract.underlying)
        else:
            inst = store.find_contract(contract.exchange, contract.underlying, contract.kind, contract.expiry, contract.strike)
    except InstrumentResolutionError as exc:
        text = str(exc)
        verdict = ResolutionVerdict.AMBIGUOUS if "ambiguous" in text else ResolutionVerdict.UNMAPPED
        return _no("zerodha", verdict, text[:200], contract)
    return ContractResolution(
        "zerodha", ResolutionVerdict.RESOLVED_UNVERIFIED,
        "single candidate in Zerodha's own dump; mapping not evidenced against a real account",
        contract.key, inst.tradingsymbol, inst.instrument_token or None, False, inst.lot_size if inst.lot_size_stated else None,
    )
