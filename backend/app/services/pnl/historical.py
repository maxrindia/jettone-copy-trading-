"""Historical (non-Today) realized P&L, computed from Jettone's own `orders`
table - the one gap in coverage that data Jettone already stores can
actually close, unlike the broker's live order/position book (which the
broker only ever exposes for the CURRENT trading day - see pnl/service.py's
module docstring: "PnlService does not [support historical ranges]: it
always reads the broker's CURRENT order/position book, which has no
historical query capability this codebase has confirmed").

Every MASTER/FOLLOWER order Jettone has ever observed - whether placed via
its own UI (INTERNAL_UI), copied by the engine (SYSTEM), or placed on the
Alice Blue Mobile/Web app and picked up by MasterOrderPoller/engine_loop's
websocket handler (BROKER_EXTERNAL) - is persisted to the `orders` table
with its fill price, quantity, side and an (approximate, DB-insert)
timestamp. That is a real, if imperfect, fill ledger for any day Jettone was
actually running.

What this module deliberately does NOT attempt, ever:
  - Unrealized P&L for a past day. That needs the account's end-of-day
    position AND that instrument's market price at that moment. Jettone
    stores neither anywhere in its schema (no position-snapshot table, no
    price-history table - see app/models/) and the broker cannot supply a
    historical LTP either. Never approximated with today's LTP.
  - Realized P&L across an unverified overnight carry. There is no stored
    broker position snapshot for any past day, so the day's starting
    position can never be read the way PnlService reads
    BrokerPosition.overnight_quantity for Today. Instead, the flat-at-open
    assumption is checked AFTER THE FACT: every product this platform
    trades is MIS/intraday (square-off by design - CNC/NRML carry products
    are excluded up front as unsupported here), so replaying a day's own
    fills for one instrument from a flat position must land back on a
    net-zero position. A non-zero ending position proves that assumption
    was wrong for that instrument that day, and the entire account's result
    for the whole requested range is reported unavailable rather than a
    partially-wrong number - the same all-or-nothing rule
    pnl/calculator.py's compute_pnl already applies per account, extended
    here across the days in the range.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, Iterable, List, NamedTuple, Optional

from app.services.pnl.broker_data import InstrumentKey
from app.services.pnl.calculator import CASH_EQUITY_EXCHANGES, ZERO, InstrumentPnl

# This platform only trades intraday (square-off by design). A CNC/NRML
# fill is a genuine overnight-carry product, not a data gap - excluded up
# front rather than relying on the net-zero check to catch it indirectly.
_INTRADAY_PRODUCTS = frozenset({"MIS", "INTRADAY"})


class HistoricalFill(NamedTuple):
    """One of Jettone's own `orders` rows, reduced to what this module needs.
    `created_at` is Jettone's DB-insert time, not a true broker exchange
    timestamp (Order has no such column) - close enough to order fills for
    both day-bucketing and same-day sequencing, since MasterOrderPoller
    persists within seconds of the broker showing the fill."""
    account_id: str
    exchange: str
    symbol: str
    product_type: str
    side: str  # "BUY" / "SELL"
    quantity: int
    price: Decimal
    created_at: datetime
    day: date  # created_at's trading-day (IST), precomputed by the caller


@dataclass(frozen=True)
class HistoricalRealizedPoint:
    time: datetime
    symbol: str
    side: str
    quantity: int
    cumulative_realized: Decimal


@dataclass(frozen=True)
class HistoricalRealizedResult:
    realized: Optional[Decimal]
    points: List[HistoricalRealizedPoint] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.realized is not None


# NSE index symbols never carry a -EQ suffix, on either side of the
# comparison - matches the exemption already established in
# app/services/alice_blue/instruments.py::resolve_instrument_info.
_NSE_INDEX_PREFIXES = ("NIFTY", "BANKNIFTY", "FINNIFTY")


def _canonical_symbol(exchange: str, symbol: str) -> str:
    """
    Groups equivalent NSE cash-equity symbol spellings ("RELIANCE" and
    "RELIANCE-EQ") into one canonical form for instrument-key grouping.

    This is the exact NSE "+EQ" rule already established and in production
    use at app/services/alice_blue/instruments.py::resolve_instrument_info
    (KNOWN_INSTRUMENTS maps both "RELIANCE" and "RELIANCE-EQ" to the same
    ("2885", "RELIANCE-EQ") pair) - reused here as a small, local, pure
    string rule rather than calling that function directly, since it is
    async and does Redis/HTTP I/O for live order placement, which this
    module (see its own docstring: "No I/O and no rounding") must never do.

    Deliberately NOT extended to BSE: KNOWN_INSTRUMENTS shows BSE equities
    canonically have NO -EQ suffix at all (e.g. ("BSE", "RELIANCE") ->
    "RELIANCE", never "RELIANCE-EQ") - appending or stripping "-EQ" for BSE
    would be guessing at a convention that isn't actually this project's.
    Options/futures (NFO) never reach this function - they are already
    excluded upstream by CASH_EQUITY_EXCHANGES before grouping happens.
    """
    sym = symbol.upper().strip()
    if exchange == "NSE" and not sym.endswith("-EQ") and not any(sym.startswith(p) for p in _NSE_INDEX_PREFIXES):
        return f"{sym}-EQ"
    return sym


def _instrument_key(f: HistoricalFill) -> InstrumentKey:
    # Jettone's own orders carry no separate broker instrumentId - the
    # (exchange, canonical_symbol, product) triple is a consistent key
    # within this single table, unlike broker_data.py's cross-book join
    # concern. canonical_symbol collapses the "RELIANCE" vs "RELIANCE-EQ"
    # spelling split observed between INTERNAL_UI and BROKER_EXTERNAL
    # order rows for the same real fill (see historical P&L investigation).
    return InstrumentKey(f.exchange, _canonical_symbol(f.exchange, f.symbol), f.product_type)


def compute_historical_realized(fills: Iterable[HistoricalFill]) -> HistoricalRealizedResult:
    """
    Realized P&L across every calendar day present in `fills`, replaying
    each (day, instrument) independently from a flat position (see module
    docstring for why that is verified, not assumed, per group).

    All-or-nothing per call: any fill this cannot cost safely - a
    non-cash-equity exchange, a carry product, a missing/non-positive fill
    price, or a day/instrument that does not end flat - withholds the
    entire result (realized=None) rather than silently omitting that one
    fill's effect from an otherwise-plausible-looking total.
    """
    notes: List[str] = []
    valid: List[HistoricalFill] = []

    for f in fills:
        if f.exchange not in CASH_EQUITY_EXCHANGES:
            notes.append(f"UNSUPPORTED_INSTRUMENT:{f.exchange}:{f.symbol}")
            continue
        if f.product_type not in _INTRADAY_PRODUCTS:
            notes.append(f"CARRY_PRODUCT_UNSUPPORTED:{f.product_type}:{f.symbol}:{f.day.isoformat()}")
            continue
        if f.price is None or f.price <= 0:
            notes.append(f"MISSING_FILL_PRICE:{f.symbol}:{f.created_at.isoformat()}")
            continue
        if f.quantity <= 0:
            continue
        valid.append(f)

    if notes:
        return HistoricalRealizedResult(None, notes=notes)

    # One true chronological pass across every instrument, exactly like the
    # live compute_realized_timeline - never grouped-then-concatenated,
    # which would misorder the cumulative curve whenever more than one
    # instrument traded the same day.
    valid.sort(key=lambda f: f.created_at)

    instruments: Dict[InstrumentKey, InstrumentPnl] = {}
    last_day_by_instrument: Dict[InstrumentKey, date] = {}
    total_realized = ZERO
    points: List[HistoricalRealizedPoint] = []

    for f in valid:
        key = _instrument_key(f)
        inst = instruments.get(key)
        if inst is None:
            inst = instruments[key] = InstrumentPnl(key=key, symbol=f.symbol)

        prev_day = last_day_by_instrument.get(key)
        if prev_day is not None and f.day != prev_day and inst.net_quantity != 0:
            notes.append(f"CARRY_SUSPECTED:{key.instrument_id}:{prev_day.isoformat()}")
        last_day_by_instrument[key] = f.day

        before = inst.realized
        inst.apply_fill(f.side, f.quantity, f.price)
        total_realized += inst.realized - before
        points.append(
            HistoricalRealizedPoint(
                time=f.created_at,
                symbol=f.symbol,
                side=f.side,
                quantity=f.quantity,
                cumulative_realized=total_realized,
            )
        )

    # The day-transition check above never fires for the LAST day each
    # instrument traded (there is no later fill to trigger it) - check those
    # separately.
    for key, inst in instruments.items():
        if inst.net_quantity != 0:
            notes.append(f"CARRY_SUSPECTED:{key.instrument_id}:{last_day_by_instrument[key].isoformat()}")

    if notes:
        return HistoricalRealizedResult(None, notes=notes)

    return HistoricalRealizedResult(total_realized, points=points, notes=[])


# ── per-day (DAILY, not cumulative) realized P&L ────────────────────────────

@dataclass(frozen=True)
class DailyPnl:
    """One trading day's own realized P&L for one account - that day's fills
    only, replayed from a flat position; never a running total across days.

    status:
      OK          every fill that day was costed.
      PARTIAL     `daily_pnl` is real but incomplete: some fills that day
                  could not be costed (e.g. an NFO fill with no contract
                  identity) or an instrument ended the day non-flat (its
                  unrealized part has no historical mark price). The
                  uncostable part is NEVER treated as ₹0 - it is named in
                  `reasons`.
      UNAVAILABLE nothing that day could be costed; `daily_pnl` is None.
    """
    day: date
    daily_pnl: Optional[Decimal]
    status: str
    reasons: List[str] = field(default_factory=list)


def compute_historical_daily(fills: Iterable[HistoricalFill]) -> List[DailyPnl]:
    """Realized P&L per trading day, each day isolated from every other: one
    unsupported/unverifiable fill degrades only its own day (or only its own
    instrument within that day), never the rest of the range.

    Same cost model as compute_historical_realized / the live calculator
    (InstrumentPnl.apply_fill: weighted-average cost, gross, actual filled
    quantity and price) applied per (day, instrument) from a flat open.
    Cash-equity (NSE/BSE) fills are costed whatever their product
    (INTRADAY / LONGTERM / ...) - the same-day flat check, not the product
    label, is what proves a round trip. Fills this cannot cost are excluded
    and reported, never guessed.
    """
    by_day: Dict[date, List[HistoricalFill]] = {}
    for f in fills:
        if f.quantity <= 0:
            continue
        by_day.setdefault(f.day, []).append(f)

    out: List[DailyPnl] = []
    for day in sorted(by_day):
        reasons: List[str] = []

        def note(reason: str) -> None:
            if reason not in reasons:
                reasons.append(reason)

        groups: Dict[InstrumentKey, List[HistoricalFill]] = {}
        for f in sorted(by_day[day], key=lambda x: x.created_at):
            if f.exchange not in CASH_EQUITY_EXCHANGES:
                # `orders` stores no contract identity (strike/expiry/token)
                # for derivatives, so fills of different contracts under one
                # underlying symbol cannot be told apart or costed.
                note(f"UNSUPPORTED_INSTRUMENT:{f.exchange}:{f.symbol}")
                continue
            groups.setdefault(_instrument_key(f), []).append(f)

        total = ZERO
        costed_groups = 0
        for key, gfills in groups.items():
            if any(g.price is None or g.price <= 0 for g in gfills):
                note(f"MISSING_FILL_PRICE:{gfills[0].symbol}")
                continue
            inst = InstrumentPnl(key=key, symbol=gfills[0].symbol)
            for g in gfills:
                inst.apply_fill(g.side, g.quantity, g.price)
            total += inst.realized
            costed_groups += 1
            if inst.net_quantity != 0:
                note(f"OPEN_POSITION_AT_DAY_END:{gfills[0].symbol}:{inst.net_quantity}")

        if costed_groups == 0:
            out.append(DailyPnl(day, None, "UNAVAILABLE", reasons))
        else:
            out.append(DailyPnl(day, total, "PARTIAL" if reasons else "OK", reasons))
    return out
