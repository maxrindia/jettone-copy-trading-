import uuid
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.core.database import get_db
from app.core.redis import get_redis
from app.models.strategies import STRATEGY_ROLE_CHILD, GroupStrategy, StrategyMember
from app.models.accounts import Account, CopySetting, QuantityMode
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide as DBOrderSide, OrderSource, OrderStatus
from app.services import strategy_membership as membership
from app.services import strategy_readiness
from app.services import strategy_switch
from app.services.runtime import hooks as runtime_hooks
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.exceptions import AuthenticationError
from app.services.alice_blue.types import OrderResultCategory, OrderSide
from app.services.brokers.guards import ensure_alice_blue_account
from app.schemas.strategies import (
    BrokerPairInfo,
    MasterSwitchRequest,
    MemberAdd,
    MemberResponse,
    MemberUpdate,
    QuantityPolicyResponse,
    QuantityPolicyUpdate,
    StrategyCreate,
    StrategyResponse,
    StrategyUpdate,
)
from app.services.copy_engine.pipeline import CopyEnginePipeline, is_ambiguous_send
from app.services.copy_engine.validator import EventValidator
from app.services.monitoring.notifier import emit_system_event
from app.services.reliability.stop_provider import build_stop_provider
from app.services.reliability.unresolved_sends import (
    UNRESOLVED_BROKER_STATUS,
    ResolutionError,
    blocked_detail,
    emit_unresolved_send_event,
    escalate_if_overdue,
    find_unresolved_send,
    list_unresolved_sends,
    outcome_unknown_message,
    resolve_unresolved_send,
    send_claim_lock,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/strategies", tags=["Group Strategies"])


def _load_opts():
    """Reusable selectinload options for strategies."""
    return [
        selectinload(GroupStrategy.members).selectinload(StrategyMember.account),
        selectinload(GroupStrategy.master_account),
    ]


def _to_response(strategy: GroupStrategy) -> StrategyResponse:
    """Build StrategyResponse including computed member_count and (R6) the read-only broker-pair readiness."""
    data = StrategyResponse.model_validate(strategy)
    data.member_count = len(strategy.members)
    try:
        readiness = strategy_readiness.strategy_readiness(strategy)
        data.readiness = readiness
        for member, row in zip(data.members, readiness["children"]):
            member.broker_pair = BrokerPairInfo(**row["broker_pair"])
    except Exception as exc:                   # diagnostics never break the strategy response
        logger.error(f"[Strategies] readiness diagnostics unavailable: {type(exc).__name__}")
    return data


async def _member_responses(db: AsyncSession, strategy_id: uuid.UUID, members: List[StrategyMember]) -> List[MemberResponse]:
    """Member responses with the R6 broker-pair diagnostics. Reads the strategy's Master broker only; writes nothing."""
    master = (await db.execute(
        select(GroupStrategy).where(GroupStrategy.id == strategy_id, GroupStrategy.deleted_at.is_(None))
        .options(selectinload(GroupStrategy.master_account))
    )).scalar_one_or_none()
    out = [MemberResponse.model_validate(m) for m in members]
    try:
        rows = strategy_readiness.children_readiness(
            strategy_readiness.master_broker_for(master.master_account if master else None), members,
            has_master=bool(master and master.master_account is not None),
        )
        for response, row in zip(out, rows):
            response.broker_pair = BrokerPairInfo(**row["broker_pair"])
    except Exception as exc:
        logger.error(f"[Strategies] member pair diagnostics unavailable: {type(exc).__name__}")
    return out


def _conflict(exc: "membership.MembershipConflict") -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message)


def get_switch_client_provider():
    """Broker clients for Master-switch verification: the engine's own
    registered clients (same session and Static IP proxy the engine uses).
    An account without one cannot be verified and blocks the switch."""
    from app.services.alice_blue.manager import account_manager

    return account_manager.get_client



# ─── Strategy CRUD ────────────────────────────────────────────────────────────

@router.get("", response_model=List[StrategyResponse])
async def list_strategies(db: AsyncSession = Depends(get_db)):
    """List all active group strategies."""
    result = await db.execute(
        select(GroupStrategy)
        .where(GroupStrategy.deleted_at.is_(None))
        .options(*_load_opts())
        .order_by(GroupStrategy.created_at.desc())
    )
    strategies = result.scalars().all()
    return [_to_response(s) for s in strategies]


