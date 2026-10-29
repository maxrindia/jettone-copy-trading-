"""
Read-only preview of what the copy pipeline WOULD decide for one Master order and one Child broker.

For OBSERVE-ONLY testing of a pair that is not enabled - first Alice Blue Master -> Groww Child. In observe-only mode the engine loop
stops every new Master event before the pipeline (EngineLoop: "[live-trading OFF] Would process ..."), and a Groww Child is refused at
membership (CHILD_BROKER_UNSUPPORTED / BROKER_PAIR_NOT_SUPPORTED) and at routing (CHILD_BROKER_NOT_READY). This module lets the rest of
the path be examined without changing any of that.

It never sends and never writes: no database write, no Redis, broker call, client or session (preview_child_order reads only
in-memory catalogs; observe_child_previews additionally READS the Master's strategy and the accounts). `would_send` is always False;
the pipeline stays the only sender. It applies the pipeline's own pre-send checks with the pipeline's own functions, and it does NOT stop at the first
failure - every blocker is listed, so a closed pair shows everything that would still stand in the way once it were opened:

  UNSUPPORTED_EVENT          not a new LIMIT / SL / SL-M / MARKET entry (modify, cancel and fills act on existing orders)
  CHILD_BROKER_NOT_READY     registry role gate, as at routing time (registry.can_be_child)
  BROKER_PAIR_NOT_SUPPORTED  explicit pair allowlist and its evidence (pairs.check_pair)
  INSTRUMENT_UNMAPPED        cross-broker derivative refusal (planner); an Alice Child's exact contract-master token; Groww's own catalog
  ZERO_LOT_RESULT            the Child quantity is not positive
  ADAPTER_REFUSED            the Groww adapter's own order validation (side allowed, freeze, lot, product, order type, prices)

Not previewed (each is reported as a note, never as a pass): funds and the risk engine, the SELL position check and the duplicate
claim - all need live Child data or a database write. Child quantity uses the multiplier formula (the pipeline's no-CopySetting
path); capital-based quantity modes need the Child's live funds. Adapter-level checks exist for Groww only.
"""
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderType
from app.services.brokers.pairs import check_pair
from app.services.brokers.registry import can_be_child, normalize_broker_id
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import child_broker_product
from app.services.instruments.planner import (
    ALICE_ID, alice_cross_broker_token, alice_unresolved_skip, cross_broker_derivative_skip,
)

GROWW_ID = "groww"
UNSUPPORTED_EVENT = "UNSUPPORTED_EVENT"
CHILD_BROKER_NOT_READY = "CHILD_BROKER_NOT_READY"
BROKER_PAIR_NOT_SUPPORTED = "BROKER_PAIR_NOT_SUPPORTED"
INSTRUMENT_UNMAPPED = "INSTRUMENT_UNMAPPED"
ZERO_LOT_RESULT = "ZERO_LOT_RESULT"
ADAPTER_REFUSED = "ADAPTER_REFUSED"
# The blockers that only say "this pair / broker is not enabled"; everything else is a property of the order itself.
GATE_CODES = frozenset({CHILD_BROKER_NOT_READY, BROKER_PAIR_NOT_SUPPORTED})


@dataclass(frozen=True)
class PreviewBlocker:
    code: str
    detail: str


@dataclass(frozen=True)
class ChildPreview:
    master_broker: Optional[str]
    child_broker: Optional[str]
    child_order_type: Optional[str]
    child_quantity: Optional[int]
    blockers: Tuple[PreviewBlocker, ...]
    order_body: Optional[Dict[str, Any]] = None        # the adapter's body with a placeholder reference; never sent
    notes: Tuple[str, ...] = ()
    would_send: bool = field(default=False, init=False)

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(b.code for b in self.blockers)

    @property
    def order_ok_if_enabled(self) -> bool:
        """True when the only blockers are the closed gates: the ORDER itself passed every previewed check. Not a readiness verdict
        for the pair - funds, risk, the SELL guard and live evidence are outside this preview."""
        return all(b.code in GATE_CODES for b in self.blockers)


def _child_order_type(event: MasterTradeEvent) -> Optional[str]:
    kind = str(event.event_type or "").upper()
    if kind == "LIMIT_CREATED":
        return "LIMIT"
    if kind == "SL_PENDING_CREATED":
        return "SL" if str(event.order_type or "").upper() == "SL" else "SLM"
    if kind == "MARKET_ACCEPTED":
        return "LIMIT" if settings.MARKET_ORDER_EXECUTION_MODE == "MARKETABLE_LIMIT" else "MARKET"
    return None


