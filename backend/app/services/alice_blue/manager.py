import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.crypto import decrypt_secret
from app.core.database import AsyncSessionLocal
from app.core.redis import get_redis
from app.models.accounts import Account, AccountType
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import FundsData, Position
# NOTE: the Angel One / Groww builders and credential_vault are imported inside the
# functions that use them. They import their broker clients, which import
# alice_blue.types, which runs this package's __init__ and therefore this module;
# a module-level import here made `import app.services.angel_one.client` (or the
# Groww one) fail with a circular import whenever it was the first thing imported.
from app.services.brokers.registry import (
    UnsupportedBrokerError,
    can_be_master,
    is_alice_blue,
    require_alice_blue,
    require_engine_broker,
)

logger = logging.getLogger("alice_blue_manager")

# P2 Finding #10: a follower whose auth attempt fails at startup (e.g. a
# daily OAuth token that hasn't rotated yet right as the container comes up)
# was previously left in `_clients` forever with no retry - every trade for
# it then fails closed silently for the rest of the process lifetime. This
# interval governs how often `reauth_failed_accounts()` re-attempts auth for
# ONLY the accounts whose last attempt failed. Same order of magnitude as
# StaleOrderSweeper/FollowerFillSyncer's periodic loops (60s default), but
# deliberately longer since this hits the broker's OAuth endpoint rather
# than just our own DB - no point hammering a broken login every 60s.
DEFAULT_REAUTH_INTERVAL_SECONDS = 300


@dataclass(frozen=True)
class RuntimeClientSync:
    """Outcome of AliceBlueAccountManager.sync_account().

    status: REGISTERED | ALREADY_REGISTERED | REMOVED | NOT_REGISTERED |
    FAILED | MASTER_UNCHANGED. `ready` is True only when the running engine
    holds a broker client for the account after the call."""
    account_id: str
    status: str
    ready: bool
    detail: str = ""


