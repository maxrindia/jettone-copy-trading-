from pydantic_settings import BaseSettings, SettingsConfigDict
from decimal import Decimal
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Service
    PROJECT_NAME: str = "Alice Blue Copy Trading Engine"
    VERSION: str = "1.0.0"
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://copytrade_admin:password@postgres:5432/copytrading"

    # Redis
    REDIS_URL: str = "redis://:password@redis:6379/0"

    # Trading
    # Master account capital used by CAPITAL_PROPORTIONAL / RISK_BASED sizing.
    # MUST be set to a positive value via environment; the engine refuses to
    # start otherwise (validated in main.py lifespan — never bypass this).
    MASTER_CAPITAL: Decimal = Decimal("0")

    # Security & Encryption
    ALICE_BLUE_ENCRYPTION_KEY: Optional[str] = None

    # Internal service-to-service auth (Next.js -> FastAPI). Fail closed when unset.
    INTERNAL_API_TOKEN: Optional[str] = None

    # Alerting (Telegram). Unset = alerts are logged, not sent.
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    # Live trading gate. When false the engine runs in observe-only mode:
    # WS feed and risk pipeline run, but no broker orders are ever placed.
    ENGINE_LIVE_TRADING_ENABLED: bool = False

    # R3b restart recovery. When true, after an engine restart a LIMIT / SL-pending Master order whose fanout
    # PROVABLY began (a durable COPY_FANOUT_STARTED record) but did not reach every routed Child may be resumed for
    # the Children that have no decision yet - only inside a 10-minute window, only while the order is still working
    # and unchanged, and only with live trading and the master toggle ON. OFF by default: with it OFF, recovery only
    # reports. Lifecycle continuity (cancel / modify of orders that were already copied) is not governed by this flag.
    # See docs/RESTART_RECOVERY.md.
    ENGINE_RECOVERY_RESUME_ENABLED: bool = False

    # R4c observe-only instrument shadow comparison. OFF by default: no task is created and no event is written. When
    # on, a background job compares persisted Master orders' instruments with the in-memory broker catalogs and records
    # INSTRUMENT_SHADOW_COMPARE events for the read-only report; it never calls a broker or influences an order.
    # The interval is raised to at least 300 s, the window capped at 168 h and the order count at 500.
    # See docs/INSTRUMENT_SHADOW.md.
    INSTRUMENT_SHADOW_ENABLED: bool = False
    INSTRUMENT_SHADOW_INTERVAL_SECONDS: int = 900
    INSTRUMENT_SHADOW_WINDOW_HOURS: int = 24
    INSTRUMENT_SHADOW_MAX_ORDERS: int = 500
    # R4i: path to an official NSE security-master file you downloaded yourself (NSE_CM_security_DDMMYYYY.csv[.gz]; the
    # snapshot date is read from the file name). Empty = off. Used only by the shadow job and report as MASTER_TOKEN_LINK_UNVERIFIED
    # evidence; never downloaded, never read by the planner or the copy pipeline. See docs/INSTRUMENT_SHADOW.md.
    INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH: str = ""

    # R4f per-order planner checks. OFF by default: nothing changes. When on, each new Child copy is first checked for
    # segment, instrument mapping, product and order type against the Child broker's capability profile
    # (services/instruments/planner.py); a failing Child is SKIPPED with INSTRUMENT_UNMAPPED / SEGMENT_NOT_SUPPORTED /
    # PRODUCT_NOT_SUPPORTED / ORDER_TYPE_NOT_SUPPORTED on its own copy_trades row, and other Children are unaffected.
    # Fails closed: a cross-broker Child needs an ISIN-level Master identity, which does not exist yet, so such a Child is
    # skipped as INSTRUMENT_UNMAPPED. Do not turn this on until the shadow observation (docs/INSTRUMENT_SHADOW_OBSERVATION.md)
    # has been reviewed. It is not a pair gate and enables no broker.
    CANONICAL_MAPPING_ENFORCED: bool = False

    # Lot / freeze quantity validation. OBSERVE is the only implemented mode: at each Child final-quantity point the pipeline
    # logs and counts what a validator would say (services/instruments/terms.py) and changes NO allow / skip / send decision.
    # Any other value is treated as OBSERVE; enforcement does not exist yet.
    QUANTITY_VALIDATION_MODE: str = "OBSERVE"

    # TEMPORARY test switch: true skips the follower SELL position checks in
    # both places - initial SELL placement (_enforce_sell_position_safety) and
    # the SELL child LIMIT->MARKET reprice (handle_master_modification) - so a
    # follower SELL can open a naked short. Default false = both checks
    # active. Remove once testing is done.
    SELL_POSITION_SAFETY_BYPASS: bool = False

    # Angel One (SmartAPI) Child support. Default false = Angel One accounts can
    # not be added, logged in or traded and no Angel One request is ever made.
    # Even when true, trading stays closed until the response contract
    # (app/services/angel_one/contract.py) is verified from a supervised capture.
    ANGEL_ONE_ENABLED: bool = False
    # Separate, independent gates (all default false). ANGEL_ONE_ENABLED alone only allows account setup
    # and an explicit login. Child / Master capability each additionally needs its own flag AND a
    # verified response contract in code (angel_one/contract.py).
    ANGEL_ONE_CHILD_TRADING_ENABLED: bool = False
    ANGEL_ONE_MASTER_ENABLED: bool = False
    # Client-side rate limits, "requests/seconds" pairs separated by commas, e.g. "10/1,200/60".
    # Empty (the default) = no client-side limiting. NO limit is assumed: set them only from Angel One's
    # documented figures. A malformed value disables ordering (fail closed), it is never ignored.
    ANGEL_ONE_ORDER_RATE_LIMITS: str = ""
    ANGEL_ONE_READ_RATE_LIMITS: str = ""
    # Official Angel One instrument master (scrip master) download URL. Empty =
    # no download is ever attempted; set it only to the URL Angel One documents.
    ANGEL_ONE_SCRIP_MASTER_URL: str = ""

    # Groww Trading API Child support. Default false = Groww accounts cannot be
    # added, logged in or traded and no request is ever sent to api.groww.in.
    # Groww can never be a Master. Live verification (scripts/groww_capture.py)
    # is still required before enabling this in production.
    GROWW_ENABLED: bool = False
    # Second, independent gate. GROWW_ENABLED=true alone only allows an account to be
    # added, its credentials stored and an explicit login; a Groww account can be a
    # strategy Child (and so receive copied orders) only when this is ALSO true.
    GROWW_CHILD_TRADING_ENABLED: bool = False
    # Third gate: may a Groww account be a strategy MASTER (its orders detected and copied)?
    # Even when true it has no effect until GrowwContract.master_verified is set in code, which
    # needs the owner's captures (order-list paging, stable order ids, part-fill vocabulary).
    GROWW_MASTER_ENABLED: bool = False
    # Groww's instrument master (CSV). Empty = never downloaded; set it to the URL
    # in Groww's instruments documentation, or load a downloaded file instead.
    GROWW_INSTRUMENT_MASTER_URL: str = ""

    # Zerodha Kite Connect support, built from the official v3 documentation only (nothing observed live). Default false
    # = Zerodha accounts cannot be added, logged in or traded and no request is ever sent to api.kite.trade.
    ZERODHA_ENABLED: bool = False
    # Independent gates, as for Groww: ENABLED alone allows setup and the interactive login only.
    ZERODHA_CHILD_TRADING_ENABLED: bool = False
    # Has no effect until ZerodhaContract.master_verified is set in code: Zerodha's pages do not say whether orders placed
    # outside the API key appear in GET /orders, so that needs a supervised capture.
    ZERODHA_MASTER_ENABLED: bool = False

    # VPS public IP — must be whitelisted on Alice Blue Developer Portal for each account's App.
    # All API/WebSocket calls to Alice Blue originate from this IP.
    STATIC_VPS_IP: str = ""

    # Master MARKET order execution strategy. Alice Blue rejects MARKET
    # orders placed through the API/vendor channel with EC965 "Market
    # orders are not allowed" (confirmed: the same account places MARKET
    # orders successfully via Alice Blue's own Web/Mobile app - this is a
    # broker/vendor-channel restriction, not a payload bug). Default
    # "MARKETABLE_LIMIT" converts a confirmed master MARKET fill into an
    # aggressive LIMIT order per follower (see CopyEnginePipeline.
    # _process_single_market_follower), which the same API accepts. Any
    # other value (e.g. "DIRECT_MARKET") preserves the original direct-
    # MARKET behavior unchanged - kept as an explicit rollback switch, not
    # because that path currently works against Alice Blue.
    MARKET_ORDER_EXECUTION_MODE: str = "MARKETABLE_LIMIT"

    # Protective slippage buffer applied to the master's confirmed fill
    # price when constructing the marketable LIMIT price: BUY submits at
    # price*(1+buffer/100), SELL at price*(1-buffer/100). Kept as a plain
    # percentage (not hardcoded) so it can be tuned without a code change.
    MARKET_ORDER_BUFFER_PERCENT: Decimal = Decimal("0")

    # How often MasterOrderPoller re-reads the master's broker order book.
    # This is the ONLY mechanism proven to observe orders placed from Alice
    # Blue's own Mobile app / Web terminal - the NorenWS socket delivers no
    # order frames for them (see master_order_poller.py's module docstring).
    # One GET per interval for a single account; 5s = 12 requests/minute,
    # comfortably above the 1/minute/account FollowerFillSyncer already
    # sustains, but Alice Blue's documented rate limit for
    # /open-api/od/v1/orders/book has NOT been confirmed - lower this if the
    # broker starts throttling. The interval is also the worst-case delay
    # before an external master order is copied, and determines whether a
    # fast-filling external LIMIT order is still seen OPEN (replicated as a
    # resting LIMIT) or already COMPLETE (replicated as a marketable LIMIT).
    MASTER_ORDER_POLL_INTERVAL_SECONDS: int = 5

    # Trading-day boundary for reporting (Demat card P&L and daily order
    # counts): the trading day starts at 00:00 in this zone. NSE/BSE trade on
    # India time, and Alice Blue's exchangeTimestamp values are exchange-local
    # time in this same zone.
    TRADING_TIMEZONE: str = "Asia/Kolkata"

    # How long the read-only broker books used for P&L reporting (order book,
    # position book) are cached in Redis per account, so the cards' 15s
    # polling adds at most one read of each book per account per TTL on top
    # of MasterOrderPoller / FollowerFillSyncer.
    PNL_BROKER_CACHE_TTL_SECONDS: int = 10


PLACEHOLDER_MARKER = "CHANGE_ME"


def find_placeholder_secrets(cfg: "Settings") -> list[str]:
    """Names of security-critical settings that still hold the `.env.example`
    placeholder (CHANGE_ME...). Real secrets come from scripts/bootstrap-env.sh;
    a placeholder is publicly known, so it must never run a trading engine."""
    candidates = {
        "INTERNAL_API_TOKEN": cfg.INTERNAL_API_TOKEN,
        "ALICE_BLUE_ENCRYPTION_KEY": cfg.ALICE_BLUE_ENCRYPTION_KEY,
        "DATABASE_URL": cfg.DATABASE_URL,
        "REDIS_URL": cfg.REDIS_URL,
    }
    return [name for name, value in candidates.items() if value and PLACEHOLDER_MARKER in str(value).upper()]


settings = Settings()
