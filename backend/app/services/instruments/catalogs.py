"""
Read-only catalog adapters (R4b): reshape what the existing instrument stores ALREADY hold into R4a's `BrokerView`.

Synchronous and in-memory: no broker call, no Redis, no database, no network, no clock. Nothing here changes how the
stores load or resolve, and nothing in the engine imports this module.

Honesty rules
-------------
* A catalog is "not loaded" (`candidates=None`) when its store is empty or unavailable. That is never evidence that an
  instrument does not exist.
* `loaded_at` is the store's COMPLETE-snapshot time, or None when completeness cannot be established (rows added
  directly, repeated loads, an empty load). None makes the comparator report MASTER_DATA_STALE, never "fresh".
* Only fields the store really holds are mapped. Everything else stays None (unverified): Angel One has no ISIN, lot or
  tick; Groww has no tick size, and its lot size is mapped only when the CSV actually stated one; Zerodha has no ISIN in its
  instruments dump (so a Zerodha row can only be SYMBOL_ONLY), and its lot size is mapped only when the dump stated one.
* Duplicate rows are passed through as separate candidates so the comparator can answer AMBIGUOUS.
* Alice Blue is a deliberate NOT-LOADED stub: its in-process table mixes contract-master rows with hand-written
  placeholders, keeps no ISIN, and records no provenance. It is not used as catalog data.

`view_for` returns only the rows the comparator could consult (same exchange AND same symbol or same ISIN), which is
exactly what the comparator filters to itself, so the verdict equals comparing against the full catalog.
"""
from typing import Iterable, Optional, Protocol, Tuple

from app.services.instruments.model import BrokerInstrument, BrokerView, CanonicalInstrument
from app.services.instruments.normalizer import normalize_exchange, normalize_isin, normalize_symbol

ALICE_ID, GROWW_ID, ANGEL_ID, ZERODHA_ID = "alicebluenew", "groww", "angelone", "zerodha"


class InstrumentCatalog(Protocol):
    broker_id: str

    def view_for(self, master: CanonicalInstrument) -> BrokerView: ...


def _relevant(master: CanonicalInstrument, rows: Iterable[BrokerInstrument]) -> Tuple[BrokerInstrument, ...]:
    out = []
    for row in rows:
        if normalize_exchange(row.exchange) != master.exchange:
            continue
        symbol, _ = normalize_symbol(row.trading_symbol)
        isin, isin_problem = normalize_isin(row.isin)
        if symbol == master.symbol or (master.isin is not None and not isin_problem and isin == master.isin):
            out.append(row)
    return tuple(out)


class GrowwCatalog:
    broker_id = GROWW_ID

    def __init__(self, store: Optional[object] = None) -> None:
        self._store = store

    def _resolve_store(self):
        if self._store is not None:
            return self._store
        from app.services.groww import instruments as groww_instruments
        return groww_instruments.store             # looked up at call time: always the live process-wide store

    def view_for(self, master: CanonicalInstrument) -> BrokerView:
        store = self._resolve_store()
        if not getattr(store, "size", 0):
            return BrokerView(self.broker_id, None, None)
        rows = (
            BrokerInstrument(
                broker_id=self.broker_id,
                exchange=i.exchange,
                trading_symbol=i.trading_symbol,
                isin=i.isin or None,
                lot_size=i.lot_size if i.lot_size_stated else None,
            )
            for i in store.iter_instruments()
        )
        return BrokerView(self.broker_id, _relevant(master, rows), store.snapshot_loaded_at)


class AngelCatalog:
    broker_id = ANGEL_ID

    def __init__(self, store: Optional[object] = None) -> None:
        self._store = store

    def _resolve_store(self):
        if self._store is not None:
            return self._store
        from app.services.angel_one import instruments as angel_instruments
        return angel_instruments.store

    def view_for(self, master: CanonicalInstrument) -> BrokerView:
        store = self._resolve_store()
        if not getattr(store, "size", 0):
            return BrokerView(self.broker_id, None, None)      # the production store is empty unless loaded by hand
        rows = (
            BrokerInstrument(broker_id=self.broker_id, exchange=exchange, trading_symbol=symbol, token=token)
            for exchange, symbol, token in store.iter_records()
        )
        return BrokerView(self.broker_id, _relevant(master, rows), store.snapshot_loaded_at)


class ZerodhaCatalog:
    """Equity rows of Zerodha's instruments dump. The dump has no ISIN column, so a Zerodha row is matched by exchange + tradingsymbol
    only and can never be a definitive (ISIN) MATCH. Derivative rows are not exposed here: see instruments/contracts.py."""
    broker_id = ZERODHA_ID

    def __init__(self, store: Optional[object] = None) -> None:
        self._store = store

    def _resolve_store(self):
        if self._store is not None:
            return self._store
        from app.services.zerodha import instruments as zerodha_instruments
        return zerodha_instruments.store

    def view_for(self, master: CanonicalInstrument) -> BrokerView:
        store = self._resolve_store()
        if not getattr(store, "size", 0):
            return BrokerView(self.broker_id, None, None)
        rows = (
            BrokerInstrument(
                broker_id=self.broker_id, exchange=i.exchange, trading_symbol=i.tradingsymbol, token=i.instrument_token or None,
                lot_size=i.lot_size if i.lot_size_stated else None, tick_size=i.tick_size,
            )
            for i in store.iter_instruments()
            if not i.is_derivative
        )
        return BrokerView(self.broker_id, _relevant(master, rows), store.snapshot_loaded_at)


class AliceCatalog:
    """Always NOT LOADED in R4b (see the module docstring). It reads nothing."""
    broker_id = ALICE_ID

    def view_for(self, master: CanonicalInstrument) -> BrokerView:
        return BrokerView(self.broker_id, None, None)


def default_catalogs() -> Tuple[InstrumentCatalog, ...]:
    """The four adapters bound to the live process-wide stores, ordered by broker id."""
    return (AliceCatalog(), AngelCatalog(), GrowwCatalog(), ZerodhaCatalog())