@router.post("", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
async def create_strategy(payload: StrategyCreate, db: AsyncSession = Depends(get_db)):
    """Create a new group strategy (without a Master).

    The Master is assigned afterwards with POST /strategies/{id}/switch-master,
    so a first Master gets the same preflight (working orders, open
    positions, broker verification) and the same activation snapshot as any
    later change - never a plain field write."""
    if payload.master_account_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create the strategy first, then assign its Master with POST /strategies/{id}/switch-master.",
        )

    strategy = GroupStrategy(
        name=payload.name,
        description=payload.description,
        visibility=payload.visibility,
        allocated_amount=payload.allocated_amount,
        max_subscribers=payload.max_subscribers,
    )
    db.add(strategy)
    await db.commit()

    result = await db.execute(
        select(GroupStrategy)
        .where(GroupStrategy.id == strategy.id)
        .options(*_load_opts())
    )
    return _to_response(result.scalar_one())


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(strategy_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a single strategy by ID."""
    result = await db.execute(
        select(GroupStrategy)
        .where(GroupStrategy.id == strategy_id, GroupStrategy.deleted_at.is_(None))
        .options(*_load_opts())
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return _to_response(strategy)


@router.put("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: uuid.UUID,
    payload: StrategyUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update strategy fields (name, master account, toggles, etc.)."""
    result = await db.execute(
        select(GroupStrategy)
        .where(GroupStrategy.id == strategy_id, GroupStrategy.deleted_at.is_(None))
        .options(*_load_opts())
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    changes = payload.model_dump(exclude_unset=True)
    if "master_account_id" in changes:
        if changes["master_account_id"] == strategy.master_account_id:
            changes.pop("master_account_id")
        else:
            # A Master change must go through the verified switch flow
            # (preflight blockers, switch locks, monitor sync) - never a
            # plain field write.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Change a strategy's Master with POST /strategies/{id}/switch-master.",
            )

    for field, value in changes.items():
        setattr(strategy, field, value)

    await db.commit()

    result = await db.execute(
        select(GroupStrategy)
        .where(GroupStrategy.id == strategy_id)
        .options(*_load_opts())
    )
    return _to_response(result.scalar_one())


@router.get("/{strategy_id}/readiness")
async def get_strategy_readiness(
    strategy_id: uuid.UUID,
    hours: int = 24,
    db: AsyncSession = Depends(get_db),
):
    """R6, read-only: which Master -> Child broker pairs the order-time gate accepts, and how much of the Master's recent
    instrument activity has any mapping evidence. Makes no broker call, writes nothing, changes no role. A candidate ISIN is
    never reported as a verified identity (`verified_identities` is always 0 until authorised evidence exists)."""
    strategy = (await db.execute(
        select(GroupStrategy).where(GroupStrategy.id == strategy_id, GroupStrategy.deleted_at.is_(None)).options(*_load_opts())
    )).scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    pairs = strategy_readiness.strategy_readiness(strategy)
    coverage = await strategy_readiness.coverage_for_master(db, strategy.master_account_id, hours=hours)
    return {
        **pairs,
        "mapping_coverage": coverage,
        "warnings": pairs["warnings"] + strategy_readiness.coverage_warnings(coverage),
        "scope": [
            "Only Alice Blue -> Alice Blue is supported end to end. Any other pair is skipped at order time as BROKER_PAIR_NOT_SUPPORTED.",
            "Instrument mapping is diagnostic only: a candidate ISIN is MASTER_TOKEN_LINK_UNVERIFIED and is not a verified identity.",
        ],
    }


# --- Master switch ------------------------------------------------------------

@router.get("/{strategy_id}/switch-master/preflight")
async def preflight_master_switch(
    strategy_id: uuid.UUID,
    new_master_account_id: uuid.UUID,
    old_master_action: str = "REMOVE",
    db: AsyncSession = Depends(get_db),
    client_provider=Depends(get_switch_client_provider),
):
    """Read-only: whether the Master can be switched right now, and every
    specific reason it cannot. Makes broker READ calls (order book,
    positions) only; never places, modifies or cancels anything."""
    try:
        result = await strategy_switch.run_preflight(
            db, strategy_id, new_master_account_id, old_master_action.upper(), client_provider
        )
    except strategy_switch.SwitchValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return result.to_dict()


@router.post("/{strategy_id}/switch-master")
async def switch_master(
    strategy_id: uuid.UUID,
    payload: MasterSwitchRequest,
    db: AsyncSession = Depends(get_db),
    client_provider=Depends(get_switch_client_provider),
):
    """Switches the strategy's Master after re-running every preflight check
    under the switch locks. Blocked switches return 409 and change nothing;
    no order is ever cancelled and no position squared off."""
    try:
        result = await strategy_switch.execute_switch(
            db,
            strategy_id,
            payload.new_master_account_id,
            payload.old_master_action,
            client_provider,
            after_commit=lambda preflight: runtime_hooks.sync_master_monitors(
                activations=preflight.activations()
            ),
            acknowledge_unverified=payload.acknowledge_unverified,
            acknowledge_cnc_positions=payload.acknowledge_cnc_positions,
        )
    except strategy_switch.SwitchValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except strategy_switch.SwitchBlocked as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    refreshed = (
        await db.execute(select(GroupStrategy).where(GroupStrategy.id == strategy_id).options(*_load_opts()))
    ).scalar_one()
    return {
        "switched": True,
        "strategy": _to_response(refreshed).model_dump(mode="json"),
        "new_master_account_id": str(payload.new_master_account_id),
        "old_master_action": payload.old_master_action,
        # False until the new Master's first (baseline) order-book poll
        # completes. Orders it places before then are recorded but never
        # copied - that poll is the activation boundary.
        "monitor_ready": runtime_hooks.is_master_monitor_ready(str(payload.new_master_account_id)),
        "preflight": result.to_dict(),
    }


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_strategy(strategy_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Soft-delete a strategy."""
    from datetime import datetime, timezone
    result = await db.execute(
        select(GroupStrategy)
        .where(GroupStrategy.id == strategy_id, GroupStrategy.deleted_at.is_(None))
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    strategy.deleted_at = datetime.now(timezone.utc)
    # Releases every account of this strategy so it can join another one.
    await membership.release_strategy(db, strategy.id)
    await db.commit()
    await runtime_hooks.sync_master_monitors()


# ─── Strategy Members (Child Accounts) ───────────────────────────────────────

@router.get("/{strategy_id}/members", response_model=List[MemberResponse])
async def list_members(strategy_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """List all child accounts of a strategy."""
    result = await db.execute(
        select(StrategyMember)
        .where(StrategyMember.strategy_id == strategy_id)
        .options(selectinload(StrategyMember.account))
        .order_by(StrategyMember.created_at.asc())
    )
    return await _member_responses(db, strategy_id, list(result.scalars().all()))


@router.post("/{strategy_id}/members", response_model=MemberResponse, status_code=status.HTTP_201_CREATED)
async def add_member(
    strategy_id: uuid.UUID,
    payload: MemberAdd,
    db: AsyncSession = Depends(get_db),
):
    """Add a child account to a strategy."""
    # Verify strategy exists
    strat_result = await db.execute(
        select(GroupStrategy).where(GroupStrategy.id == strategy_id, GroupStrategy.deleted_at.is_(None))
        .options(selectinload(GroupStrategy.master_account))
    )
    strategy = strat_result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    # Verify account exists
    acc_result = await db.execute(
        select(Account).where(Account.id == payload.account_id, Account.deleted_at.is_(None))
    )
    account = acc_result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # The broker must be allowed to receive copied orders (registry as_child). The backend decides; the
    # UI's disabled "Add" button only mirrors this.
    try:
        membership.assert_broker_can_be_child(account.broker)
        # R5 pair gate: a Child whose broker pair with this strategy's Master is unsupported is refused (409).
        membership.assert_pair_supported(strategy.master_account, account.broker)
    except membership.MembershipConflict as exc:
        raise _conflict(exc)

    # One strategy per account, in either role (also covers adding the
    # strategy's own Master as its Child).
    try:
        await membership.assert_account_available(db, payload.account_id)
    except membership.MembershipConflict as exc:
        raise _conflict(exc)

    member = StrategyMember(
        strategy_id=strategy_id,
        account_id=payload.account_id,
        multiplier=payload.multiplier,
        trading_enabled=payload.trading_enabled,
    )
    db.add(member)
    await membership.assign(db, strategy_id, payload.account_id, STRATEGY_ROLE_CHILD)
    # Every Child gets a CopySetting so the RiskEngine gate runs for it, and
    # its copy_enabled follows the Trading toggle chosen here (one switch).
    await membership.sync_copy_enabled(db, payload.account_id, payload.trading_enabled)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="This account is already assigned to a strategy.")

    result = await db.execute(
        select(StrategyMember)
        .where(StrategyMember.id == member.id)
        .options(selectinload(StrategyMember.account))
    )
    return (await _member_responses(db, strategy_id, [result.scalar_one()]))[0]


@router.put("/{strategy_id}/members/{account_id}", response_model=MemberResponse)
async def update_member(
    strategy_id: uuid.UUID,
    account_id: uuid.UUID,
    payload: MemberUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a child account's multiplier or trading_enabled flag."""
    result = await db.execute(
        select(StrategyMember)
        .where(
            StrategyMember.strategy_id == strategy_id,
            StrategyMember.account_id == account_id,
        )
        .options(selectinload(StrategyMember.account))
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found in this strategy")

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("trading_enabled") is True and not member.trading_enabled:
        # Turning copying ON for a Child is an activation: refuse it for an unsupported pair. Turning it off, and changing the
        # multiplier of an existing (even unsupported) member, stay allowed - existing memberships are preserved.
        strategy = (await db.execute(
            select(GroupStrategy).where(GroupStrategy.id == strategy_id).options(selectinload(GroupStrategy.master_account))
        )).scalar_one_or_none()
        try:
            membership.assert_pair_supported(strategy.master_account if strategy else None, member.account.broker)
        except membership.MembershipConflict as exc:
            raise _conflict(exc)
    for field, value in changes.items():
        setattr(member, field, value)
    if "trading_enabled" in changes and changes["trading_enabled"] is not None:
        # The Trading toggle is the single on/off for copying to this Child.
        await membership.sync_copy_enabled(db, account_id, changes["trading_enabled"])

    await db.commit()
    await db.refresh(member)
    return (await _member_responses(db, strategy_id, [member]))[0]


_WORKING_ORDER_STATUSES = (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIAL)


@router.put("/{strategy_id}/members/{account_id}/quantity-policy", response_model=QuantityPolicyResponse)
async def update_member_quantity_policy(
    strategy_id: uuid.UUID,
    account_id: uuid.UUID,
    payload: QuantityPolicyUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Explicitly choose how this Child's copy quantity is derived:
    MULTIPLIER (Master qty x multiplier) or FIXED (same qty every order).

    Changes ONLY the quantity policy. It never enables or disables copying
    (copy_enabled / the Trading toggle are untouched), never creates a
    CopySetting, and affects only orders copied after the change. Refused
    while the Child has a working copied order, because a later Master
    modification would re-size that order under the new policy."""
    member = (
        await db.execute(
            select(StrategyMember).where(
                StrategyMember.strategy_id == strategy_id,
                StrategyMember.account_id == account_id,
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found in this strategy")

    setting = (
        await db.execute(select(CopySetting).where(CopySetting.follower_account_id == account_id))
    ).scalar_one_or_none()
    if setting is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Child has no copy settings yet. Turn its Trading toggle on or off once, then retry.",
        )

    working = (
        await db.execute(
            select(Order.id).where(
                Order.account_id == account_id,
                Order.source == OrderSource.SYSTEM,
                Order.status.in_(_WORKING_ORDER_STATUSES),
            ).limit(1)
        )
    ).first()
    if working is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Child has a working copied order. Change the quantity policy after it completes or is cancelled.",
        )

    new_mode = QuantityMode(payload.quantity_mode)
    new_fixed = payload.fixed_qty if payload.fixed_qty is not None else setting.fixed_qty
    if new_mode == QuantityMode.FIXED and (new_fixed is None or new_fixed < 1):
        raise HTTPException(status_code=422, detail="fixed_qty (1 or more) is required for the FIXED policy")

    before = f"{getattr(setting.quantity_mode, 'value', setting.quantity_mode)} (fixed_qty={setting.fixed_qty}, multiplier={member.multiplier})"
    setting.quantity_mode = new_mode
    if payload.fixed_qty is not None:
        setting.fixed_qty = payload.fixed_qty
    if payload.multiplier is not None:
        member.multiplier = payload.multiplier
    after = f"{new_mode.value} (fixed_qty={setting.fixed_qty}, multiplier={member.multiplier})"

    await emit_system_event(
        db,
        event_type="CHILD_QUANTITY_POLICY_CHANGED",
        severity="INFO",
        message=f"Quantity policy changed: {before} -> {after}.",
        account_id=account_id,
        commit=False,
    )
    await db.commit()
    return QuantityPolicyResponse(
        account_id=account_id,
        quantity_mode=new_mode.value,
        multiplier=member.multiplier,
        fixed_qty=setting.fixed_qty,
    )


@router.delete("/{strategy_id}/members/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    strategy_id: uuid.UUID,
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Remove a child account from a strategy."""
    result = await db.execute(
        select(StrategyMember).where(
            StrategyMember.strategy_id == strategy_id,
            StrategyMember.account_id == account_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found in this strategy")

    # Removing a Child with a copied order still working would leave that
    # order's later modifications resolved without this Child's strategy
    # settings. Block until it finishes; never cancel anything.
    open_children = (
        await db.execute(
            select(Order.id).where(
                Order.account_id == account_id,
                Order.source == OrderSource.SYSTEM,
                Order.status.in_([OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.PENDING]),
            )
        )
    ).all()
    in_flight = (
        await db.execute(
            select(CopyTrade.id).where(
                CopyTrade.follower_account_id == account_id,
                CopyTrade.status.in_([CopyTradeStatus.PENDING, CopyTradeStatus.SUBMITTED]),
            )
        )
    ).all()
    if open_children or in_flight:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This Child still has {len(open_children)} copied order(s) working and "
                f"{len(in_flight)} copy placement(s) in progress. Wait for them to finish "
                "before removing it."
            ),
        )

    await db.delete(member)
    await membership.release(db, account_id)
    await db.commit()


# ─── Member Positions & Square-Off ──────────────────────────────────────────

class SquareOffRequest(BaseModel):
    symbol: str = Field(min_length=1)
    exchange: str = "NSE"
    quantity: int = Field(gt=0)
    side: str = Field(default="BUY", description="Current position side ('BUY' or 'SELL')")
    product_type: str = "INTRADAY"


async def _load_member_client(
    strategy_id: uuid.UUID,
    account_id: uuid.UUID,
    db: AsyncSession,
    redis,
) -> tuple[GroupStrategy, StrategyMember, Account, AliceBlueClient]:
    # 1. Verify strategy exists
    strat_res = await db.execute(
        select(GroupStrategy).where(GroupStrategy.id == strategy_id, GroupStrategy.deleted_at.is_(None))
    )
    strategy = strat_res.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    # 2. Verify account is a valid member of this strategy
    mem_res = await db.execute(
        select(StrategyMember)
        .where(
            StrategyMember.strategy_id == strategy_id,
            StrategyMember.account_id == account_id,
        )
        .options(selectinload(StrategyMember.account).selectinload(Account.api_credential))
    )
    member = mem_res.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Child account is not a member of this strategy")

    account = member.account
    if not account or account.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Child account not found")

    # The broker is checked FIRST: positions and square-off here are Alice Blue operations, and a Groww / Angel One
    # account keeps its credentials in the vault (no api_credential), which used to be reported as "no API
    # credentials" - wrong advice. Alice Blue accounts take exactly the same path as before.
    ensure_alice_blue_account(account)

    if not account.api_credential:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Account '{account.display_name}' ({account.alice_user_id}) has no API credentials.",
        )

    client = AliceBlueClient(
        account_id=str(account.id),
        user_id=account.alice_user_id,
        api_secret=decrypt_secret(account.api_credential.encrypted_api_secret),
        redis=redis,
    )
    return strategy, member, account, client


@router.get("/{strategy_id}/members/{account_id}/positions")
async def get_child_member_positions(
    strategy_id: uuid.UUID,
    account_id: uuid.UUID,
    include_closed: bool = False,
    db: AsyncSession = Depends(get_db),
    redis = Depends(get_redis),
):
    """
    Fetches live position book for a specific child account in a group strategy.
    """
    strategy, member, account, client = await _load_member_client(strategy_id, account_id, db, redis)

    positions = await client.get_positions_detail(include_closed=include_closed)
    
    # Fetch real-time orders book from broker
    orders = await client.get_order_book()
    orders_list = [
        {
            "order_id": o.broker_order_id,
            "symbol": o.symbol,
            "exchange": o.exchange,
            "side": o.side.value if hasattr(o.side, "value") else str(o.side),
            "quantity": o.quantity,
            "price": float(o.price) if o.price else 0.0,
            "average_price": float(o.average_price) if o.average_price else 0.0,
            "status": o.status,
            "product_type": o.product_type.value if hasattr(o.product_type, "value") else str(o.product_type),
            "order_type": o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type),
            "timestamp": o.timestamp.isoformat() if o.timestamp else None,
            "rejection_reason": o.rejection_reason,
        }
        for o in orders
    ]

    return {
        "strategy_id": str(strategy.id),
        "strategy_name": strategy.name,
        "account": {
            "id": str(account.id),
            "display_name": account.display_name,
            "alice_user_id": account.alice_user_id,
            "account_type": account.account_type.value if hasattr(account.account_type, "value") else str(account.account_type),
            "is_active": account.is_active,
            "session_valid": account.session_valid,
            "is_emergency_stopped": account.is_emergency_stopped,
            "multiplier": float(member.multiplier),
            "trading_enabled": member.trading_enabled,
        },
        "positions": positions,
        "positions_count": len(positions),
        "orders": orders_list,
        "orders_count": len(orders_list),
    }


@router.post("/{strategy_id}/members/{account_id}/square-off")
async def square_off_child_position(
    strategy_id: uuid.UUID,
    account_id: uuid.UUID,
    payload: SquareOffRequest,
    db: AsyncSession = Depends(get_db),
    redis = Depends(get_redis),
):
    """
    Squares off an open position for a specific child account.
    """
    strategy, member, account, client = await _load_member_client(strategy_id, account_id, db, redis)

    # Trading Safety Gates
    if not account.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Account '{account.display_name}' is disabled for trading.",
        )
    if account.is_emergency_stopped:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Account '{account.display_name}' is currently in Emergency Stop mode.",
        )
    if not member.trading_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Trading is disabled for this child account in strategy '{strategy.name}'.",
        )

    pipeline = CopyEnginePipeline(
        validator=EventValidator(redis=redis),
        stop_provider=build_stop_provider(redis, account_ids=None),
    )

    # Resolve the actual broker-facing side/quantity/client_order_id up
    # front (pure computation, no network call) so the pre-write below and
    # the actual broker submission share the exact same identity, and so
    # this endpoint never re-derives the BUY/SELL flip independently of
    # AliceBlueClient's own logic.
    _req, target_side, target_qty = client._resolve_square_off_order(
        symbol=payload.symbol,
        exchange=payload.exchange,
        quantity=payload.quantity,
        side=payload.side,
        product=payload.product_type,
    )
    client_order_id = _req.client_order_id
    db_side = DBOrderSide.BUY if target_side == OrderSide.BUY else DBOrderSide.SELL

    # Pre-write PENDING before contacting the broker (mirrors broker.py's
    # manual master-order pattern) - source=SYSTEM so ReconciliationEngine.
    # compute_expected_positions (which only reads SYSTEM-sourced Order rows
    # for a follower) picks this up; master_broker_order_id stays NULL,
    # there is no master order behind a manual square-off. No CopyTrade
    # write - this isn't a copy-replication event.
    #
    # The guard and the pre-write are ONE step per account + symbol (send_claim_lock, released before the broker
    # call): an earlier order whose broker outcome is still unknown - or one still in flight - blocks a new
    # square-off, because sending again could close the position twice and reverse it, and two simultaneous
    # requests cannot both pass the check.
    async with send_claim_lock(account.id, payload.symbol, payload.exchange):
        unresolved = await find_unresolved_send(db, account.id, payload.symbol, payload.exchange)
        if unresolved is not None:
            logger.warning(
                f"[SquareOff] Blocked: unresolved send {unresolved.internal_order_id} "
                f"for account {account.id}, symbol {payload.symbol.upper()}"
            )
            await escalate_if_overdue(db, unresolved)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=blocked_detail(unresolved))
        if target_qty > 0:
            await pipeline.save_or_update_order(
                internal_order_id=client_order_id,
                broker_order_id=None,
                account_id=account.id,
                source=OrderSource.SYSTEM,
                exchange=payload.exchange.upper(),
                symbol=payload.symbol.upper(),
                side=db_side,
                order_type="MARKET",
                product_type=payload.product_type,
                quantity=target_qty,
                filled_quantity=0,
                pending_quantity=target_qty,
                price=Decimal("0"),
                status=OrderStatus.PENDING,
                master_broker_order_id=None,
            )

    try:
        result, target_side, target_qty = await client.square_off_position(
            symbol=payload.symbol,
            exchange=payload.exchange,
            quantity=payload.quantity,
            side=payload.side,
            product=payload.product_type,
            client_order_id=client_order_id,
        )
    except Exception as exc:
        # Only the EXACT AuthenticationError is known to be pre-send: it comes from get_session()/_get_headers(),
        # before the request is issued. A SessionExpiredError (a subclass) is raised AFTER the request went out - the
        # broker's answer may be a gateway error that merely mentions the session - and any other exception (or a
        # cancellation, which is not an Exception at all) cannot be proven pre-send either. Those leave the
        # pre-written PENDING row unresolved, so the retry guard keeps blocking until the operator verifies it.
        if type(exc) is not AuthenticationError:
            logger.critical(
                f"[SquareOff] Order {client_order_id}: place_order raised {type(exc).__name__}; the request may have "
                "reached the broker. Left unresolved - verify at the broker, then clear it with the resolve endpoint."
            )
            raise
        logger.error(f"[SquareOff] Order {client_order_id} was not sent: no active broker session")
        if target_qty > 0:
            try:
                await pipeline.save_or_update_order(
                    internal_order_id=client_order_id,
                    broker_order_id=None,
                    account_id=account.id,
                    source=OrderSource.SYSTEM,
                    exchange=payload.exchange.upper(),
                    symbol=payload.symbol.upper(),
                    side=db_side,
                    order_type="MARKET",
                    product_type=payload.product_type,
                    quantity=target_qty,
                    filled_quantity=0,
                    pending_quantity=0,
                    price=Decimal("0"),
                    status=OrderStatus.FAILED,
                    broker_status="FAILED",
                    rejection_reason="Not sent: no active broker session",
                    master_broker_order_id=None,
                )
            except Exception as persist_exc:
                logger.critical(
                    f"[SquareOff] Could not record order {client_order_id} as not sent: {persist_exc}", exc_info=True
                )
        raise

    # An AMBIGUOUS outcome (TIMEOUT / UNKNOWN) is not a rejection: the order may exist at the broker. Keep it
    # unresolved (never REJECTED), alert, and tell the caller not to retry.
    if is_ambiguous_send(result):
        symbol = payload.symbol.upper()
        logger.critical(
            f"[SquareOff] Order {client_order_id} for {symbol} on account {account.id}: broker outcome UNKNOWN "
            f"(category={result.category.value}). Not rejected, not retried."
        )
        if target_qty > 0:
            try:
                await pipeline.save_or_update_order(
                    internal_order_id=client_order_id,
                    broker_order_id=None,
                    account_id=account.id,
                    source=OrderSource.SYSTEM,
                    exchange=payload.exchange.upper(),
                    symbol=symbol,
                    side=db_side,
                    order_type="MARKET",
                    product_type=payload.product_type,
                    quantity=target_qty,
                    filled_quantity=0,
                    pending_quantity=target_qty,
                    price=Decimal("0"),
                    status=OrderStatus.PENDING,
                    broker_status=UNRESOLVED_BROKER_STATUS,
                    master_broker_order_id=None,
                )
            except Exception as exc:
                logger.critical(
                    f"[SquareOff] Could not mark square-off order {client_order_id} unresolved "
                    f"for account {account.id}: {exc}",
                    exc_info=True,
                )
        await emit_unresolved_send_event(
            db,
            event_type="SQUARE_OFF_UNRESOLVED_SEND",
            account_id=account.id,
            message=(
                f"Square-off order {client_order_id} ({db_side.value} {target_qty} {symbol}) on account "
                f"{account.id} has an UNKNOWN broker outcome ({result.category.value}). It may have been placed. "
                "Verify it at the broker before squaring off again; it is not rejected."
            ),
            metadata={
                "account_id": str(account.id),
                "strategy_id": str(strategy_id),
                "client_order_id": client_order_id,
                "symbol": symbol,
                "exchange": payload.exchange.upper(),
                "side": db_side.value,
                "quantity": target_qty,
                "category": result.category.value,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=outcome_unknown_message("Square-off", symbol, client_order_id),
        )

    if result.category != OrderResultCategory.SUCCESS:
        if target_qty > 0:
            try:
                await pipeline.save_or_update_order(
                    internal_order_id=client_order_id,
                    broker_order_id=result.broker_order_id,
                    account_id=account.id,
                    source=OrderSource.SYSTEM,
                    exchange=payload.exchange.upper(),
                    symbol=payload.symbol.upper(),
                    side=db_side,
                    order_type="MARKET",
                    product_type=payload.product_type,
                    quantity=target_qty,
                    filled_quantity=0,
                    pending_quantity=0,
                    price=Decimal("0"),
                    status=OrderStatus.REJECTED,
                    broker_status="REJECTED",
                    rejection_reason=result.message,
                    master_broker_order_id=None,
                )
            except Exception as exc:
                logger.error(
                    f"[SquareOff] Failed to persist REJECTED square-off order {client_order_id} "
                    f"for account {account.id}: {exc}",
                    exc_info=True,
                )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Square off rejected: {result.message}",
        )

    # Broker SUCCESS from here on - a real order has been placed. Any
    # failure below must never be reported back to the caller as a failed
    # square-off; the broker action already happened.
    try:
        await pipeline.save_or_update_order(
            internal_order_id=client_order_id,
            broker_order_id=result.broker_order_id,
            account_id=account.id,
            source=OrderSource.SYSTEM,
            exchange=payload.exchange.upper(),
            symbol=payload.symbol.upper(),
            side=db_side,
            order_type="MARKET",
            product_type=payload.product_type,
            quantity=target_qty,
            filled_quantity=target_qty,
            pending_quantity=0,
            price=Decimal("0"),
            average_price=Decimal("0"),
            status=OrderStatus.COMPLETE,
            broker_status="COMPLETE",
            master_broker_order_id=None,
        )
    except Exception as exc:
        logger.critical(
            f"[SquareOff] Broker square-off SUCCEEDED (broker_order_id={result.broker_order_id}, "
            f"client_order_id={client_order_id}) but DB persistence failed for account {account.id}: {exc}",
            exc_info=True,
        )
        try:
            await emit_system_event(
                db,
                event_type="SQUARE_OFF_PERSISTENCE_FAILED",
                severity="CRITICAL",
                message=(
                    f"Square-off broker order {result.broker_order_id} for account {account.id} "
                    "succeeded but could not be persisted to the Order table. Manual verification required."
                ),
                account_id=account.id,
                metadata_payload={
                    "account_id": str(account.id),
                    "broker_order_id": result.broker_order_id,
                    "client_order_id": client_order_id,
                    "symbol": payload.symbol.upper(),
                    "quantity": target_qty,
                    "side": db_side.value,
                },
            )
        except Exception as event_exc:
            logger.critical(
                f"[SquareOff] Failed to emit SQUARE_OFF_PERSISTENCE_FAILED SystemEvent: {event_exc}",
                exc_info=True,
            )

    return {
        "status": "success",
        "message": f"Square off order submitted for {payload.symbol}",
        "broker_order_id": result.broker_order_id,
        "client_order_id": result.client_order_id,
    }

