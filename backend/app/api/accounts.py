import uuid
import logging
import httpx
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select, case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.trading_day import trading_day, trading_day_start_utc
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.exceptions import AuthenticationError, IPNotWhitelistedError
from app.models.accounts import Account, ApiCredential, CopySetting, AccountType
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderStatus
from app.models.strategies import GroupStrategy, StrategyAccountRole, StrategyMember
from app.schemas.accounts import (
    AccountCreate,
    AccountResponse,
    AccountUpdate,
    CopySettingResponse,
    CopySettingUpdate,
)

from app.services.brokers.catalog import SUPPORTED_BROKERS
from app.services.static_ip import account_static_ip_host
from app.services.pnl.service import PnlService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["Accounts & Copy Configuration"])


@router.get("/supported-brokers")
async def get_supported_brokers():
    """Returns the catalog of all supported Indian stockbrokers and credential requirements."""
    return {"brokers": SUPPORTED_BROKERS}


# ─── Schema for per-account metrics ──────────────────────────────────────────
class AccountActivityCounts(BaseModel):
    in_group: int = 0           # number of strategies this account is a member of
    orders_count: int = 0       # total orders this trading day
    pending_count: int = 0
    completed_count: int = 0
    rejected_count: int = 0
    cancelled_count: int = 0    # FAILED + TIMEOUT treated as cancelled
    needs_review_count: int = 0  # SUBMITTED (in flight, ambiguous) + RECONCILIATION_REQUIRED -
                                  # never folded into pending/cancelled, since those imply a
                                  # known, resolving-on-its-own state; this one needs a human
                                  # to check GET /reliability/reconciliation/records.


class PnlComponentCheck(BaseModel):
    jettone: Optional[str] = None   # Jettone's value, 2-decimal string
    broker: Optional[str] = None    # Alice Blue's value, rounded to 2 decimals
    diff: Optional[str] = None
    status: str                     # MATCH / WITHIN_TOLERANCE / MISMATCH / NOT_COMPARABLE / UNAVAILABLE


class PnlBrokerCheck(BaseModel):
    status: str
    tolerance: Optional[str] = None
    realized: PnlComponentCheck     # vs sum(realizedPnl)
    unrealized: PnlComponentCheck   # vs sum(unrealizedPnl)
    total: PnlComponentCheck        # vs sum(MtoM)


class AccountMetrics(AccountActivityCounts):
    account_id: str
    # Gross P&L from this account's OWN broker fills (app/services/pnl):
    # 2-decimal strings, or null when the value could not be verified.
    # Never turnover, never derived from another account or copy_trades.
    realized_pnl: Optional[str] = None
    unrealized_pnl: Optional[str] = None
    total_pnl: Optional[str] = None
    pnl_basis: str                  # "GROSS": before brokerage, STT, GST, exchange/SEBI charges, stamp duty
    pnl_status: str                 # OK / PARTIAL / UNAVAILABLE
    pnl_notes: List[str] = []
    open_positions_count: Optional[int] = None  # POS: broker position rows with netQuantity != 0
    broker_check: PnlBrokerCheck
    trading_day: str
    as_of: str


class AllAccountMetrics(BaseModel):
    metrics: Dict[str, AccountMetrics]


# ─── Endpoints ────────────────────────────────────────────────────────────────

async def _sync_static_ip_expiry(db: AsyncSession, account: Account) -> None:
    """
    A Static IP is identified by its host, so one expiry applies to every account using that
    host: renewing it (or entering its expiry again) updates the existing IP everywhere instead
    of leaving per-account copies that disagree. Only touches proxy_expires_on.
    """
    host = account_static_ip_host(account.notes)
    if host is None or account.proxy_expires_on is None:
        return
    res = await db.execute(select(Account).where(Account.deleted_at.is_(None), Account.id != account.id))
    for other in res.scalars().all():
        if account_static_ip_host(other.notes) == host:
            other.proxy_expires_on = account.proxy_expires_on


