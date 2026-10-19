"""
Groww instrument mapping (equity only)
======================================
Groww orders are placed by `trading_symbol` + `exchange` + `segment`
(instrument master docs: `trading_symbol` is "the trading symbol of the
instrument to place orders with"). The Master's own instrument id (Alice Blue's
numeric id, or any other broker's) is never used: the instrument is always
resolved from Groww's master by (exchange, symbol).

Loaded columns (all named in Groww's instruments documentation): exchange,
trading_symbol, instrument_type, segment, isin, lot_size, freeze_quantity,
buy_allowed, sell_allowed. Only CASH-segment "EQ" instruments on NSE/BSE are
kept - F&O is not supported, so derivatives never resolve.

Resolution refuses unless exactly one instrument matches, and an instrument
whose buy/sell flag is false or unreadable is refused for that side.
"""

import csv
import io
import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import httpx

from app.core.config import settings
from app.services.alice_blue.instruments import InstrumentResolutionError

logger = logging.getLogger("groww_instruments")

_EXCHANGES = ("NSE", "BSE")
_TRUE = {"true", "1", "yes", "y", "t"}
_FALSE = {"false", "0", "no", "n", "f"}


@dataclass(frozen=True)
class GrowwInstrument:
    exchange: str
    trading_symbol: str
    isin: str
    lot_size: int
    freeze_quantity: int          # 0 = not provided
    buy_allowed: Optional[bool]   # None = unreadable (treated as not allowed)
    sell_allowed: Optional[bool]


def _flag(value: object) -> Optional[bool]:
    v = str(value or "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def _int(value: object) -> int:
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return 0


class GrowwInstrumentStore:
    def __init__(self) -> None:
        self._by_key: Dict[Tuple[str, str], List[GrowwInstrument]] = {}
        self.size = 0

    def load_rows(self, rows: Iterable[Dict[str, str]]) -> int:
        added = 0
        for r in rows:
            exch = str(r.get("exchange") or "").strip().upper()
            seg = str(r.get("segment") or "").strip().upper()
            itype = str(r.get("instrument_type") or "").strip().upper()
            sym = str(r.get("trading_symbol") or "").strip().upper()
            if exch not in _EXCHANGES or seg != "CASH" or itype != "EQ" or not sym:
                continue
            isin = str(r.get("isin") or "").strip().upper()
            if isin == "NAN":
                isin = ""
            inst = GrowwInstrument(
                exchange=exch, trading_symbol=sym, isin=isin,
                lot_size=max(1, _int(r.get("lot_size"))), freeze_quantity=max(0, _int(r.get("freeze_quantity"))),
                buy_allowed=_flag(r.get("buy_allowed")), sell_allowed=_flag(r.get("sell_allowed")),
            )
            self._by_key.setdefault((exch, sym), []).append(inst)
            added += 1
        self.size += added
        return added

    def load_csv_text(self, text: str) -> int:
        return self.load_rows(csv.DictReader(io.StringIO(text)))

    def load_file(self, path: str) -> int:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return self.load_csv_text(fh.read())

    def resolve(self, exchange: str, symbol: str) -> GrowwInstrument:
        exch = str(exchange or "").strip().upper()
        if exch not in _EXCHANGES:
            raise InstrumentResolutionError(f"Groww instrument mapping supports NSE/BSE equity only (got {exchange!r}).")
        if not self.size:
            raise InstrumentResolutionError("Groww instrument master is not loaded.")
        sym = str(symbol or "").strip().upper()
        if sym.endswith("-EQ"):
            sym = sym[:-3]
        if not sym:
            raise InstrumentResolutionError("Empty symbol.")
        found = self._by_key.get((exch, sym), [])
        if len(found) != 1:
            raise InstrumentResolutionError(
                f"Groww instrument for {exch}:{sym} is " + ("not found." if not found else f"ambiguous ({len(found)} matches).")
            )
        return found[0]


# Process-wide store, populated at startup only when Groww is enabled.
store = GrowwInstrumentStore()


async def load_instruments_if_enabled(target: Optional[GrowwInstrumentStore] = None) -> int:
    """Downloads Groww's instrument master once at startup, only when GROWW_ENABLED
    and GROWW_INSTRUMENT_MASTER_URL are both set. Any failure leaves the store
    empty, so every Groww order is refused (fail closed)."""
    target = target if target is not None else store
    if not settings.GROWW_ENABLED or not settings.GROWW_INSTRUMENT_MASTER_URL:
        return 0
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.get(settings.GROWW_INSTRUMENT_MASTER_URL)
            resp.raise_for_status()
        n = target.load_csv_text(resp.text)
        logger.info(f"Groww instrument master loaded: {n} equity instruments.")
        return n
    except Exception as exc:
        logger.error(f"Groww instrument master could not be loaded ({type(exc).__name__}); Groww orders will be refused.")
        return 0
