"""
Angel One instrument mapping (equity only)
==========================================
Angel One orders need Angel One's own `symboltoken`. A token from another
broker (e.g. the Alice Blue instrument id carried by a copied Master event) is
meaningless here and is NEVER used: the token is always resolved from Angel
One's instrument master by (exchange, symbol).

The instrument master is loaded from a file/records the owner supplies, or
downloaded ONLY from ANGEL_ONE_SCRIP_MASTER_URL when it is configured. Its field
names (token, symbol, name, exch_seg, instrumenttype) follow Angel One's
published scrip master but are treated as unverified: a record missing any of
them is skipped, and resolution refuses unless exactly one instrument matches.
Derivatives (exch_seg NFO) are loaded too, but only a row that AGREES WITH ITSELF: the trading symbol (underlying + DDMMMYY expiry + strike +
CE / PE, or FUT) must match the record's `name`, `expiry` and `strike` fields, so a wrong assumption about a field format (the strike scale, the
expiry spelling) loads nothing instead of loading a wrong contract. Not documented, therefore not relied on: a freeze quantity (the file has
none: 0 = unknown, never "unlimited"). Resolution of a derivative is by the exact trading symbol (or the exact canonical contract) and refuses
unless exactly one row matches.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.services.alice_blue.instruments import InstrumentResolutionError

_EXCHANGES = {"NSE": "NSE", "NSE_CM": "NSE", "BSE": "BSE", "BSE_CM": "BSE"}

# underlying + DDMMMYY expiry + (strike) + CE / PE / FUT, e.g. NIFTY28NOV2423500CE or NIFTY28NOV24FUT (Angel One's trading-symbol spelling).
_FNO_SYMBOL = re.compile(r"^(?P<und>[A-Z0-9&\-]+?)(?P<exp>\d{2}[A-Z]{3}\d{2})(?P<strike>\d+(?:\.\d+)?)?(?P<kind>CE|PE|FUT)$")
STRIKE_SCALE = Decimal(100)         # the record's `strike` is the strike x 100; a row whose symbol disagrees is not loaded
TICK_SCALE = Decimal(100)           # the record's `tick_size` is in paise; unparseable = unknown


@dataclass(frozen=True)
class AngelDerivative:
    token: str
    tradingsymbol: str
    underlying: str
    kind: str                       # FUT, CE, PE
    expiry: date
    strike: Optional[Decimal]
    lot_size: int
    tick_size: Optional[Decimal]
    freeze_quantity: int = 0        # the scrip master carries none: 0 = unknown


def _derivative_from_record(r: Dict[str, Any]) -> Optional[AngelDerivative]:
    """One NFO scrip-master record as a derivative, or None when ANY part of it is missing or does not agree with the trading symbol."""
    try:
        token = str(r.get("token") or "").strip()
        symbol = str(r.get("symbol") or "").strip().upper()
        name = str(r.get("name") or "").strip().upper()
        itype = str(r.get("instrumenttype") or "").strip().upper()
        m = _FNO_SYMBOL.match(symbol)
        if not (token and m and name and itype):
            return None
        kind = m.group("kind")
        if (kind == "FUT") != itype.startswith("FUT") or (kind != "FUT" and not itype.startswith("OPT")) or m.group("und") != name:
            return None
        expiry = datetime.strptime(str(r.get("expiry") or "").strip().upper(), "%d%b%Y").date()
        if datetime.strptime(m.group("exp"), "%d%b%y").date() != expiry:
            return None
        strike: Optional[Decimal] = None
        if kind != "FUT":
            strike = Decimal(str(r.get("strike") or "").strip()) / STRIKE_SCALE
            if not m.group("strike") or Decimal(m.group("strike")) != strike or strike <= 0:
                return None
        lot = int(Decimal(str(r.get("lotsize") or "").strip()))
        if lot <= 0:
            return None
        tick: Optional[Decimal] = None
        try:
            raw_tick = Decimal(str(r.get("tick_size") or "").strip()) / TICK_SCALE
            tick = raw_tick if raw_tick > 0 else None
        except (InvalidOperation, ValueError):
            tick = None
        return AngelDerivative(token, symbol, name, kind, expiry, strike, lot, tick)
    except (ValueError, InvalidOperation, ArithmeticError):
        return None


class AngelInstrumentStore:
    def __init__(self, clock: Optional[Callable[[], datetime]] = None) -> None:
        # (exchange, tradingsymbol upper) -> {token, ...}
        self._by_symbol: Dict[Tuple[str, str], set] = {}
        self._derivatives: Dict[str, List[AngelDerivative]] = {}       # trading symbol -> rows (more than one = ambiguous)
        self._derivatives_by_token: Dict[str, List[AngelDerivative]] = {}
        self.derivative_count = 0
        self.size = 0
        # Freshness metadata for read-only consumers (R4b); it does not influence loading or resolve().
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._load_calls = 0
        self._snapshot_at: Optional[datetime] = None

    @property
    def snapshot_loaded_at(self) -> Optional[datetime]:
        """When a COMPLETE catalog snapshot finished loading, else None. Only exactly one full-file load
        (load_file) counts; records added directly, a second load, or an empty load leave freshness unknown."""
        return self._snapshot_at if self._load_calls == 1 else None

    def iter_records(self) -> Tuple[Tuple[str, str, str], ...]:
        """(exchange, symbol, token) for every stored record, sorted."""
        return tuple(sorted((ex, sym, tok) for (ex, sym), toks in self._by_symbol.items() for tok in toks))

    def lookup_symbol(self, exchange: str, symbol: str) -> Tuple[Tuple[str, str], ...]:
        """(symbol, token) rows for the exchange and symbol, trying the bare and "-EQ" spellings as resolve() does."""
        exch = _EXCHANGES.get(str(exchange or "").strip().upper())
        sym = str(symbol or "").strip().upper()
        if exch is None or not sym:
            return ()
        names = (sym,) if sym.endswith("-EQ") else (sym, f"{sym}-EQ")
        return tuple(sorted((name, tok) for name in names for tok in self._by_symbol.get((exch, name), ())))

    def load_records(self, records: Iterable[Dict[str, Any]]) -> int:
        self._load_calls += 1
        added = 0
        for r in records:
            if str(r.get("exch_seg") or "").strip().upper() == "NFO":
                d = _derivative_from_record(r)
                if d is not None:
                    self._derivatives.setdefault(d.tradingsymbol, []).append(d)
                    self._derivatives_by_token.setdefault(d.token, []).append(d)
                    self.derivative_count += 1
                    added += 1
                continue
            token = str(r.get("token") or "").strip()
            symbol = str(r.get("symbol") or "").strip().upper()
            exch = _EXCHANGES.get(str(r.get("exch_seg") or "").strip().upper())
            if not (token and symbol and exch):
                continue
            self._by_symbol.setdefault((exch, symbol), set()).add(token)
            added += 1
        self.size += added
        return added

    def load_file(self, path: str) -> int:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise InstrumentResolutionError("Angel One instrument master must be a JSON list.")
        first_load = self._load_calls == 0
        added = self.load_records(data)
        if first_load and added > 0:
            self._snapshot_at = self._clock()      # the whole file parsed; set only after load_records returned
        return added

    def derivative(self, symbol: str) -> AngelDerivative:
        """The derivative with exactly this trading symbol, or InstrumentResolutionError (not loaded / not found / ambiguous)."""
        if not self.derivative_count:
            raise InstrumentResolutionError("Angel One F&O instruments are not loaded.")
        rows = self._derivatives.get(str(symbol or "").strip().upper(), [])
        if len({(d.token, d.expiry, d.strike, d.kind) for d in rows}) != 1:
            raise InstrumentResolutionError(f"Angel One F&O contract {symbol!r} is " + ("not found." if not rows else "ambiguous."))
        return rows[0]

    def derivative_by_token(self, token: str) -> AngelDerivative:
        """The derivative with exactly this token (the position book carries one), or InstrumentResolutionError."""
        rows = self._derivatives_by_token.get(str(token or "").strip(), [])
        if len({d.tradingsymbol for d in rows}) != 1:
            raise InstrumentResolutionError("Angel One F&O token is " + ("not found." if not rows else "ambiguous."))
        return rows[0]

    def find_derivative(self, kind: str, underlying: str, expiry: date, strike: Optional[Decimal]) -> AngelDerivative:
        """Exactly one row for the canonical contract (underlying + expiry + strike + CE / PE / FUT), else InstrumentResolutionError."""
        if not self.derivative_count:
            raise InstrumentResolutionError("Angel One F&O instruments are not loaded.")
        want_kind, want_und = str(kind or "").strip().upper(), str(underlying or "").strip().upper()
        found = [d for rows in self._derivatives.values() for d in rows
                 if d.kind == want_kind and d.underlying == want_und and d.expiry == expiry and (d.strike == strike if want_kind != "FUT" else True)]
        if len({(d.tradingsymbol, d.token) for d in found}) != 1:
            raise InstrumentResolutionError("Angel One F&O contract is " + ("not found." if not found else "ambiguous."))
        return found[0]

    def resolve(self, exchange: str, symbol: str) -> Tuple[str, str]:
        """(symboltoken, tradingsymbol) for an equity, or InstrumentResolutionError."""
        exch = _EXCHANGES.get(str(exchange or "").strip().upper())
        if exch is None:
            raise InstrumentResolutionError(
                f"Angel One instrument mapping supports NSE/BSE equity only (got {exchange!r})."
            )
        if not self.size:
            raise InstrumentResolutionError("Angel One instrument master is not loaded.")
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise InstrumentResolutionError("Empty symbol.")
        found: List[Tuple[str, str]] = []
        for candidate in (sym, f"{sym}-EQ") if not sym.endswith("-EQ") else (sym,):
            for token in self._by_symbol.get((exch, candidate), ()):
                found.append((token, candidate))
        distinct = {t for t, _ in found}
        if len(distinct) != 1:
            raise InstrumentResolutionError(
                f"Angel One instrument for {exch}:{sym} is "
                + ("not found." if not distinct else f"ambiguous ({len(distinct)} matches).")
            )
        return found[0]


# Process-wide store; populated at startup only when Angel One is enabled.
store = AngelInstrumentStore()
