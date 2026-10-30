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

Loading (the store is never marked ready by an empty or unreadable download)
----------------------------------------------------------------------------
`ensure_loaded` is the one entry point, used at startup and lazily before a Groww order. It does nothing unless GROWW_ENABLED and
GROWW_INSTRUMENT_MASTER_URL are set. A download is parsed into a separate staging store, validated (required columns present, at
least a minimum number of equity rows), and only then swapped in as one unit, so a failed, malformed, empty or truncated refresh can
never corrupt a previously valid catalog and a refresh never accumulates duplicate rows. Concurrent callers share one download. A
catalog older than SOFT_MAX_AGE is refreshed in the background while the current one keeps serving; older than HARD_MAX_AGE it is
refused (the master may have changed), and a failed attempt is not repeated for FAILURE_COOLDOWN.
"""

import asyncio
import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

import httpx

from app.core.config import settings
from app.services.alice_blue.instruments import InstrumentResolutionError

logger = logging.getLogger("groww_instruments")

_EXCHANGES = ("NSE", "BSE")
_TRUE = {"true", "1", "yes", "y", "t"}
_FALSE = {"false", "0", "no", "n", "f"}

REQUIRED_COLUMNS = ("exchange", "trading_symbol", "instrument_type", "segment", "buy_allowed", "sell_allowed")
MIN_EQUITY_ROWS = 1000                       # the live file holds ~12,800; far fewer means a truncated or wrong document
SOFT_MAX_AGE = timedelta(hours=12)           # past this a refresh is started in the background
HARD_MAX_AGE = timedelta(hours=36)           # past this the catalog is refused until a refresh succeeds
FAILURE_COOLDOWN = timedelta(seconds=60)     # no download retry sooner than this after a failure
FETCH_TIMEOUT_SECONDS = 60.0
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024


@dataclass(frozen=True)
class GrowwInstrument:
    exchange: str
    trading_symbol: str
    isin: str
    lot_size: int
    freeze_quantity: int          # 0 = not provided
    buy_allowed: Optional[bool]   # None = unreadable (treated as not allowed)
    sell_allowed: Optional[bool]
    # False when the CSV gave no usable lot size and `lot_size` is only the coerced default of 1 (R4b).
    lot_size_stated: bool = False


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
    def __init__(self, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._by_key: Dict[Tuple[str, str], List[GrowwInstrument]] = {}
        self.size = 0
        # Freshness metadata for read-only consumers (R4b); it does not influence loading or resolve().
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._load_calls = 0
        self._snapshot_at: Optional[datetime] = None
        self._refresh_lock = asyncio.Lock()
        self._last_failure_at: Optional[datetime] = None
        self._background: Optional["asyncio.Future"] = None

    @property
    def snapshot_loaded_at(self) -> Optional[datetime]:
        """When a COMPLETE catalog snapshot finished loading, else None. Loading accumulates, so only the case
        where exactly one full-document load (load_csv_text / load_file) populated the store counts. Any other
        history - rows added directly, a second load, a failed or empty load - leaves the freshness unknown."""
        return self._snapshot_at if self._load_calls == 1 else None

    def _age(self) -> Optional[timedelta]:
        return None if self._snapshot_at is None else self._clock() - self._snapshot_at

    @property
    def is_ready(self) -> bool:
        """Loaded and not past HARD_MAX_AGE. A catalog with unknown freshness (rows added directly) counts as ready."""
        if not self.size:
            return False
        age = self._age()
        return age is None or age <= HARD_MAX_AGE

    def replace_from_csv_text(self, text: str, *, min_rows: int = 1) -> int:
        """Parse `text` into a staging store, validate it, then swap it in as one unit. Raises ValueError (the store is untouched)
        for a missing/renamed required column, no readable rows, or fewer than `min_rows` equity rows."""
        reader = csv.DictReader(io.StringIO(text))
        columns = set(reader.fieldnames or ())
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise ValueError("instrument master is missing required column(s): " + ", ".join(missing))
        staging = GrowwInstrumentStore(self._clock)
        added = staging.load_rows(reader)
        if added <= 0 or added < min_rows:
            raise ValueError(f"instrument master holds {added} NSE/BSE equity row(s); expected at least {max(1, min_rows)}")
        self._by_key = staging._by_key
        self.size = staging.size
        self._load_calls = 1                 # one complete document: the snapshot time is meaningful
        self._snapshot_at = self._clock()
        return added

    async def _download_and_replace(self, fetch: Optional[Callable[[str], Awaitable[str]]]) -> int:
        text = await (fetch or _fetch_text)(settings.GROWW_INSTRUMENT_MASTER_URL)
        return self.replace_from_csv_text(text, min_rows=MIN_EQUITY_ROWS)

    async def _refresh(self, fetch: Optional[Callable[[str], Awaitable[str]]]) -> bool:
        """One download under the lock. True when a valid catalog is now loaded. Any failure keeps the previous catalog."""
        async with self._refresh_lock:
            age = self._age()
            if self.size and age is not None and age < SOFT_MAX_AGE:
                return True                                  # another caller refreshed while this one waited
            now = self._clock()
            if self._last_failure_at is not None and now - self._last_failure_at < FAILURE_COOLDOWN:
                return self.is_ready
            try:
                n = await self._download_and_replace(fetch)
            except Exception as exc:
                self._last_failure_at = now
                logger.error(f"Groww instrument master could not be loaded ({type(exc).__name__}); "
                             f"{'keeping the previous catalog' if self.size else 'Groww orders will be refused'}.")
                return self.is_ready
            self._last_failure_at = None
            logger.info(f"Groww instrument master loaded: {n} equity instruments.")
            return True

    async def ensure_loaded(self, fetch: Optional[Callable[[str], Awaitable[str]]] = None) -> bool:
        """True when a usable catalog is loaded. Does nothing (no network) unless GROWW_ENABLED and the URL are set. An empty store
        waits for one shared download; a soft-stale one is refreshed in the background and keeps serving meanwhile."""
        if not settings.GROWW_ENABLED or not settings.GROWW_INSTRUMENT_MASTER_URL:
            return self.is_ready
        age = self._age()
        if self.size and age is not None and age < SOFT_MAX_AGE:
            return True
        if self.is_ready and age is not None:               # soft-stale but still within HARD_MAX_AGE: serve, refresh behind
            if self._background is None or self._background.done():
                self._background = asyncio.ensure_future(self._refresh(fetch))
            return True
        return await self._refresh(fetch)

    def iter_instruments(self) -> Tuple[GrowwInstrument, ...]:
        return tuple(i for key in sorted(self._by_key) for i in self._by_key[key])

    def lookup_symbol(self, exchange: str, symbol: str) -> Tuple[GrowwInstrument, ...]:
        """Every stored row for the exact (exchange, trading_symbol), duplicates included. No suffix handling."""
        return tuple(self._by_key.get((str(exchange or "").strip().upper(), str(symbol or "").strip().upper()), ()))

    def load_rows(self, rows: Iterable[Dict[str, str]]) -> int:
        self._load_calls += 1
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
            lot = _int(r.get("lot_size"))
            inst = GrowwInstrument(
                exchange=exch, trading_symbol=sym, isin=isin,
                lot_size=max(1, lot), freeze_quantity=max(0, _int(r.get("freeze_quantity"))),
                buy_allowed=_flag(r.get("buy_allowed")), sell_allowed=_flag(r.get("sell_allowed")),
                lot_size_stated=lot > 0,
            )
            self._by_key.setdefault((exch, sym), []).append(inst)
            added += 1
        self.size += added
        return added

    def load_csv_text(self, text: str) -> int:
        first_load = self._load_calls == 0
        added = self.load_rows(csv.DictReader(io.StringIO(text)))
        if first_load and added > 0:
            self._snapshot_at = self._clock()      # the whole document parsed; set only after load_rows returned
        return added

    def load_file(self, path: str) -> int:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return self.load_csv_text(fh.read())

    def resolve(self, exchange: str, symbol: str) -> GrowwInstrument:
        exch = str(exchange or "").strip().upper()
        if exch not in _EXCHANGES:
            raise InstrumentResolutionError(f"Groww instrument mapping supports NSE/BSE equity only (got {exchange!r}).")
        if not self.size:
            raise InstrumentResolutionError("Groww instrument master is not loaded.")
        if not self.is_ready:
            raise InstrumentResolutionError("Groww instrument master is stale; it must be refreshed before an order.")
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


async def _fetch_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as http:
        resp = await http.get(url)
        resp.raise_for_status()
    if len(resp.content) > MAX_DOWNLOAD_BYTES:
        raise ValueError("instrument master download is larger than the allowed size")
    return resp.text


async def load_instruments_if_enabled(target: Optional[GrowwInstrumentStore] = None) -> int:
    """Startup load: downloads Groww instrument master only when GROWW_ENABLED and GROWW_INSTRUMENT_MASTER_URL are both set, through
    the store validated, atomic, single-flight `ensure_loaded`. Returns the number of instruments now loaded (0 when disabled or
    when the load failed, which leaves the store empty so every Groww order is refused)."""
    target = target if target is not None else store
    if not settings.GROWW_ENABLED or not settings.GROWW_INSTRUMENT_MASTER_URL:
        return 0
    await target.ensure_loaded()
    return target.size if target.is_ready else 0