def _groww_body(event: MasterTradeEvent, child_type: str, quantity: int, store: Any) -> Dict[str, Any]:
    """The Groww adapter's own validation and mapping (GrowwClient.build_order_body), run on its instrument store alone - no client,
    session or HTTP exists here. Raises exactly what the adapter raises before it would send."""
    from app.services.groww.client import GrowwClient
    if store is None:
        from app.services.groww.instruments import store as default_store
        store = default_store
    market = str(event.event_type or "").upper() == "MARKET_ACCEPTED"
    price = event.limit_price if event.limit_price and event.limit_price > 0 else event.price
    otype = {"LIMIT": OrderType.LIMIT, "SL": OrderType.SL, "SLM": OrderType.SL_M, "MARKET": OrderType.MARKET}[child_type]
    body = GrowwClient.build_order_body(
        SimpleNamespace(_instruments=store),
        symbol=event.symbol, exchange=event.exchange, side=str(event.side).upper(), quantity=quantity,
        product_type=child_broker_product(event.product_type, market_path=market), order_type=otype,
        price=Decimal(price or 0), trigger_price=Decimal(event.trigger_price or 0), reference="PREVIEW",
    )
    return body


def preview_child_order(
    event: MasterTradeEvent, *, master_broker: Optional[str], child_broker: Optional[str],
    multiplier: Any = Decimal("1"), groww_store: Any = None, alice_store: Any = None,
) -> ChildPreview:
    """Every pre-send blocker the pipeline would apply to this Child for this Master entry. Pure apart from reading in-memory
    catalogs; never raises."""
    master = normalize_broker_id(master_broker) if master_broker is not None else None
    child = normalize_broker_id(child_broker)
    blockers = []
    notes = ["funds, risk engine, SELL position check and duplicate claim are not previewed (need live Child data or a DB write)"]
    body: Optional[Dict[str, Any]] = None
    quantity: Optional[int] = None

    child_type = _child_order_type(event)
    if child_type is None:
        blockers.append(PreviewBlocker(UNSUPPORTED_EVENT, f"{event.event_type} is not a new entry this preview evaluates"))

    if not can_be_child(child_broker):
        blockers.append(PreviewBlocker(CHILD_BROKER_NOT_READY, f"{child or child_broker}: not cleared to receive copied orders"))
    pair = check_pair(master_broker, child_broker)
    if not pair.supported:
        blockers.append(PreviewBlocker(BROKER_PAIR_NOT_SUPPORTED, pair.detail))

    try:
        quantity = int(Decimal(str(event.quantity)) * Decimal(str(multiplier)))
    except (InvalidOperation, TypeError, ValueError):
        quantity = 0
    if quantity <= 0:
        blockers.append(PreviewBlocker(ZERO_LOT_RESULT, f"child quantity {quantity} is not positive"))

    derivative = cross_broker_derivative_skip(master_broker=master, child_broker=child_broker, exchange=event.exchange)
    if derivative is not None:
        blockers.append(PreviewBlocker(INSTRUMENT_UNMAPPED, derivative.detail))
    elif child == ALICE_ID and master is not None and master != ALICE_ID:
        unresolved = alice_unresolved_skip(alice_cross_broker_token(exchange=event.exchange, symbol=event.symbol, store=alice_store))
        if unresolved is not None:
            blockers.append(PreviewBlocker(INSTRUMENT_UNMAPPED, unresolved.detail))
    elif child == GROWW_ID and child_type is not None and quantity > 0:
        if child_type == "LIMIT" and str(event.event_type).upper() == "MARKET_ACCEPTED" and not (event.price and event.price > 0):
            notes.append("adapter body not checked: a MARKETABLE_LIMIT price is only known at fill time")
        else:
            try:
                body = _groww_body(event, child_type, quantity, groww_store)
            except InstrumentResolutionError as exc:
                blockers.append(PreviewBlocker(INSTRUMENT_UNMAPPED, f"GROWW:{str(exc)[:180]}"))
            except Exception as exc:                                   # the adapter's own refusal, or an unreadable field
                blockers.append(PreviewBlocker(ADAPTER_REFUSED, f"GROWW:{str(exc)[:180] or type(exc).__name__}"))
    elif child not in (GROWW_ID, ALICE_ID):
        notes.append(f"no adapter-level preview for {child or child_broker}")

    if str(event.side).upper() == "SELL":
        notes.append("SELL: the Child's live position book decides at send time (SELL_POSITION_SAFETY_BYPASS is not consulted here)")
    return ChildPreview(master, child, child_type, quantity, tuple(blockers), body, tuple(notes))


# ── observe-only engine hook ──────────────────────────────────────────────────────────────────────────────────────────────────

