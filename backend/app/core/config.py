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
    # Groww's instrument master (CSV). Empty = never downloaded; set it to the URL
    # in Groww's instruments documentation, or load a downloaded file instead.
    GROWW_INSTRUMENT_MASTER_URL: str = ""

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
