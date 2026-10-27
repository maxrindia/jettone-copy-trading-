"""
Zerodha instrument master (Kite Connect `GET /instruments/:exchange`, a daily gzipped CSV)
===========================================================================================
Zerodha orders are placed by `exchange` + `tradingsymbol`. The Master's own instrument id (Alice Blue's numeric id, or any other
broker's) is never used: the instrument is always resolved from THIS broker's dump.

Columns (all named on the market-quotes page): instrument_token, exchange_token, tradingsymbol, name, last_price, expiry, strike,
tick_size, lot_size, instrument_type (EQ, FUT, CE, PE), segment, exchange. Zerodha's own advice is to key on
(exchange, tradingsymbol), NOT the numeric token, because exchanges reuse tokens for different derivatives after each expiry - so the
token is stored but never used as an identity here.

What is NOT documented, and therefore not relied on as verified:
  * `name` is described only as "Name of the company (for equity instruments)". For a derivative row its content (the underlying's name,
    in practice) is not specified, so `find_contract` - which matches a derivative by underlying name, expiry, strike and option type -
    is a best-effort candidate finder whose result stays UNVERIFIED evidence; it refuses unless EXACTLY ONE row matches.
  * the `expiry` text format (read as ISO yyyy-mm-dd; a derivative row whose expiry does not parse is not loaded);
  * freeze quantity: the dump has no such column, so it is never invented (callers see 0 = "not provided", exactly as Groww's store).
Resolution refuses on zero or more than one match; it never picks one.
"""
import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.zerodha import contract as C

logger = logging.getLogger("zerodha_instruments")

EQUITY_TYPES = frozenset({"EQ"})
DERIVATIVE_TYPES = frozenset({"FUT", "CE", "PE"})


@dataclass(frozen=True)
class ZerodhaInstrument:
    exchange: str
    tradingsymbol: str
    instrument_token: str          # kept for reference only: never an identity (tokens are reused after expiry)
    exchange_token: str
    name: str
    instrument_type: str           # EQ, FUT, CE, PE
    segment: str
    lot_size: int
    lot_size_stated: bool          # False when the CSV gave no usable lot size and `lot_size` is only the coerced default of 1
    tick_size: Optional[Decimal]
    expiry: Optional[date] = None
    strike: Optional[Decimal] = None
    freeze_quantity: int = 0       # never provided by the dump: always 0 (= unknown, not "unlimited")

    @property
    def is_derivative(self) -> bool:
        return self.instrument_type in DERIVATIVE_TYPES


def _int(value: object) -> int:
    try:
        return int(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError, TypeError):
        return 0


def _decimal(value: object) -> Optional[Decimal]:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _date(value: object) -> Optional[date]:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


