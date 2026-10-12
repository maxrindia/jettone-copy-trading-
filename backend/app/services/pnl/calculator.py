"""Pure gross P&L calculation from broker fills, weighted average cost.

No I/O and no rounding: every value is an exact Decimal. Rounding to 2
decimals happens only when the API response is built (money_str).

Per instrument (exchange + instrumentId + product), fills in exchange-time
order, with s = +1 for BUY / -1 for SELL and a fill of q units at price p:

  opening or adding (flat, or same direction as the position):
      avg = (|qty| * avg + q * p) / (|qty| + q)
      qty = qty + s * q
  reducing (opposite direction):
      c = min(q, |qty|)
      realized += c * (p - avg) * sign(qty)     # long: sell - cost; short: cost - buy
      qty = qty + s * c
      a remainder q - c opens the opposite side at avg = p

  unrealized = qty * (ltp - avg)                # long and short alike
  total      = realized + unrealized            # GROSS: no charges of any kind

Anything that cannot be costed from the available broker data - an overnight
carry, a non-cash-equity instrument, an open position without an LTP, a fill
without a timestamp, or books that disagree on net quantity - is flagged, and
the affected component is reported as None instead of being approximated.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.trading_day import trading_tz
from app.services.pnl.broker_data import BrokerFill, BrokerPosition, InstrumentKey

ZERO = Decimal("0")
CENT = Decimal("0.01")

# Cash equity only (multiplier 1) in this version. Anything else is reported
# as unverified rather than costed with equity arithmetic.
CASH_EQUITY_EXCHANGES = frozenset({"NSE", "BSE"})

# Broker cross-check. Alice Blue reports P&L as float32 values (e.g.
# -1.4800000190734863). After rounding both sides to 2 decimals, a difference
# of up to one paisa is a MATCH, and up to max(Rs 0.05, 1e-6 x the day's
# traded value) is float noise (WITHIN_TOLERANCE).
MATCH_TOLERANCE = Decimal("0.01")
MIN_NOISE_TOLERANCE = Decimal("0.05")
NOISE_TOLERANCE_PER_RUPEE_TRADED = Decimal("0.000001")

CHECK_MATCH = "MATCH"
CHECK_WITHIN_TOLERANCE = "WITHIN_TOLERANCE"
CHECK_MISMATCH = "MISMATCH"
CHECK_NOT_COMPARABLE = "NOT_COMPARABLE"
CHECK_UNAVAILABLE = "UNAVAILABLE"


def round_money(value: Decimal) -> Decimal:
    rounded = value.quantize(CENT, rounding=ROUND_HALF_UP)
    return rounded if rounded != 0 else ZERO.quantize(CENT)  # never "-0.00"


def money_str(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(round_money(value), "f")


@dataclass
class InstrumentPnl:
    key: InstrumentKey
    symbol: str
    net_quantity: int = 0
    average_cost: Decimal = ZERO
    realized: Decimal = ZERO
    unrealized: Optional[Decimal] = None
    buy_value: Decimal = ZERO
    sell_value: Decimal = ZERO
    fills_today: int = 0
    realized_verified: bool = True
    unrealized_verified: bool = True
    notes: List[str] = field(default_factory=list)

    def flag(self, note: str, realized: bool = True, unrealized: bool = True) -> None:
        if realized:
            self.realized_verified = False
        if unrealized:
            self.unrealized_verified = False
        if note not in self.notes:
            self.notes.append(note)

    def apply_fill(self, side: str, quantity: int, price: Decimal) -> None:
        """Weighted-average-cost update for one fill (see module docstring)."""
        step = 1 if side == "BUY" else -1
        value = quantity * price
        if step > 0:
            self.buy_value += value
        else:
            self.sell_value += value
        self.fills_today += 1

        held = abs(self.net_quantity)
        if self.net_quantity == 0 or (self.net_quantity > 0) == (step > 0):
            self.average_cost = (held * self.average_cost + value) / (held + quantity)
            self.net_quantity += step * quantity
            return

        closing = min(quantity, held)
        direction = 1 if self.net_quantity > 0 else -1
        self.realized += closing * (price - self.average_cost) * direction
        self.net_quantity += step * closing
        remainder = quantity - closing
        if self.net_quantity == 0:
            self.average_cost = ZERO
        if remainder > 0:
            self.net_quantity = step * remainder
            self.average_cost = price


@dataclass
class PnlResult:
    realized: Optional[Decimal]
    unrealized: Optional[Decimal]
    total: Optional[Decimal]
    turnover: Decimal
    notes: List[str]
    instruments: List[InstrumentPnl]

    @property
    def fully_verified(self) -> bool:
        return self.total is not None and not self.notes


def _order_id_sort_key(broker_order_id: str) -> Tuple[int, int, str]:
    if broker_order_id.isdigit():
        return (0, int(broker_order_id), "")
    return (1, 0, broker_order_id)


def _check_supported(inst: InstrumentPnl, pos: Optional[BrokerPosition]) -> None:
    if inst.key.exchange not in CASH_EQUITY_EXCHANGES:
        inst.flag(f"UNSUPPORTED_INSTRUMENT:{inst.key.exchange or 'UNKNOWN_EXCHANGE'}")
    if pos is not None and pos.multiplier is not None and pos.multiplier != 1:
        inst.flag("UNSUPPORTED_MULTIPLIER")


def _cross_check_positions(
    instruments: Dict[InstrumentKey, InstrumentPnl],
    positions: Sequence[BrokerPosition],
    instrument: Callable[[InstrumentKey, str], InstrumentPnl],
) -> None:
    rows_by_key: Dict[InstrumentKey, List[BrokerPosition]] = defaultdict(list)
    for pos in positions:
        rows_by_key[pos.key].append(pos)
    for key, rows in rows_by_key.items():
        # Position-only instruments too (e.g. an overnight carry not traded today).
        instrument(key, rows[0].symbol)

    for key, inst in instruments.items():
        rows = rows_by_key.get(key, [])
        if len(rows) > 1:
            inst.flag("DUPLICATE_POSITION_ROWS")
            continue
        pos = rows[0] if rows else None
        _check_supported(inst, pos)

        if pos is None:
            if inst.net_quantity != 0:
                inst.flag("BROKER_POSITION_ROW_MISSING")
        elif pos.overnight_quantity is None:
            inst.flag("OVERNIGHT_QUANTITY_UNAVAILABLE")
        elif pos.overnight_quantity != 0:
            # Today's fills do not contain the carried position's cost basis,
            # so neither what today's trades realized against it nor what
            # is still open can be costed. A carry closed out today is flat
            # per the broker, which is the one thing that can be stated.
            inst.flag(
                "OVERNIGHT_POSITION_COST_BASIS_UNAVAILABLE",
                realized=inst.fills_today > 0,
                unrealized=pos.net_quantity != 0,
            )
            if pos.net_quantity == 0 and inst.unrealized_verified:
                inst.unrealized = ZERO
            continue
        elif pos.net_quantity != inst.net_quantity:
            inst.flag("NET_QUANTITY_MISMATCH")

        if not inst.unrealized_verified:
            continue
        if inst.net_quantity == 0:
            inst.unrealized = ZERO
        elif pos is None or pos.ltp is None or pos.ltp <= 0:
            inst.flag("LTP_UNAVAILABLE", realized=False)
        else:
            inst.unrealized = inst.net_quantity * (pos.ltp - inst.average_cost)


def compute_pnl(
    fills: Iterable[BrokerFill],
    positions: Optional[Sequence[BrokerPosition]],
    day: date,
    account_notes: Iterable[str] = (),
) -> PnlResult:
    """
    Gross P&L for one account and one trading day.

    fills         - the account's own executed fills (parse_order_book_fills)
    positions     - the account's broker position rows, or None when the
                    position book could not be read
    day           - the trading day; a fill whose exchange time falls on
                    any other day never enters the result
    account_notes - parse issues; any of them withholds realized P&L
    """
    parse_issues = list(dict.fromkeys(account_notes))
    notes: List[str] = list(parse_issues)
    tz = trading_tz()
    instruments: Dict[InstrumentKey, InstrumentPnl] = {}
    todays: Dict[InstrumentKey, List[BrokerFill]] = defaultdict(list)

    def instrument(key: InstrumentKey, symbol: str) -> InstrumentPnl:
        inst = instruments.get(key)
        if inst is None:
            inst = instruments[key] = InstrumentPnl(key=key, symbol=symbol)
        return inst

    for fill in fills:
        if fill.exchange_time is None:
            instrument(fill.key, fill.symbol).flag(f"FILL_TIME_UNAVAILABLE:{fill.broker_order_id}")
            continue
        if fill.exchange_time.astimezone(tz).date() != day:
            continue
        if fill.side is None or fill.average_price is None or fill.average_price <= 0:
            instrument(fill.key, fill.symbol).flag(f"FILL_UNPARSEABLE:{fill.broker_order_id}")
            continue
        instrument(fill.key, fill.symbol)
        todays[fill.key].append(fill)

    for key, key_fills in todays.items():
        inst = instruments[key]
        ordered = sorted(key_fills, key=lambda f: (f.exchange_time, _order_id_sort_key(f.broker_order_id)))
        for fill in ordered:
            inst.apply_fill(fill.side, fill.filled_quantity, fill.average_price)

    if positions is None:
        notes.append("BROKER_POSITIONS_UNAVAILABLE")
        for inst in instruments.values():
            _check_supported(inst, None)
            if inst.net_quantity != 0:
                # No LTP without the position book, and a prior-day carry in
                # the same instrument cannot be ruled out, so not even the
                # realized part is reported for an open instrument.
                inst.flag("OPEN_POSITION_WITHOUT_BROKER_POSITIONS")
            inst.unrealized_verified = False
    else:
        _cross_check_positions(instruments, positions, instrument)

    all_instruments = list(instruments.values())
    for inst in all_instruments:
        label = inst.symbol or inst.key.instrument_id
        for note in inst.notes:
            tagged = f"{label}: {note}"
            if tagged not in notes:
                notes.append(tagged)

    realized: Optional[Decimal] = None
    if not parse_issues and all(i.realized_verified for i in all_instruments):
        realized = sum((i.realized for i in all_instruments), ZERO)

    unrealized: Optional[Decimal] = None
    if positions is not None and all(i.unrealized_verified for i in all_instruments):
        unrealized = sum((i.unrealized for i in all_instruments), ZERO)

    total = realized + unrealized if realized is not None and unrealized is not None else None
    turnover = sum((i.buy_value + i.sell_value for i in all_instruments), ZERO)
    return PnlResult(realized, unrealized, total, turnover, notes, all_instruments)


@dataclass(frozen=True)
class ComponentCheck:
    jettone: Optional[Decimal]
    broker: Optional[Decimal]
    diff: Optional[Decimal]
    status: str


@dataclass(frozen=True)
class BrokerCheck:
    status: str
    tolerance: Optional[Decimal]
    realized: ComponentCheck
    unrealized: ComponentCheck
    total: ComponentCheck


def _broker_sum(values: List[Optional[Decimal]]) -> Optional[Decimal]:
    if any(v is None for v in values):
        return None
    return sum(values, ZERO)


def _compare(jettone: Optional[Decimal], broker: Optional[Decimal], tolerance: Decimal) -> ComponentCheck:
    if jettone is None or broker is None:
        return ComponentCheck(jettone, broker, None, CHECK_NOT_COMPARABLE)
    diff = round_money(jettone) - round_money(broker)
    if abs(diff) <= MATCH_TOLERANCE:
        status = CHECK_MATCH
    elif abs(diff) <= tolerance:
        status = CHECK_WITHIN_TOLERANCE
    else:
        status = CHECK_MISMATCH
    return ComponentCheck(jettone, broker, diff, status)


@dataclass(frozen=True)
class RealizedPoint:
    """One step of an account's cumulative realized P&L today - the running
    total immediately after one fill was applied, across every instrument in
    the account."""
    time: datetime
    broker_order_id: str
    symbol: str
    side: str
    quantity: int
    cumulative_realized: Decimal


def compute_realized_timeline(
    fills: Iterable[BrokerFill], day: date
) -> Tuple[List[RealizedPoint], List[str]]:
    """
    Cumulative realized P&L after each of today's fills, in exchange-time
    order, across every instrument in this account. Reuses the same
    weighted-average-cost step as compute_pnl (InstrumentPnl.apply_fill), so
    the last point's cumulative_realized equals compute_pnl(...).realized
    whenever every fill was parseable and no other note applies.

    This is a dashboard chart data source, not a P&L figure: it never
    invents a historical mark-to-market/unrealized curve (no LTP history
    exists), only the realized side, which today's own fills fully
    determine. A fill that cannot be interpreted (bad side/price, missing
    exchange time) is skipped and reported as a note rather than guessed -
    the timeline then has one fewer point, and the caller must not present
    it as complete when notes is non-empty.
    """
    tz = trading_tz()
    todays: List[BrokerFill] = []
    notes: List[str] = []
    for fill in fills:
        if fill.exchange_time is None:
            notes.append(f"FILL_TIME_UNAVAILABLE:{fill.broker_order_id}")
            continue
        if fill.exchange_time.astimezone(tz).date() != day:
            continue
        if fill.side is None or fill.average_price is None or fill.average_price <= 0:
            notes.append(f"FILL_UNPARSEABLE:{fill.broker_order_id}")
            continue
        todays.append(fill)

    ordered = sorted(todays, key=lambda f: (f.exchange_time, _order_id_sort_key(f.broker_order_id)))
    instruments: Dict[InstrumentKey, InstrumentPnl] = {}
    points: List[RealizedPoint] = []
    running_total = ZERO
    for fill in ordered:
        inst = instruments.get(fill.key)
        if inst is None:
            inst = instruments[fill.key] = InstrumentPnl(key=fill.key, symbol=fill.symbol)
        before = inst.realized
        inst.apply_fill(fill.side, fill.filled_quantity, fill.average_price)
        running_total += inst.realized - before
        points.append(
            RealizedPoint(
                time=fill.exchange_time,
                broker_order_id=fill.broker_order_id,
                symbol=fill.symbol,
                side=fill.side,
                quantity=fill.filled_quantity,
                cumulative_realized=running_total,
            )
        )
    return points, notes


def reconcile_with_broker(
    result: Optional[PnlResult],
    positions: Optional[Sequence[BrokerPosition]],
) -> BrokerCheck:
    """
    Cross-check Jettone's values against the broker's own figures:
    realized <-> sum(realizedPnl), unrealized <-> sum(unrealizedPnl),
    total <-> sum(MtoM). The broker values are reported alongside, never
    substituted for Jettone's.
    """
    realized = result.realized if result else None
    unrealized = result.unrealized if result else None
    total = result.total if result else None

    if positions is None:
        def unavailable(value: Optional[Decimal]) -> ComponentCheck:
            return ComponentCheck(value, None, None, CHECK_UNAVAILABLE)

        return BrokerCheck(CHECK_UNAVAILABLE, None, unavailable(realized), unavailable(unrealized), unavailable(total))

    turnover = result.turnover if result else ZERO
    tolerance = max(MIN_NOISE_TOLERANCE, turnover * NOISE_TOLERANCE_PER_RUPEE_TRADED)
    checks = (
        _compare(realized, _broker_sum([p.realized_pnl for p in positions]), tolerance),
        _compare(unrealized, _broker_sum([p.unrealized_pnl for p in positions]), tolerance),
        _compare(total, _broker_sum([p.mtm for p in positions]), tolerance),
    )
    statuses = {c.status for c in checks}
    if CHECK_MISMATCH in statuses:
        overall = CHECK_MISMATCH
    elif CHECK_WITHIN_TOLERANCE in statuses:
        overall = CHECK_WITHIN_TOLERANCE
    elif CHECK_MATCH in statuses:
        overall = CHECK_MATCH
    else:
        overall = CHECK_NOT_COMPARABLE
    return BrokerCheck(overall, tolerance, *checks)