# The Master events that would create NEW Child orders; only these are previewed.
ENTRY_EVENTS = frozenset({"LIMIT_CREATED", "SL_PENDING_CREATED", "MARKET_ACCEPTED"})
NOT_A_STRATEGY_MEMBER = "NOT_A_STRATEGY_MEMBER"
MEMBER_TRADING_DISABLED = "MEMBER_TRADING_DISABLED"
STRATEGY_TRADING_DISABLED = "STRATEGY_TRADING_DISABLED"
NO_STRATEGY = "NO_STRATEGY"
# Fields of the Child order body that describe the order; the reference and anything else are dropped.
_BODY_FIELDS = ("trading_symbol", "exchange", "segment", "transaction_type", "quantity", "product", "order_type", "price",
                "trigger_price", "validity")


def account_ref(account_id: Any) -> str:
    """A stable, non-reversible 8-character reference for an account in logs - never its id, name or broker user id."""
    return hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()[:8]


def _mask(value: Any) -> str:
    text = str(value or "")
    return f"...{text[-4:]}" if len(text) > 4 else text


def preview_record(event: MasterTradeEvent, master_broker: Optional[str], account: Any, membership: str,
                   extra: Tuple[PreviewBlocker, ...], multiplier: Any) -> Dict[str, Any]:
    """One sanitized, JSON-ready preview for one candidate Child."""
    p = preview_child_order(event, master_broker=master_broker, child_broker=getattr(account, "broker", None), multiplier=multiplier)
    blockers = tuple(extra) + p.blockers
    return {
        "master": {"broker": p.master_broker, "order": _mask(event.master_order_id), "event": event.event_type,
                   "symbol": event.symbol, "exchange": event.exchange, "side": event.side, "quantity": event.quantity,
                   "order_type": event.order_type, "product_type": event.product_type,
                   "price": str(event.price), "limit_price": str(event.limit_price), "trigger_price": str(event.trigger_price)},
        "child": {"ref": account_ref(getattr(account, "id", "")), "broker": p.child_broker, "membership": membership},
        "would_send": p.would_send,
        "order_ok_if_enabled": all(b.code in GATE_CODES for b in p.blockers),
        "blockers": [{"code": b.code, "detail": b.detail} for b in blockers],
        "child_order": None if p.order_body is None else {k: p.order_body[k] for k in _BODY_FIELDS if k in p.order_body},
        "child_order_type": p.child_order_type,
        "child_quantity": p.child_quantity,
        "notes": list(p.notes),
    }


async def observe_child_previews(event: MasterTradeEvent, master_account_id: Any) -> List[Dict[str, Any]]:
    """Previews for every active account other than the Master: its strategy's Children AND every other account, so a broker that is
    not (or cannot yet be) a member - Groww today - shows its membership blocker next to the rest. Database READS only: no write, no
    broker client, no session, and the Master's credentials are never loaded. An empty list for a non-entry event."""
    import uuid

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.core.database import AsyncSessionLocal
    from app.models.accounts import Account
    from app.models.strategies import GroupStrategy, StrategyMember

    if str(event.event_type or "").upper() not in ENTRY_EVENTS:
        return []
    master_id = uuid.UUID(str(master_account_id))
    async with AsyncSessionLocal() as session:
        master = await session.get(Account, master_id)
        strategies = list((await session.execute(
            select(GroupStrategy).where(GroupStrategy.master_account_id == master_id, GroupStrategy.deleted_at.is_(None))
            .options(selectinload(GroupStrategy.members).selectinload(StrategyMember.account))
        )).scalars().all())
        candidates = list((await session.execute(
            select(Account).where(Account.id != master_id, Account.deleted_at.is_(None), Account.is_active.is_(True))
        )).scalars().all())
    master_broker = normalize_broker_id(getattr(master, "broker", None)) if master is not None else None
    strategy = strategies[0] if len(strategies) == 1 else None
    members = {m.account_id: m for m in (strategy.members if strategy is not None else [])}

    records = []
    for account in sorted(candidates, key=lambda a: (str(a.broker or ""), account_ref(a.id))):
        extra: List[PreviewBlocker] = []
        member = members.get(account.id)
        if strategy is None:
            membership = NO_STRATEGY
            extra.append(PreviewBlocker(NO_STRATEGY, f"{len(strategies)} strategies list this Master"))
        elif member is None:
            membership = NOT_A_STRATEGY_MEMBER
            extra.append(PreviewBlocker(NOT_A_STRATEGY_MEMBER, "not a Child of the Master's strategy"))
        else:
            membership = "MEMBER" if member.trading_enabled else MEMBER_TRADING_DISABLED
            if not member.trading_enabled:
                extra.append(PreviewBlocker(MEMBER_TRADING_DISABLED, "the Child's trading is disabled in the strategy"))
        if strategy is not None and not strategy.trading_enabled:
            extra.append(PreviewBlocker(STRATEGY_TRADING_DISABLED, "the strategy's trading is disabled"))
        multiplier = member.multiplier if member is not None else Decimal("1")
        records.append(preview_record(event, master_broker, account, membership, tuple(extra), multiplier))
    return records