@router.get("", response_model=List[AccountResponse])
async def list_accounts(
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
):
    query = (
        select(Account)
        .where(Account.deleted_at.is_(None))
        .options(selectinload(Account.copy_settings))
        .order_by(Account.account_type.asc(), Account.created_at.asc())
    )
    result = await db.execute(query)
    accounts = result.scalars().all()

    now_utc = datetime.now(timezone.utc)
    db_changed = False

    for acc in accounts:
        # Check if session is valid for TODAY (same calendar date & within 18h TTL)
        is_live = False
        if acc.session_valid and acc.last_authenticated_at:
            auth_dt = acc.last_authenticated_at
            # Ensure auth_dt is timezone-aware for comparison
            if auth_dt.tzinfo is None:
                auth_dt = auth_dt.replace(tzinfo=timezone.utc)

            is_same_day = (auth_dt.date() == now_utc.date())
            is_within_ttl = ((now_utc - auth_dt).total_seconds() <= 18 * 3600)

            # Check if token exists in Redis
            has_redis_token = True
            if redis:
                cached_token = await redis.get(f"alice:session:{acc.id}")
                if not cached_token:
                    has_redis_token = False

            is_live = is_same_day and is_within_ttl and has_redis_token

        if acc.session_valid != is_live:
            acc.session_valid = is_live
            db_changed = True

        if is_live:
            ms_val = 28 + (int(abs(hash(acc.id)) + now_utc.second) % 20)
            acc.ping_ms = ms_val
        else:
            acc.ping_ms = None

    if db_changed:
        await db.commit()

    # Current role per account (strategy_account_roles) - the same source the
    # Dashboard's strategy rows use. Never derived from `account_type`.
    role_rows = await db.execute(
        select(StrategyAccountRole.account_id, StrategyAccountRole.role, GroupStrategy.name)
        .join(GroupStrategy, GroupStrategy.id == StrategyAccountRole.strategy_id)
        .where(GroupStrategy.deleted_at.is_(None))
    )
    roles = {row.account_id: (row.role, row.name) for row in role_rows}
    for acc in accounts:
        acc.strategy_role, acc.strategy_name = roles.get(acc.id, (None, None))

    return accounts


def get_broker_client_provider() -> Callable[[str], Optional[AliceBlueClient]]:
    """The engine loop's live per-account broker clients (a dependency so
    tests can substitute fakes). Imported lazily, as reliability.py does."""
    from app.services.alice_blue.manager import account_manager

    return account_manager.get_client


def get_runtime_client_manager():
    """The engine's broker-client registry (a dependency so tests can
    substitute an isolated manager)."""
    from app.services.alice_blue.manager import account_manager

    return account_manager


async def _sync_runtime_client(manager, account: Account, db: AsyncSession, redis, report_failure: bool) -> str:
    """Syncs the running engine's broker client for `account` after its DB
    change is committed. A failed activation leaves the DB change in place
    (copies to the account keep failing closed) but is made visible: logged,
    recorded as an ERROR system event when report_failure is set, and
    returned as the status string."""
    from app.services.alice_blue.manager import RuntimeClientSync
    from app.services.monitoring.notifier import emit_system_event

    try:
        result = await manager.sync_account(str(account.id), db=db, redis=redis)
    except Exception as exc:
        logger.error(f"[{account.id}] Runtime broker client sync failed: {exc}", exc_info=True)
        result = RuntimeClientSync(str(account.id), "FAILED", False, f"runtime sync error: {exc}")

    if result.status == "FAILED" and report_failure:
        await emit_system_event(
            db,
            event_type="RUNTIME_CLIENT_UNAVAILABLE",
            severity="ERROR",
            message=(
                f"{account.display_name} is active but the running engine has no broker client for it "
                f"({result.detail}). Copies to this account will fail closed until it is reconnected."
            ),
            account_id=account.id,
        )
    return result.status


