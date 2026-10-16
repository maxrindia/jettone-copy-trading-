import asyncio
import uuid
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.core.database import get_db
from app.core.redis import get_redis
from app.models.accounts import Account
from app.models.strategies import GroupStrategy
from app.models.trades import OrderSide as DBOrderSide, OrderSource, OrderStatus
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.instruments import KNOWN_INSTRUMENTS, load_and_cache_contract_master, resolve_instrument_info
from app.services.alice_blue.exceptions import IPNotWhitelistedError, AuthenticationError
from app.services.alice_blue.types import (
    OrderRequest,
    OrderResultCategory,
    OrderSide,
    OrderType,
    ProductType,
)
from app.services.brokers.guards import ensure_alice_blue_account
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.live_trading import get_live_trading_state
from app.services.monitoring.notifier import emit_system_event
from app.services.reliability.emergency_stop import (
    is_master_trading_enabled,
    set_master_trading,
)
from app.services.reliability.stop_provider import build_stop_provider
from app.services import strategy_membership as membership
from app.services.runtime import hooks as runtime_hooks
from sqlalchemy.orm import selectinload

router = APIRouter(prefix="/broker", tags=["Alice Blue Broker Operations"])

logger = logging.getLogger("broker_api")


async def _load_client(
    account_id: uuid.UUID, db: AsyncSession, redis
) -> tuple[Account, AliceBlueClient]:
    query = (
        select(Account)
        .where(Account.id == account_id, Account.deleted_at.is_(None))
        .options(selectinload(Account.api_credential))
    )
    account = (await db.execute(query)).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    credential = account.api_credential
    if not credential:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account has no stored API credential.",
        )
    ensure_alice_blue_account(account)
    client = AliceBlueClient(
        account_id=str(account.id),
        user_id=account.alice_user_id,
        api_secret=decrypt_secret(credential.encrypted_api_secret),
        redis=redis,
    )
    return account, client


async def _load_master_client(
    db: AsyncSession, redis, strategy_id: uuid.UUID
) -> tuple[Account, AliceBlueClient]:
    """The selected strategy's own Master and the engine's broker client for
    it. Never "the oldest account_type=MASTER account": every strategy has
    its own explicit Master.

    Uses the engine's registered client (the same session and Static IP
    proxy the engine uses for that account) rather than building a fresh,
    proxy-less one. Fails closed when the strategy has no Master, trading is
    off for it, the account is unusable, or the engine is not yet monitoring
    that Master - a MARKET order's copy depends on that monitor seeing its
    fill, so placing one before the monitor is active would leave the
    Children uncopied."""
    from app.services.alice_blue.manager import account_manager

    strategy = (
        await db.execute(
            select(GroupStrategy).where(GroupStrategy.id == strategy_id, GroupStrategy.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    if strategy.master_account_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This strategy has no Master account.")
    if not strategy.trading_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Trading is disabled for this strategy, so the order would not be copied. Enable it first.",
        )

    master = (
        await db.execute(
            select(Account)
            .where(Account.id == strategy.master_account_id, Account.deleted_at.is_(None))
            .options(selectinload(Account.api_credential))
        )
    ).scalar_one_or_none()
    if not master or not master.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This strategy's Master account is not active.")
    if not master.api_credential:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Master account has no stored API credential.",
        )

    client = account_manager.get_client(str(master.id))
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The engine has no active broker session for this strategy's Master. Log it in first.",
        )
    if not runtime_hooks.is_master_monitor_ready(str(master.id)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The engine is not monitoring this strategy's Master yet. Try again in a few seconds.",
        )
    return master, client


@router.post("/authenticate/{account_id}")
async def authenticate_account(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    account, client = await _load_client(account_id, db, redis)

    try:
        token = await client.get_session(force_refresh=True)
        return {
            "status": "authenticated",
            "account_id": str(account.id),
            "alice_user_id": account.alice_user_id,
            "session_token_preview": f"{token[:8]}...{token[-4:]}",
        }
    except IPNotWhitelistedError as ip_err:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"IP restriction error from broker: {ip_err}",
        )
    except AuthenticationError as auth_err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {auth_err}",
        )