class AliceBlueAccountManager:
    """
    Coordinates and maintains AliceBlueClient instances for all 6 demat accounts.
    Allows concurrent session healthchecks and parallel operations.
    """

    def __init__(self):
        self._clients: Dict[str, AliceBlueClient] = {}
        self._master_account_id: Optional[str] = None
        # Tracks the outcome of the most recent auth attempt (initial
        # authenticate_all() call OR a subsequent reauth_failed_accounts()
        # retry) per account_id. Absence of a key means "never attempted
        # yet" and is treated the same as a failure by the reauth loop (safe
        # default: attempt it rather than silently skip it).
        self._last_auth_status: Dict[str, bool] = {}
        self.check_interval_seconds: int = DEFAULT_REAUTH_INTERVAL_SECONDS
        self._reauth_running: bool = False
        self._reauth_task: Optional[asyncio.Task] = None
        # Serializes every registry mutation (startup load and runtime
        # sync_account). Readers stay lock-free: they only do synchronous
        # dict reads, which cannot interleave with an await in asyncio.
        self._registry_lock = asyncio.Lock()

    @staticmethod
    def _build_client(acc: Account, redis) -> Any:
        # Broker-neutral routing. Fail closed before touching credentials: an
        # unsupported broker gets no client, and an account is never handed to
        # another broker's client (its secrets would go to that broker).
        spec = require_engine_broker(acc.broker)
        if spec.id == "angelone":
            from app.services.brokers.angel_builder import build_angel_client
            return build_angel_client(acc, redis)
        if spec.id == "groww":
            from app.services.brokers.groww_builder import build_groww_client
            return build_groww_client(acc, redis)
        require_alice_blue(acc.broker)
        # Parse optional proxy URL from notes field
        proxy_url: Optional[str] = None
        if acc.notes:
            for part in acc.notes.split(","):
                part = part.strip()
                if part.lower().startswith("proxyurl:"):
                    proxy_url = part[len("proxyurl:"):].strip()
                    break
                elif part.lower().startswith("http://") or part.lower().startswith("https://"):
                    proxy_url = part
                    break

        return AliceBlueClient(
            account_id=str(acc.id),
            user_id=acc.alice_user_id,
            api_secret=decrypt_secret(acc.api_credential.encrypted_api_secret),
            redis=redis,
            proxy_url=proxy_url,
        )

    async def initialize(self):
        """Loads accounts from PostgreSQL, decrypts credentials, instantiates AliceBlueClient per account."""
        async with self._registry_lock:
            redis = await get_redis()
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Account)
                    .where(Account.deleted_at.is_(None), Account.is_active.is_(True))
                    .options(selectinload(Account.api_credential), selectinload(Account.broker_credential))
                )
                accounts = result.scalars().all()
            from app.services.brokers.angel_builder import accounts_sharing_static_ip
            from app.services.brokers.credential_vault import CredentialError
            shared_ip_accounts = set(
                accounts_sharing_static_ip([a for a in accounts if (a.broker or "").lower() == "angelone"])
            )

            self._clients.clear()
            # Client instances are being rebuilt from scratch below, so any
            # previously-recorded auth status is stale - drop it rather than
            # risk the reauth loop trusting a status that belonged to a
            # now-discarded client object.
            self._last_auth_status.clear()
            for acc in accounts:
                if is_alice_blue(acc.broker) and acc.api_credential is None:
                    logger.warning(f"Account {acc.display_name} ({acc.id}) has no API credential; skipping.")
                    continue
                if str(acc.id) in shared_ip_accounts:
                    logger.error(
                        f"Account {acc.display_name} ({acc.id}) skipped: its static IP is recorded on another "
                        "Angel One account; each account needs its own registered IP."
                    )
                    continue
                try:
                    self._clients[str(acc.id)] = self._build_client(acc, redis)
                except (UnsupportedBrokerError, CredentialError) as exc:
                    logger.warning(f"Account {acc.display_name} ({acc.id}) skipped: {exc}")
                    continue
                # Only a broker whose orders can be detected may be the legacy Master;
                # Child-only brokers (Angel One, Groww) never are, whatever their label.
                if acc.account_type == AccountType.MASTER and can_be_master(acc.broker):
                    self._master_account_id = str(acc.id)

            logger.info(f"Initialized {len(self._clients)} Alice Blue clients. Master ID: {self._master_account_id}")

    async def sync_account(self, account_id: str, db: AsyncSession, redis) -> RuntimeClientSync:
        """
        Brings the running registry in line with one account's current DB
        state, so a follower activated or deactivated after startup takes
        effect without a restart.

        Active follower with no client: builds one and registers it only
        after its Redis session is confirmed (a Redis read - no broker call),
        so no half-ready client is ever visible. On failure nothing is
        registered and FAILED is returned. An already-registered client is
        kept as is. Inactive/deleted/missing account: its client is removed
        from the registry; in-flight operations keep the reference they
        already hold, and no broker order is touched. The master is never
        added, replaced or removed here - that still needs a restart.
        """
        acc_id = str(account_id)
        async with self._registry_lock:
            try:
                acc_uuid = uuid.UUID(acc_id)
            except ValueError:
                return RuntimeClientSync(acc_id, "FAILED", False, "invalid account id")

            acc = (
                await db.execute(
                    select(Account)
                    .where(Account.id == acc_uuid)
                    .options(selectinload(Account.api_credential), selectinload(Account.broker_credential))
                )
            ).scalar_one_or_none()

            if acc_id == self._master_account_id or (acc is not None and acc.account_type == AccountType.MASTER):
                return RuntimeClientSync(
                    acc_id, "MASTER_UNCHANGED", acc_id in self._clients,
                    "master client changes take effect only on engine restart",
                )

            if acc is None or acc.deleted_at is not None or not acc.is_active:
                removed = self._clients.pop(acc_id, None)
                self._last_auth_status.pop(acc_id, None)
                if removed is not None:
                    logger.info(f"Account {acc_id} removed from the runtime follower client registry.")
                    return RuntimeClientSync(acc_id, "REMOVED", False, "account inactive or deleted")
                return RuntimeClientSync(acc_id, "NOT_REGISTERED", False, "account inactive or deleted")

            if acc_id in self._clients:
                return RuntimeClientSync(acc_id, "ALREADY_REGISTERED", True)

            if is_alice_blue(acc.broker) and acc.api_credential is None:
                logger.error(f"Runtime client NOT registered for {acc.display_name} ({acc_id}): no API credential.")
                return RuntimeClientSync(acc_id, "FAILED", False, "no API credential configured")

            try:
                client = self._build_client(acc, redis)
            except Exception as exc:
                logger.error(f"Runtime client NOT registered for {acc.display_name} ({acc_id}): {exc}")
                return RuntimeClientSync(acc_id, "FAILED", False, f"client construction failed: {exc}")

            try:
                await client.get_session()
            except Exception as exc:
                logger.error(f"Runtime client NOT registered for {acc.display_name} ({acc_id}): {exc}")
                try:
                    await client._http.aclose()
                except Exception:
                    pass
                return RuntimeClientSync(acc_id, "FAILED", False, str(exc))

            self._clients[acc_id] = client
            self._last_auth_status[acc_id] = True
            logger.info(f"Account {acc.display_name} ({acc_id}) registered in the runtime follower client registry.")
            return RuntimeClientSync(acc_id, "REGISTERED", True)

    @staticmethod
    def _auto_login_ok(client: Any) -> bool:
        """False for clients (Angel One, Groww) whose login contacts the broker and
        must only ever happen on an explicit request, never at engine start. Looked
        up on the class so a mock client cannot masquerade as one."""
        return getattr(type(client), "AUTO_LOGIN_ON_STARTUP", True) is not False

    def get_client(self, account_id: str) -> Optional[AliceBlueClient]:
        return self._clients.get(str(account_id))

    def get_master_client(self) -> Optional[AliceBlueClient]:
        if self._master_account_id:
            return self._clients.get(self._master_account_id)
        return None

    def get_follower_clients(self) -> List[AliceBlueClient]:
        return [
            client
            for acc_id, client in self._clients.items()
            if acc_id != self._master_account_id
        ]

    def get_follower_clients_map(self) -> Dict[str, AliceBlueClient]:
        return {
            acc_id: client
            for acc_id, client in self._clients.items()
            if acc_id != self._master_account_id
        }

    def get_follower_ids(self) -> List[str]:
        return [acc_id for acc_id in self._clients if acc_id != self._master_account_id]

    # Strategy-role-aware views. A strategy's Master/Child roles are set
    # per strategy, not by account_type, so copy/sync paths must not drop
    # the account created as MASTER (it may be a Child now) - only the
    # account an order came FROM is excluded from its own copy targets.
    def get_all_clients_map(self) -> Dict[str, AliceBlueClient]:
        return dict(self._clients)

    def get_clients_map_excluding(self, account_id: Optional[str]) -> Dict[str, AliceBlueClient]:
        exclude = str(account_id) if account_id else None
        return {acc_id: client for acc_id, client in self._clients.items() if acc_id != exclude}

    def get_all_account_ids(self) -> List[str]:
        return list(self._clients)

    async def authenticate_all(self) -> Dict[str, bool]:
        """
        Authenticates all 6 accounts concurrently from the VPS static IP.
        Verifies whether all 6 sessions can run concurrently without IP conflict or rate limiting.
        """
        results = {}

        async def _auth(acc_id: str, client: AliceBlueClient):
            try:
                await client.get_session(force_refresh=True)
                results[acc_id] = True
                self._last_auth_status[acc_id] = True
            except Exception as e:
                logger.error(f"Concurrent auth failed for {acc_id}: {e}")
                results[acc_id] = False
                self._last_auth_status[acc_id] = False

        tasks = [_auth(acc_id, client) for acc_id, client in self._clients.items() if self._auto_login_ok(client)]
        await asyncio.gather(*tasks, return_exceptions=True)
        return results

    async def reauth_failed_accounts(self) -> Dict[str, bool]:
        """
        Single pass of the periodic re-authentication retry mechanism
        (P2 Finding #10). Re-attempts `get_session(force_refresh=True)`
        ONLY for accounts whose most recent auth attempt (the initial
        authenticate_all() call, or a previous call to this method) failed,
        or that have never been attempted at all.

        Accounts whose last attempt succeeded are left completely
        untouched - no redundant broker calls, and a still-broken account
        keeps failing closed (via the normal downstream broker-call error
        paths, e.g. FUNDS_FETCH_FAILED in the copy pipeline) exactly as
        before, until this successfully re-authenticates it. This never
        weakens fail-closed behavior - it only gives a currently-failed
        account another chance to become usable again.

        Returns {account_id: bool} for only the accounts actually retried
        in this pass (empty dict if nothing currently needs retrying).
        """
        targets = [
            acc_id for acc_id, client in self._clients.items()
            if self._last_auth_status.get(acc_id) is not True and self._auto_login_ok(client)
        ]
        if not targets:
            return {}

        retry_results: Dict[str, bool] = {}

        async def _retry(acc_id: str):
            client = self._clients.get(acc_id)
            if client is None:
                return
            try:
                await client.get_session(force_refresh=True)
                retry_results[acc_id] = True
                self._last_auth_status[acc_id] = True
                logger.info(f"Re-authentication succeeded for previously-failed account {acc_id}.")
            except Exception as e:
                retry_results[acc_id] = False
                self._last_auth_status[acc_id] = False
                logger.warning(f"Re-authentication retry still failing for account {acc_id}: {e}")

        tasks = [_retry(acc_id) for acc_id in targets]
        await asyncio.gather(*tasks, return_exceptions=True)
        return retry_results

    async def start_reauth_loop(self) -> None:
        """Starts the background periodic reauth retry task. Follows the
        same start()/stop()/_run_loop() shape as StaleOrderSweeper and
        FollowerFillSyncer (backend/app/services/reliability/) for
        consistency with this codebase's other periodic background tasks.

        NOT called automatically anywhere yet - see manager.py's module
        docstring / P2 Finding #10 fix notes for where this needs to be
        wired into EngineLoop's startup.
        """
        self._reauth_running = True
        self._reauth_task = asyncio.create_task(self._reauth_run_loop())
        logger.info("AliceBlueAccountManager re-auth loop started.")

    async def stop_reauth_loop(self) -> None:
        self._reauth_running = False
        if self._reauth_task:
            self._reauth_task.cancel()
            try:
                await self._reauth_task
            except asyncio.CancelledError:
                pass
        logger.info("AliceBlueAccountManager re-auth loop stopped.")

    async def _reauth_run_loop(self) -> None:
        while self._reauth_running:
            try:
                await self.reauth_failed_accounts()
            except Exception as e:
                logger.error(f"Error in AliceBlueAccountManager re-auth loop: {e}", exc_info=True)
            await asyncio.sleep(self.check_interval_seconds)

    async def fetch_all_funds(self) -> Dict[str, FundsData]:
        """Fetches funds for all active accounts in parallel."""
        funds_map = {}

        async def _fetch(acc_id: str, client: AliceBlueClient):
            funds_map[acc_id] = await client.get_funds()

        tasks = [_fetch(acc_id, client) for acc_id, client in self._clients.items()]
        await asyncio.gather(*tasks, return_exceptions=True)
        return funds_map


# Global Singleton Manager
account_manager = AliceBlueAccountManager()