@router.get("/metrics", response_model=AllAccountMetrics)
async def get_all_account_metrics(
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
    client_provider: Callable[[str], Optional[AliceBlueClient]] = Depends(get_broker_client_provider),
):
    """
    Returns per-account metrics for all active accounts.
    Used by the Demat and Group Strategy cards to show: In Group, P&L, POS,
    Orders, Pending, Complete, Reject, Cancel.

    Counts come from Jettone's own tables for the current trading day
    (00:00 in settings.TRADING_TIMEZONE). P&L and POS come from each
    account's own broker order and position books via PnlService.
    """
    now = datetime.now(timezone.utc)
    today_start = trading_day_start_utc(trading_day(now))

    # ── 1. Get all active account IDs ────────────────────────────────────────
    acc_q = await db.execute(
        select(Account.id).where(Account.deleted_at.is_(None))
    )
    account_ids = [row[0] for row in acc_q.all()]

    result: Dict[str, AccountActivityCounts] = {}

    # Initialise every account with zeros so the frontend always gets a row
    for aid in account_ids:
        result[str(aid)] = AccountActivityCounts()

    # ── 2. In-group count (strategy_members + group_strategies as master) ─────
    # Count strategies where account is a member
    member_q = await db.execute(
        select(StrategyMember.account_id, func.count().label("cnt"))
        .join(GroupStrategy, GroupStrategy.id == StrategyMember.strategy_id)
        .where(GroupStrategy.deleted_at.is_(None))
        .group_by(StrategyMember.account_id)
    )
    for row in member_q.all():
        aid = str(row[0])
        if aid in result:
            result[aid].in_group += row[1]

    # Count strategies where account is the master
    master_q = await db.execute(
        select(GroupStrategy.master_account_id, func.count().label("cnt"))
        .where(
            GroupStrategy.deleted_at.is_(None),
            GroupStrategy.master_account_id.isnot(None),
        )
        .group_by(GroupStrategy.master_account_id)
    )
    for row in master_q.all():
        aid = str(row[0])
        if aid in result:
            result[aid].in_group += row[1]

    # ── 3. This trading day's copy_trade stats (follower side) ──────────────
    trade_q = await db.execute(
        select(
            CopyTrade.follower_account_id,
            CopyTrade.status,
            func.count().label("cnt"),
        )
        .where(CopyTrade.queued_at >= today_start)
        .group_by(CopyTrade.follower_account_id, CopyTrade.status)
    )

    for row in trade_q.all():
        aid = str(row.follower_account_id)
        if aid not in result:
            continue
        m = result[aid]
        cnt = int(row.cnt)
        m.orders_count += cnt

        if row.status == CopyTradeStatus.PENDING:
            m.pending_count += cnt
        elif row.status == CopyTradeStatus.SUCCESS:
            m.completed_count += cnt
        elif row.status == CopyTradeStatus.REJECTED:
            m.rejected_count += cnt
        elif row.status in (CopyTradeStatus.FAILED, CopyTradeStatus.TIMEOUT, CopyTradeStatus.SKIPPED):
            m.cancelled_count += cnt
        elif row.status in (CopyTradeStatus.SUBMITTED, CopyTradeStatus.RECONCILIATION_REQUIRED):
            m.needs_review_count += cnt

    # ── 4. Today's own Order-table stats for MASTER accounts ──────────────────
    # CopyTrade.follower_account_id is never a master's own id (it only ever
    # records follower-side copies), so without this a master's own order
    # activity - whether placed via Jettone's UI or externally via Alice Blue
    # Mobile/Web - never shows on its Demat card. Scoped to MASTER accounts
    # only: a follower's own orders are already fully covered by the
    # CopyTrade query above, so adding Order-table counts for followers too
    # would double-count the same activity under two different tables.
    master_ids_q = await db.execute(
        select(Account.id).where(
            Account.deleted_at.is_(None),
            Account.account_type == AccountType.MASTER,
        )
    )
    master_ids = [row[0] for row in master_ids_q.all()]

    if master_ids:
        order_q = await db.execute(
            select(
                Order.account_id,
                Order.status,
                func.count().label("cnt"),
            )
            .where(Order.account_id.in_(master_ids), Order.created_at >= today_start)
            .group_by(Order.account_id, Order.status)
        )

        for row in order_q.all():
            aid = str(row.account_id)
            if aid not in result:
                continue
            m = result[aid]
            cnt = int(row.cnt)
            m.orders_count += cnt

            if row.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIAL):
                m.pending_count += cnt
            elif row.status == OrderStatus.COMPLETE:
                m.completed_count += cnt
            elif row.status == OrderStatus.REJECTED:
                m.rejected_count += cnt
            elif row.status in (OrderStatus.CANCELLED, OrderStatus.FAILED):
                m.cancelled_count += cnt

    # ── 5. Gross P&L and open positions from each account's own broker books ─
    pnl = await PnlService(
        redis=redis, client_provider=client_provider, now=lambda: now
    ).summarize_accounts(result.keys())

    return AllAccountMetrics(
        metrics={
            aid: AccountMetrics(account_id=aid, **counts.model_dump(), **pnl[aid].as_api_fields())
            for aid, counts in result.items()
        }
    )


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: AccountCreate,
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
    manager=Depends(get_runtime_client_manager),
):
    clean_user_id = payload.alice_user_id.strip()
    
    # Check if an account with this alice_user_id already exists (including soft-deleted)
    query = select(Account).where(func.lower(Account.alice_user_id) == clean_user_id.lower())
    res = await db.execute(query)
    existing_acc = res.scalar_one_or_none()

    if existing_acc:
        if existing_acc.deleted_at is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Account with this Alice Blue user ID already exists",
            )
        # Restore soft-deleted account with new details
        account = existing_acc
        account.deleted_at = None
        account.account_type = payload.account_type
        account.display_name = payload.display_name
        account.broker = payload.broker or "alicebluenew"
        account.is_active = payload.is_active
        account.session_valid = False
        account.last_authenticated_at = None
        account.notes = payload.notes
        account.proxy_expires_on = payload.proxy_expires_on
    else:
        account = Account(
            account_type=payload.account_type,
            display_name=payload.display_name,
            alice_user_id=clean_user_id,
            broker=payload.broker or "alicebluenew",
            is_active=payload.is_active,
            session_valid=False,
            last_authenticated_at=None,
            notes=payload.notes,
            proxy_expires_on=payload.proxy_expires_on,
        )
        db.add(account)

    try:
        await db.flush()
        await _sync_static_ip_expiry(db, account)
        
        # Update or create ApiCredential
        cred_res = await db.execute(select(ApiCredential).where(ApiCredential.account_id == account.id))
        credential = cred_res.scalar_one_or_none()
        if credential:
            credential.encrypted_api_secret = encrypt_secret(payload.api_secret)
        else:
            credential = ApiCredential(
                account_id=account.id,
                encrypted_api_secret=encrypt_secret(payload.api_secret),
            )
            db.add(credential)

        if payload.account_type == AccountType.FOLLOWER:
            set_res = await db.execute(select(CopySetting).where(CopySetting.follower_account_id == account.id))
            setting = set_res.scalar_one_or_none()
            if not setting:
                setting = CopySetting(
                    follower_account_id=account.id,
                    copy_enabled=False,
                    fixed_qty=1,
                )
                db.add(setting)

        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        err_text = str(exc).lower()
        if "unique" in err_text and "alice_user_id" in err_text:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Account with this Alice Blue user ID already exists",
            )
        logger.error(f"Account creation failed with unexpected integrity error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create account due to a data constraint.",
        )
    await db.refresh(account)

    # A brand-new account normally has no Alice Blue session yet, so FAILED
    # here is expected until its first OAuth login (the callback registers
    # it) - reported in the response only, not as a system event.
    runtime_status = await _sync_runtime_client(manager, account, db, redis, report_failure=False)

    res = await db.execute(
        select(Account).where(Account.id == account.id).options(selectinload(Account.copy_settings))
    )
    created = res.scalar_one()
    created.runtime_client_status = runtime_status
    return created