class ZerodhaInstrumentStore:
    def __init__(self, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._by_key: Dict[Tuple[str, str], List[ZerodhaInstrument]] = {}
        self.size = 0
        self.skipped = 0                                   # rows that could not be loaded (unsupported exchange, bad expiry, no symbol)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._load_calls = 0
        self._snapshot_at: Optional[datetime] = None

    @property
    def snapshot_loaded_at(self) -> Optional[datetime]:
        """When a COMPLETE single-document snapshot finished loading, else None (any other history leaves freshness unknown)."""
        return self._snapshot_at if self._load_calls == 1 else None

    def iter_instruments(self) -> Tuple[ZerodhaInstrument, ...]:
        return tuple(i for key in sorted(self._by_key) for i in self._by_key[key])

    def lookup_symbol(self, exchange: str, symbol: str) -> Tuple[ZerodhaInstrument, ...]:
        """Every stored row for the exact (exchange, tradingsymbol), duplicates included. No suffix handling."""
        return tuple(self._by_key.get((str(exchange or "").strip().upper(), str(symbol or "").strip().upper()), ()))

    def load_rows(self, rows: Iterable[Dict[str, str]]) -> int:
        self._load_calls += 1
        added = 0
        for r in rows:
            exch = str(r.get("exchange") or "").strip().upper()
            sym = str(r.get("tradingsymbol") or "").strip().upper()
            itype = str(r.get("instrument_type") or "").strip().upper()
            if exch not in C.SUPPORTED_EXCHANGES or not sym or itype not in (EQUITY_TYPES | DERIVATIVE_TYPES):
                self.skipped += 1
                continue
            expiry = _date(r.get("expiry"))
            strike = _decimal(r.get("strike"))
            if itype in DERIVATIVE_TYPES and expiry is None:
                self.skipped += 1                          # a derivative without a readable expiry cannot be identified
                continue
            if itype == "CE" or itype == "PE":
                if strike is None or strike < 0:
                    self.skipped += 1
                    continue
            lot = _int(r.get("lot_size"))
            inst = ZerodhaInstrument(
                exchange=exch, tradingsymbol=sym, instrument_token=str(r.get("instrument_token") or "").strip(),
                exchange_token=str(r.get("exchange_token") or "").strip(), name=str(r.get("name") or "").strip(),
                instrument_type=itype, segment=str(r.get("segment") or "").strip().upper(), lot_size=max(1, lot),
                lot_size_stated=lot > 0, tick_size=_decimal(r.get("tick_size")), expiry=expiry,
                strike=strike if itype in ("CE", "PE") else None,
            )
            self._by_key.setdefault((exch, sym), []).append(inst)
            added += 1
        self.size += added
        return added

    def load_csv_text(self, text: str) -> int:
        first_load = self._load_calls == 0
        added = self.load_rows(csv.DictReader(io.StringIO(text)))
        if first_load and added > 0:
            self._snapshot_at = self._clock()
        return added

    def load_file(self, path: str) -> int:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return self.load_csv_text(fh.read())

    # ── resolution ─────────────────────────────────────────────────────────────────────────────────────
    def resolve(self, exchange: str, symbol: str) -> ZerodhaInstrument:
        """The ONE instrument a Master's (exchange, symbol) names on Zerodha. Equity: NSE/BSE, an Alice-style "-EQ" suffix is dropped.
        Derivative (NFO): only an EXACT Zerodha tradingsymbol resolves - another broker's symbol format is never translated here (that is
        the canonical-identity layer's job, and it is shadow-only). Refuses on none or several."""
        exch = str(exchange or "").strip().upper()
        if exch not in C.SUPPORTED_EXCHANGES:
            raise InstrumentResolutionError(f"Zerodha instrument mapping supports NSE, BSE and NFO only (got {exchange!r}).")
        if not self.size:
            raise InstrumentResolutionError("Zerodha instrument master is not loaded.")
        sym = str(symbol or "").strip().upper()
        if exch in C.EQUITY_EXCHANGES and sym.endswith("-EQ"):
            sym = sym[:-3]
        if not sym:
            raise InstrumentResolutionError("Empty symbol.")
        found = [i for i in self._by_key.get((exch, sym), []) if (i.instrument_type in EQUITY_TYPES) == (exch in C.EQUITY_EXCHANGES)]
        if len(found) != 1:
            raise InstrumentResolutionError(
                f"Zerodha instrument for {exch}:{sym} is " + ("not found." if not found else f"ambiguous ({len(found)} matches).")
            )
        return found[0]

    def find_contract(self, exchange: str, underlying: str, instrument_type: str, expiry: date,
                      strike: Optional[Decimal] = None) -> ZerodhaInstrument:
        """The one derivative row with this (exchange, underlying `name`, instrument_type, expiry[, strike]). BEST EFFORT: `name` is not
        documented for derivatives (see the module docstring), so a caller must treat the result as UNVERIFIED evidence. Refuses on none
        or several, and a CE/PE without a strike never matches."""
        exch = str(exchange or "").strip().upper()
        itype = str(instrument_type or "").strip().upper()
        if exch not in C.DERIVATIVE_EXCHANGES or itype not in DERIVATIVE_TYPES:
            raise InstrumentResolutionError("Zerodha derivative lookup needs an NFO exchange and an FUT, CE or PE type.")
        if itype in ("CE", "PE") and strike is None:
            raise InstrumentResolutionError("An option contract needs a strike.")
        if not self.size:
            raise InstrumentResolutionError("Zerodha instrument master is not loaded.")
        name = str(underlying or "").strip().upper()
        if not name:
            raise InstrumentResolutionError("Empty underlying.")
        matches = [
            i for rows in self._by_key.values() for i in rows
            if i.exchange == exch and i.instrument_type == itype and i.expiry == expiry and i.name.upper() == name
            and (itype == "FUT" or i.strike == strike)
        ]
        if len(matches) != 1:
            raise InstrumentResolutionError(
                f"Zerodha contract {exch}:{name} {itype} {expiry.isoformat()} is " + ("not found." if not matches else f"ambiguous ({len(matches)} matches).")
            )
        return matches[0]


# Process-wide store, populated only when Zerodha is enabled and its dump has been fetched.
store = ZerodhaInstrumentStore()