@router.get("/funds/{account_id}")
async def get_account_funds(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    _, client = await _load_client(account_id, db, redis)
    funds = await client.get_funds()
    return {
        "account_id": funds.account_id,
        "available_margin": float(funds.available_margin),
        "collateral": float(funds.collateral),
        "used_margin": float(funds.used_margin),
        "updated_at": funds.updated_at.isoformat(),
    }


@router.get("/positions/{account_id}")
async def get_account_positions(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    _, client = await _load_client(account_id, db, redis)
    positions = await client.get_positions()
    return [
        {
            "symbol": p.symbol,
            "quantity": p.quantity,
            "side": p.side,
            "average_price": float(p.average_price),
            "exchange": p.exchange,
            "product_type": p.product_type,
        }
        for p in positions
    ]


@router.get("/instruments/search")
async def search_instruments(
    query: str = "",
    script_type: str = "Equity",
    exchange: Optional[str] = None,
    redis = Depends(get_redis),
):
    """
    Searches available instruments/stocks filtered by script_type (Equity, CE, PE, Future).
    """
    clean_q = query.strip().upper()
    st = script_type.strip().upper()
    results = []
    
    # Filter matched instruments from KNOWN_INSTRUMENTS
    for (ex, sym), (token, tr_sym) in KNOWN_INSTRUMENTS.items():
        if exchange and ex != exchange.upper():
            continue

        # Filter by script type
        if st in ("EQUITY", "EQ"):
            if ex not in ("NSE", "BSE") or " CE" in tr_sym or " PE" in tr_sym or "FUT" in tr_sym:
                continue
        elif st in ("CE", "CALL"):
            if not (" CE" in tr_sym or tr_sym.endswith("-CE") or "CE" in sym):
                continue
        elif st in ("PE", "PUT"):
            if not (" PE" in tr_sym or tr_sym.endswith("-PE") or "PE" in sym):
                continue
        elif st in ("FUTURE", "FUT", "FUTURES"):
            if not ("FUT" in tr_sym or "FUT" in sym):
                continue

        if not clean_q or clean_q in sym or clean_q in tr_sym:
            results.append({
                "symbol": sym,
                "trading_symbol": tr_sym,
                "token": token,
                "exchange": ex,
                "script_type": script_type,
            })
            if len(results) >= 50:
                break
                
    return {"exchange": exchange or "ALL", "script_type": script_type, "count": len(results), "instruments": results}


@router.post("/instruments/sync")
async def sync_instruments(
    exchange: str = "NSE",
    redis = Depends(get_redis),
):
    """
    Downloads and updates full Alice Blue contract master instruments into Redis cache.
    """
    count = await load_and_cache_contract_master(redis=redis, exchange=exchange)
    return {"status": "success", "exchange": exchange, "cached_instruments": count}


class ManualOrderRequest(BaseModel):
    # The strategy whose Master places the order; its Children receive the copy.
    strategy_id: uuid.UUID
    symbol: str = Field(min_length=1)
    exchange: str = "NSE"
    side: OrderSide
    quantity: int = Field(gt=0)
    product_type: str = "INTRADAY"
    order_type: OrderType = OrderType.MARKET
    price: Optional[Decimal] = Field(default=Decimal("0"), ge=Decimal("0"))
    trigger_price: Optional[Decimal] = Field(default=Decimal("0"), ge=Decimal("0"))

    @model_validator(mode="after")
    def validate_price_for_order_type(self) -> "ManualOrderRequest":
        if self.order_type == OrderType.LIMIT:
            if self.price is None or self.price <= Decimal("0"):
                raise ValueError("Limit price must be greater than 0 for LIMIT orders.")
        elif self.order_type in (OrderType.SL, OrderType.SL_M):
            if self.price is None or self.price <= Decimal("0"):
                raise ValueError("Limit price must be greater than 0 for Stop Loss orders.")
            if self.trigger_price is None or self.trigger_price <= Decimal("0"):
                raise ValueError("Trigger price must be greater than 0 for Stop Loss orders.")
        return self


async def _resolve_fill_status_and_price(client: AliceBlueClient, broker_order_id: Optional[str]) -> tuple[bool, Decimal, int]:
    """Polls the order book briefly to verify if the market order was actually FILLED on exchange."""
    if not broker_order_id:
        return False, Decimal("0"), 0
    for _ in range(3):
        try:
            book = await client.get_order_book()
            for record in book:
                if record.broker_order_id == broker_order_id:
                    if record.status in ("COMPLETE", "PARTIAL") and record.filled_quantity > 0:
                        return True, record.average_price, record.filled_quantity
        except Exception as exc:
            logger.warning(f"Fill-price lookup failed: {exc}")
        await asyncio.sleep(0.5)
    return False, Decimal("0"), 0


@router.post("/manual-order")
async def place_manual_master_order(
    payload: ManualOrderRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Places a REAL order on the master account and replicates it to all
    followers through the standard copy pipeline (validation, risk, dedup).
    Gated by the Live Trading switch and the master trading toggle.

    Crash-safety: a PENDING Order row is persisted under `client_order_id`
    BEFORE the broker is called, and the broker is submitted exactly once.
    If the broker accepts the order but a later status update fails, that
    PENDING/now-accepted row (identifiable by client_order_id and the logged
    broker_order_id) remains the recovery anchor -- the order is never
    reported back as unplaced.
    """
    if not (await get_live_trading_state(db)).enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Live Trading is OFF. Turn it ON in the app header to place orders.",
        )
    if not await is_master_trading_enabled(redis):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Master trading is toggled OFF.",
        )

    master, client = await _load_master_client(db, redis, payload.strategy_id)

    # Held (shared) through placement and replication so a Master switch for
    # this strategy can never interleave with it.
    async with membership.account_order_gate(str(master.id)):
        return await _place_and_replicate_manual_order(payload, db, redis, master, client)


async def _place_and_replicate_manual_order(
    payload: "ManualOrderRequest", db: AsyncSession, redis, master: Account, client: AliceBlueClient
) -> Dict[str, Any]:
    order_price = payload.price or Decimal("0")
    logger.info(
        f"Manual master order request received: master_id={master.id}, exchange={payload.exchange.upper()}, "
        f"symbol={payload.symbol.upper()}, side={payload.side.value}, quantity={payload.quantity}, "
        f"order_type={payload.order_type.value}, price={order_price}"
    )

    client_order_id = f"JT_MANUAL_{uuid.uuid4().hex[:12]}"

    # Map product string to standard ProductType
    p_str = str(payload.product_type).upper().strip()
    if p_str in ("CNC", "LONGTERM", "DELIVERY"):
        prod_type = ProductType.CNC
    elif p_str in ("NRML", "MTF", "CARRYFORWARD", "MARGIN"):
        prod_type = ProductType.NRML
    else:
        prod_type = ProductType.MIS

    req = OrderRequest(
        symbol=payload.symbol.upper(),
        exchange=payload.exchange.upper(),
        side=payload.side,
        quantity=payload.quantity,
        product_type=prod_type,
        order_type=payload.order_type,
        price=order_price,
        trigger_price=payload.trigger_price or Decimal("0"),
        client_order_id=client_order_id,
    )

    pipeline = CopyEnginePipeline(
        validator=EventValidator(redis=redis),
        stop_provider=build_stop_provider(redis, account_ids=None),
    )
    db_side = DBOrderSide.BUY if payload.side == OrderSide.BUY else DBOrderSide.SELL
    order_type_str = "LIMIT" if payload.order_type == OrderType.LIMIT else "MARKET"

    # Step 1: Pre-write the Order record in PENDING state BEFORE contacting the
    # broker, keyed by client_order_id, so this request is identifiable and
    # recoverable even if everything after the broker call below fails.
    await pipeline.save_or_update_order(
        internal_order_id=client_order_id,
        broker_order_id=None,
        account_id=master.id,
        source=OrderSource.INTERNAL_UI,
        exchange=payload.exchange.upper(),
        symbol=payload.symbol.upper(),
        side=db_side,
        order_type=order_type_str,
        product_type=payload.product_type,
        quantity=payload.quantity,
        filled_quantity=0,
        pending_quantity=payload.quantity,
        price=order_price,
        trigger_price=payload.trigger_price or Decimal("0"),
        status=OrderStatus.PENDING,
    )

    # Step 2: Submit to the broker. Exactly one call, no matter what happens
    # afterward -- a timed-out/ambiguous broker response must never be
    # re-sent from this function.
    result = await client.place_order(req)

    # Step 3: Handle rejection.
    if result.category != OrderResultCategory.SUCCESS or not result.broker_order_id:
        logger.warning(
            f"Master order placement rejected/failed: category={result.category.value}, "
            f"message='{result.message}'. Follower copy-replication SKIPPED."
        )
        await pipeline.save_or_update_order(
            internal_order_id=client_order_id,
            broker_order_id=None,
            account_id=master.id,
            source=OrderSource.INTERNAL_UI,
            exchange=payload.exchange.upper(),
            symbol=payload.symbol.upper(),
            side=db_side,
            order_type=order_type_str,
            product_type=payload.product_type,
            quantity=payload.quantity,
            price=order_price,
            status=OrderStatus.REJECTED,
            broker_status="REJECTED",
            rejection_reason=result.message,
        )
        return {
            "placed": False,
            "category": result.category.value,
            "message": result.message,
            "master_order_id": None,
            "replication": None,
        }

    master_order_id = result.broker_order_id
    logger.info(
        f"Master order successfully ACCEPTED by broker: master_order_id={master_order_id}. "
        "Proceeding to follower replication..."
    )

    # Step 4: Update the persisted order with the broker's response. The
    # broker now holds a real, live order for master_order_id -- any failure
    # from here on must never be reported back as "order not placed".
    try:
        if payload.order_type == OrderType.LIMIT:
            # Master LIMIT order accepted -> status is OPEN
            await pipeline.save_or_update_order(
                internal_order_id=client_order_id,
                broker_order_id=master_order_id,
                account_id=master.id,
                source=OrderSource.INTERNAL_UI,
                exchange=payload.exchange.upper(),
                symbol=payload.symbol.upper(),
                side=db_side,
                order_type="LIMIT",
                product_type=payload.product_type,
                quantity=payload.quantity,
                filled_quantity=0,
                pending_quantity=payload.quantity,
                price=order_price,
                status=OrderStatus.OPEN,
                broker_status="OPEN",
            )

            event = MasterTradeEvent(
                master_order_id=master_order_id,
                symbol=payload.symbol.upper(),
                exchange=payload.exchange.upper(),
                side=payload.side.value,
                quantity=payload.quantity,
                price=order_price,
                limit_price=order_price,
                product_type=payload.product_type,
                timestamp=datetime.now(timezone.utc),
                fill_sequence=1,
                account_id=str(master.id),
                order_type="LIMIT",
                event_type="LIMIT_CREATED",
                source="INTERNAL_UI",
                total_quantity=payload.quantity,
                pending_quantity=payload.quantity,
                status="OPEN",
            )
            is_filled = False
        else:
            # Master MARKET order -> check if actually FILLED on exchange
            is_filled, fill_price, filled_qty = await _resolve_fill_status_and_price(client, master_order_id)
            effective_price = fill_price if fill_price > Decimal("0") else order_price
            actual_qty = filled_qty if filled_qty > 0 else payload.quantity
            final_status = OrderStatus.COMPLETE if is_filled else OrderStatus.OPEN

            await pipeline.save_or_update_order(
                internal_order_id=client_order_id,
                broker_order_id=master_order_id,
                account_id=master.id,
                source=OrderSource.INTERNAL_UI,
                exchange=payload.exchange.upper(),
                symbol=payload.symbol.upper(),
                side=db_side,
                order_type="MARKET",
                product_type=payload.product_type,
                quantity=payload.quantity,
                filled_quantity=actual_qty if is_filled else 0,
                pending_quantity=0 if is_filled else payload.quantity,
                price=effective_price,
                average_price=effective_price if is_filled else Decimal("0"),
                status=final_status,
                broker_status="COMPLETE" if is_filled else "OPEN",
            )

            event = MasterTradeEvent(
                master_order_id=master_order_id,
                symbol=payload.symbol.upper(),
                exchange=payload.exchange.upper(),
                side=payload.side.value,
                quantity=actual_qty,
                price=effective_price,
                product_type=payload.product_type,
                timestamp=datetime.now(timezone.utc),
                fill_sequence=1,
                account_id=str(master.id),
                order_type="MARKET",
                event_type="MARKET_ACCEPTED",
                source="INTERNAL_UI",
                status="COMPLETE" if is_filled else "OPEN",
            )
    except Exception as exc:
        # The broker already has a live order for master_order_id. Losing the
        # local status update must never look like "nothing happened" -- log
        # loudly, audit it, and hand the caller everything needed to recover.
        logger.critical(
            f"Master order {master_order_id} (client_order_id={client_order_id}) was "
            f"ACCEPTED by the broker but recording/replicating it failed: {exc}",
            exc_info=True,
        )
        await emit_system_event(
            db,
            event_type="MANUAL_ORDER_POST_SUBMIT_FAILURE",
            severity="CRITICAL",
            message=(
                f"Master order {master_order_id} (client_order_id={client_order_id}) was "
                f"placed at the broker but the local status update failed: {exc}. "
                "Manual reconciliation required."
            ),
            account_id=master.id,
            metadata_payload={
                "broker_order_id": master_order_id,
                "client_order_id": client_order_id,
                "internal_order_id": client_order_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Order was accepted by the broker (broker_order_id={master_order_id}, "
                f"client_order_id={client_order_id}) but recording/replicating it locally "
                "failed. This order is NOT lost -- it is recoverable by client_order_id/"
                "broker_order_id. Manual reconciliation required; do not resubmit."
            ),
        )

    from app.services.alice_blue.manager import account_manager
    replication = None
    if payload.order_type == OrderType.LIMIT:
        replication = await pipeline.execute_replication(
            event=event,
            # Every registered account except this Master; strategy routing
            # selects the strategy's actual Children.
            clients=account_manager.get_clients_map_excluding(str(master.id)),
        )
    else:
        # MARKET orders are NEVER replicated from this endpoint, regardless
        # of whether the polling step above observed PARTIAL or COMPLETE.
        # broker.py has no access to the running MasterTradeDetector's
        # _seen_fills state, so an immediate replication here could not
        # inform the WebSocket/detector path of the quantity already sent -
        # when the detector later observes this same order's fill
        # independently, it would replicate its own (possibly full) delta on
        # top of what this endpoint already sent, over-replicating to
        # followers. The WebSocket -> MasterTradeDetector path is the sole
        # owner of MARKET follower replication; it already handles
        # partial-then-complete fills correctly via its own single source of
        # truth (_seen_fills, restart-safe per the cumulative-filled-quantity
        # fix), so nothing is lost - it just always arrives via WebSocket
        # rather than sometimes immediately.
        logger.info(
            f"Master MARKET order {master_order_id}: fill status ({'PARTIAL/COMPLETE' if is_filled else 'pending'}) "
            "observed via polling, but follower replication is NOT triggered from this endpoint. "
            "It will be replicated automatically via the WebSocket/MasterTradeDetector path once the "
            "broker confirms the fill there."
        )

    return {
        "placed": True,
        "category": result.category.value,
        "master_order_id": master_order_id,
        "client_order_id": client_order_id,
        "fill_price": float(event.price),
        "replication": replication,
    }