class CallbackPayload(BaseModel):
    auth_code: str
    account_id: Optional[str] = None
    target_user_id: Optional[str] = None
    returned_user_id: Optional[str] = None


RECONNECT_INTENT_TTL_SECONDS = 5 * 60  # a real Alice Blue login completes in seconds, not minutes


@router.post("/{account_id}/reconnect-intent", status_code=status.HTTP_204_NO_CONTENT)
async def record_reconnect_intent(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
):
    """
    Records, server-side, which account a Reconnect click is about to
    authenticate — called right before opening the Alice Blue login tab.

    This exists because Jettone is reachable on two host ports (3000 and
    8080), but Alice Blue's registered redirect_uri is fixed to one of them.
    Browser localStorage/sessionStorage are scoped per-origin *including
    port*, so whenever the user is browsing on a different port than the
    redirect_uri, a value written to storage before the redirect is
    invisible on the callback page after it — not just for a stale tab, but
    for every reconnect. This Redis-backed intent doesn't depend on the
    browser's port at all, so /accounts/callback can still identify the
    intended account through it.
    """
    query = select(Account).where(Account.id == account_id, Account.deleted_at.is_(None))
    res = await db.execute(query)
    account = res.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if redis:
        await redis.set(
            f"alice:reconnect_intent:{account.alice_user_id.strip().upper()}",
            str(account.id),
            ex=RECONNECT_INTENT_TTL_SECONDS,
        )


@router.post("/callback")
async def handle_demat_callback(
    payload: CallbackPayload,
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
    manager=Depends(get_runtime_client_manager),
):
    """
    Handles Alice Blue OAuth redirect callback with authCode.
    Marks ONLY the specific targeted account as session_valid = True.
    Guards strictly against user ID mismatch if browser was logged into another account.
    """
    if not payload.auth_code or not payload.auth_code.strip():
        raise HTTPException(status_code=400, detail="Missing auth_code from broker")

    logger.info(f"Callback received: account_id={payload.account_id}, target_user_id={payload.target_user_id}, returned_user_id={payload.returned_user_id}, auth_code_len={len(payload.auth_code.strip())}")

    # Require the caller to state which account it's reconnecting. Without
    # this, a callback can only be matched by whatever user Alice Blue
    # returned (see the returned_user_id fallback below), which makes the
    # account-mismatch guard vacuous — it always "matches" because the
    # target account is derived from the same value it would be compared
    # against.
    #
    # account_id/target_user_id normally come from browser storage set right
    # before opening the Alice Blue login tab (see
    # frontend/src/lib/alice-blue-login.ts). But Jettone is deployed on two
    # host ports (3000 and 8080 — see docker-compose.yml), and Alice Blue's
    # registered redirect_uri is fixed to one of them while the user may be
    # browsing the app on the other. localStorage/sessionStorage are scoped
    # per-origin *including port*, so that browser storage is invisible on
    # the callback's origin whenever the two ports differ — not just for a
    # stale/leftover tab, but for every single reconnect. reconnect-intent
    # (set via POST /{account_id}/reconnect-intent right before opening the
    # login tab) is the server-side equivalent: it doesn't care which port
    # the browser is on, so it's the reliable source of truth here.
    if not payload.account_id and not payload.target_user_id:
        intent_account_id: Optional[str] = None
        if redis and payload.returned_user_id:
            intent_account_id = await redis.get(
                f"alice:reconnect_intent:{payload.returned_user_id.strip().upper()}"
            )
        if intent_account_id:
            payload.account_id = intent_account_id
            await redis.delete(f"alice:reconnect_intent:{payload.returned_user_id.strip().upper()}")
            logger.info(f"Callback resolved via reconnect-intent for returned_user_id={payload.returned_user_id}")
        else:
            logger.warning(
                f"Callback rejected: no account_id/target_user_id provided and no "
                f"matching reconnect-intent found (returned_user_id={payload.returned_user_id}). "
                f"Likely a stale Alice Blue tab not opened via Jettone's Reconnect button."
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "This login wasn't started from Jettone's Reconnect button, so it's unclear "
                    "which account it belongs to. Please close this tab and click Reconnect again "
                    "from the Jettone app."
                ),
            )

    target_account = None

    if payload.account_id:
        try:
            acc_uuid = uuid.UUID(payload.account_id)
            query = select(Account).where(Account.id == acc_uuid, Account.deleted_at.is_(None))
            res = await db.execute(query)
            target_account = res.scalar_one_or_none()
        except ValueError:
            pass

    if not target_account and payload.target_user_id:
        query = select(Account).where(func.lower(Account.alice_user_id) == payload.target_user_id.strip().lower(), Account.deleted_at.is_(None))
        res = await db.execute(query)
        target_account = res.scalar_one_or_none()

    if not target_account and payload.returned_user_id:
        query = select(Account).where(func.lower(Account.alice_user_id) == payload.returned_user_id.strip().lower(), Account.deleted_at.is_(None))
        res = await db.execute(query)
        target_account = res.scalar_one_or_none()

    if not target_account:
        raise HTTPException(status_code=404, detail="No matching account found for this session")

    # Strict check: Did Alice Blue return a different user ID than the intended target account?
    if payload.returned_user_id and target_account.alice_user_id:
        if payload.returned_user_id.strip().upper() != target_account.alice_user_id.strip().upper():
            err_msg = (
                f"Account mismatch: Browser is currently logged into Alice Blue as '{payload.returned_user_id}', "
                f"but this connection is for '{target_account.display_name}' ({target_account.alice_user_id}). "
                f"Please log out of Alice Blue and scan as {target_account.alice_user_id}."
            )
            logger.warning(err_msg)
            raise HTTPException(status_code=400, detail=err_msg)

    # ── Instant Broker Session Verification ───────────────────────────────────
    # Perform official Alice Blue A3 OAuth session exchange (getUserDetails)
    # using the fresh authCode, decrypted API Secret, and ProxyURL (if configured).
    credential_res = await db.execute(select(ApiCredential).where(ApiCredential.account_id == target_account.id))
    credential = credential_res.scalar_one_or_none()
    if not credential:
        target_account.session_valid = False
        await db.commit()
        raise HTTPException(status_code=400, detail="Account has no API Secret configured.")

    api_secret = decrypt_secret(credential.encrypted_api_secret)

    # Read per-account proxy from notes
    account_proxy_url: Optional[str] = None
    if target_account.notes:
        for part in target_account.notes.split(","):
            part = part.strip()
            if part.lower().startswith("proxyurl:"):
                account_proxy_url = part[len("proxyurl:"):].strip()
                break
            elif part.lower().startswith("http://") or part.lower().startswith("https://"):
                account_proxy_url = part
                break

    client = AliceBlueClient(
        account_id=str(target_account.id),
        user_id=target_account.alice_user_id,
        api_secret=api_secret,
        redis=redis,
        proxy_url=account_proxy_url,
    )

    safe_proxy = (account_proxy_url.split("@")[-1] if account_proxy_url and "@" in account_proxy_url else account_proxy_url) if account_proxy_url else "NONE (direct)"
    # Never log target_account.notes verbatim here: it's where the UI stores
    # the account's APISecret and any ProxyURL basic-auth credentials.
    # safe_proxy above already strips proxy userinfo for this exact reason.
    logger.info(f"[{target_account.id}] Attempting A3 auth for {target_account.alice_user_id} via proxy={safe_proxy}")

    try:
        session_token = await client.authenticate_with_auth_code(payload.auth_code.strip())
    except IPNotWhitelistedError as ip_exc:
        logger.warning(f"[{target_account.id}] Session auth FAILED (IPNotWhitelisted) for {target_account.alice_user_id}: {ip_exc}")
        target_account.session_valid = False
        await db.commit()
        if redis:
            await redis.delete(f"alice:session:{target_account.id}")
        raise HTTPException(
            status_code=403,
            detail=f"Alice Blue IP restriction: {ip_exc}",
        )
    except AuthenticationError as auth_exc:
        logger.warning(f"[{target_account.id}] Session auth FAILED (AuthenticationError) for {target_account.alice_user_id}: {auth_exc}")
        target_account.session_valid = False
        await db.commit()
        if redis:
            await redis.delete(f"alice:session:{target_account.id}")
        raise HTTPException(
            status_code=401,
            detail=f"Alice Blue session authentication failed: {auth_exc}",
        )
    except Exception as exc:
        logger.error(f"[{target_account.id}] Unexpected session auth failure for {target_account.alice_user_id}: {exc}")
        target_account.session_valid = False
        await db.commit()
        if redis:
            await redis.delete(f"alice:session:{target_account.id}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to authenticate with Alice Blue: {exc}",
        )

    # ── Auth passed — session is confirmed live from this server / proxy ──────
    target_account.session_valid = True
    target_account.is_active = True
    target_account.last_authenticated_at = datetime.now(timezone.utc)

    await db.commit()
    logger.info(f"[{target_account.id}] Alice Blue A3 session VERIFIED and live for {target_account.alice_user_id}")
    runtime_status = await _sync_runtime_client(manager, target_account, db, redis, report_failure=True)
    return {
        "status": "success",
        "message": f"Alice Blue session verified and live for {target_account.display_name}",
        "runtime_client_status": runtime_status,
    }


@router.post("/reset-sessions")
async def reset_account_sessions(
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Resets all account sessions to session_valid = False until the user performs web OAuth login.
    """
    query = select(Account).where(Account.deleted_at.is_(None))
    res = await db.execute(query)
    accounts = res.scalars().all()

    for acc in accounts:
        acc.session_valid = False
        acc.last_authenticated_at = None
        if redis:
            await redis.delete(f"alice:session:{acc.id}")

    await db.commit()
    return {"status": "success", "message": "All account sessions reset to offline"}


@router.post("/{account_id}/sign-out", response_model=AccountResponse)
async def sign_out_account(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
):
    """
    Manually marks a single account as signed out on Jettone's side: clears
    session_valid, last_authenticated_at, the cached Redis session token, and
    any pending reconnect-intent.

    This does NOT log the account out of Alice Blue's own website — Jettone
    has no access to that cross-origin browser session (see
    record_reconnect_intent above for why). It only resets Jettone's own
    bookkeeping, which matters because list_accounts' "Live" badge trusts
    that cached state and never re-verifies with the broker — this gives a
    manual way to say "treat this as offline" when you suspect Alice Blue
    invalidated the session server-side, or just to get a clean slate before
    reconnecting.
    """
    query = (
        select(Account)
        .where(Account.id == account_id, Account.deleted_at.is_(None))
        .options(selectinload(Account.copy_settings))
    )
    res = await db.execute(query)
    account = res.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    account.session_valid = False
    account.last_authenticated_at = None
    account.ping_ms = None

    if redis:
        await redis.delete(f"alice:session:{account.id}")
        await redis.delete(f"alice:reconnect_intent:{account.alice_user_id.strip().upper()}")

    await db.commit()
    await db.refresh(account)
    logger.info(f"[{account.id}] Signed out of Alice Blue session on Jettone's side (manual).")
    return account


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    query = select(Account).where(Account.id == account_id, Account.deleted_at.is_(None)).options(selectinload(Account.copy_settings))
    res = await db.execute(query)
    account = res.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@router.get("/{account_id}/orders")
async def get_account_orders(
    account_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Dedicated order history for a single account - the "View" action on
    Demat Accounts / Group Strategy child cards. Kept separate from the
    unified /trades/orders listing (which is Order Manager's own, cross-
    account view) so a single-account page never depends on that route."""
    account = (
        await db.execute(select(Account).where(Account.id == account_id, Account.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    from app.api.trades import serialize_order

    query = (
        select(Order)
        .where(Order.account_id == account_id)
        .order_by(Order.created_at.desc())
        .limit(min(limit, 100))
        .offset(max(offset, 0))
    )
    orders = (await db.execute(query)).scalars().all()
    return [serialize_order(o) for o in orders]


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: uuid.UUID,
    payload: AccountUpdate,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    manager=Depends(get_runtime_client_manager),
):
    query = (
        select(Account)
        .where(Account.id == account_id, Account.deleted_at.is_(None))
        .options(selectinload(Account.copy_settings))
    )
    res = await db.execute(query)
    account = res.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    update_data = payload.model_dump(exclude_unset=True)
    
    # Security: Do not allow premature session_valid=True without valid Redis session
    if update_data.get("session_valid") is True:
        has_token = False
        if redis:
            cached = await redis.get(f"alice:session:{account.id}")
            if cached:
                has_token = True
        if not has_token:
            update_data["session_valid"] = False

    for field, value in update_data.items():
        setattr(account, field, value)

    if "proxy_expires_on" in update_data:
        await _sync_static_ip_expiry(db, account)

    await db.commit()
    await db.refresh(account)

    if "is_active" in update_data:
        runtime_status = await _sync_runtime_client(
            manager, account, db, redis, report_failure=bool(account.is_active)
        )
        await db.refresh(account)
        account.runtime_client_status = runtime_status
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
    manager=Depends(get_runtime_client_manager),
):
    """Soft-delete an account by setting deleted_at timestamp."""
    query = select(Account).where(Account.id == account_id, Account.deleted_at.is_(None))
    res = await db.execute(query)
    account = res.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    account.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    await _sync_runtime_client(manager, account, db, redis, report_failure=False)


@router.put("/{account_id}/settings", response_model=CopySettingResponse)
async def update_copy_settings(
    account_id: uuid.UUID,
    payload: CopySettingUpdate,
    db: AsyncSession = Depends(get_db),
):
    query = select(CopySetting).where(CopySetting.follower_account_id == account_id)
    res = await db.execute(query)
    setting = res.scalar_one_or_none()
    if not setting:
        raise HTTPException(status_code=404, detail="Copy settings for this follower account not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(setting, field, value)

    await db.commit()
    await db.refresh(setting)
    return setting


@router.post("/{account_id}/renew-session", response_model=AccountResponse)
async def renew_account_session(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
    manager=Depends(get_runtime_client_manager),
):
    """Renew/Validate account session. Requires an active broker session token in Redis."""
    query = (
        select(Account)
        .where(Account.id == account_id, Account.deleted_at.is_(None))
        .options(selectinload(Account.copy_settings))
    )
    res = await db.execute(query)
    account = res.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    has_token = False
    if redis:
        token = await redis.get(f"alice:session:{account.id}")
        if token:
            has_token = True

    if not has_token:
        account.session_valid = False
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot renew: Active broker session token not found. Please log in via Alice Blue OAuth.",
        )

    account.session_valid = True
    account.is_active = True
    account.last_authenticated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(account)
    logger.info(f"[{account.id}] Direct API session renewal successful for {account.alice_user_id}")
    runtime_status = await _sync_runtime_client(manager, account, db, redis, report_failure=True)
    await db.refresh(account)
    account.runtime_client_status = runtime_status
    return account


