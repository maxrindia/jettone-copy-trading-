import csv
import io
import logging
from typing import Dict, List, Optional, Tuple
import httpx
import redis.asyncio as aioredis

logger = logging.getLogger("alice_blue_instruments")


class InstrumentResolutionError(Exception):
    """Raised when resolve_instrument_info cannot find a real numeric
    Alice Blue instrumentId/token for (exchange, symbol) anywhere (explicit
    token, Redis cache, or the static KNOWN_INSTRUMENTS table).

    Production root cause this exists for: the function used to fall back
    to `return clean_sym, trading_sym` - handing back the SYMBOL STRING
    ITSELF (e.g. "NIFTY") as if it were the numeric token - which Alice
    Blue then rejected with "Invalid 'instrumentId'. It must contain only
    numeric characters." Failing closed here, instead of returning a value
    that looks superficially like a token, means the caller sees a clear
    Jettone-level error instead of a confusing broker rejection three
    layers downstream.
    """

# Pre-cached / common symbol to instrument token and trading symbol mappings for NSE Equities, Options, and Futures
KNOWN_INSTRUMENTS: Dict[Tuple[str, str], Tuple[str, str]] = {
    # NSE Equities
    ("NSE", "RELIANCE"): ("2885", "RELIANCE-EQ"),
    ("NSE", "RELIANCE-EQ"): ("2885", "RELIANCE-EQ"),
    ("NSE", "YESBANK"): ("11915", "YESBANK-EQ"),
    ("NSE", "YESBANK-EQ"): ("11915", "YESBANK-EQ"),
    ("NSE", "INFY"): ("1594", "INFY-EQ"),
    ("NSE", "INFY-EQ"): ("1594", "INFY-EQ"),
    ("NSE", "IDEA"): ("14366", "IDEA-EQ"),
    ("NSE", "IDEA-EQ"): ("14366", "IDEA-EQ"),
    ("NSE", "SBIN"): ("3045", "SBIN-EQ"),
    ("NSE", "SBIN-EQ"): ("3045", "SBIN-EQ"),
    ("NSE", "TCS"): ("11536", "TCS-EQ"),
    ("NSE", "TCS-EQ"): ("11536", "TCS-EQ"),
    ("NSE", "HDFCBANK"): ("1333", "HDFCBANK-EQ"),
    ("NSE", "HDFCBANK-EQ"): ("1333", "HDFCBANK-EQ"),
    ("NSE", "ICICIBANK"): ("4963", "ICICIBANK-EQ"),
    ("NSE", "ICICIBANK-EQ"): ("4963", "ICICIBANK-EQ"),
    ("NSE", "TATAMOTORS"): ("3456", "TATAMOTORS-EQ"),
    ("NSE", "TATAMOTORS-EQ"): ("3456", "TATAMOTORS-EQ"),
    ("NSE", "ITC"): ("1660", "ITC-EQ"),
    ("NSE", "ITC-EQ"): ("1660", "ITC-EQ"),
    ("NSE", "WIPRO"): ("3787", "WIPRO-EQ"),
    ("NSE", "WIPRO-EQ"): ("3787", "WIPRO-EQ"),
    ("NSE", "TATASTEEL"): ("3499", "TATASTEEL-EQ"),
    ("NSE", "TATASTEEL-EQ"): ("3499", "TATASTEEL-EQ"),
    ("NSE", "NIFTY"): ("26000", "NIFTY"),
    ("NSE", "BANKNIFTY"): ("26009", "BANKNIFTY"),
    # BSE Equities
    ("BSE", "RELIANCE"): ("500325", "RELIANCE"),
    ("BSE", "YESBANK"): ("532648", "YESBANK"),
    ("BSE", "SBIN"): ("500112", "SBIN"),
    ("BSE", "TCS"): ("532540", "TCS"),
    ("BSE", "INFY"): ("500209", "INFY"),
    # NFO Index & Stock Options / Futures (CE, PE, FUT)
    ("NFO", "NIFTY FUT"): ("45001", "NIFTY-FUT"),
    ("NFO", "NIFTY 24500 CE"): ("45100", "NIFTY 24500 CE"),
    ("NFO", "NIFTY 24500 PE"): ("45101", "NIFTY 24500 PE"),
    ("NFO", "NIFTY 24600 CE"): ("45102", "NIFTY 24600 CE"),
    ("NFO", "NIFTY 24600 PE"): ("45103", "NIFTY 24600 PE"),
    ("NFO", "NIFTY 24700 CE"): ("45104", "NIFTY 24700 CE"),
    ("NFO", "NIFTY 24700 PE"): ("45105", "NIFTY 24700 PE"),
    ("NFO", "BANKNIFTY FUT"): ("46001", "BANKNIFTY-FUT"),
    ("NFO", "BANKNIFTY 51500 CE"): ("46100", "BANKNIFTY 51500 CE"),
    ("NFO", "BANKNIFTY 51500 PE"): ("46101", "BANKNIFTY 51500 PE"),
    ("NFO", "BANKNIFTY 52000 CE"): ("46102", "BANKNIFTY 52000 CE"),
    ("NFO", "BANKNIFTY 52000 PE"): ("46103", "BANKNIFTY 52000 PE"),
    ("NFO", "RELIANCE FUT"): ("47001", "RELIANCE-FUT"),
    ("NFO", "RELIANCE 3000 CE"): ("47100", "RELIANCE 3000 CE"),
    ("NFO", "RELIANCE 3000 PE"): ("47101", "RELIANCE 3000 PE"),
}


async def resolve_instrument_info(
    redis: Optional[aioredis.Redis],
    exchange: str,
    symbol: str,
    provided_token: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Resolves symbol and exchange into (numeric_token, trading_symbol).
    e.g. ('NSE', 'RELIANCE') -> ('2885', 'RELIANCE-EQ')
    """
    clean_ex = exchange.upper().strip() if exchange else "NSE"
    clean_sym = symbol.upper().strip() if symbol else ""

    # Parse and extract from formats like 'RELIANCE (NSE)' or 'RELIANCE (BSE)'
    if "(" in clean_sym and clean_sym.endswith(")"):
        parts = clean_sym.rstrip(")").split("(")
        clean_sym = parts[0].strip()
        if len(parts) > 1 and parts[1].strip():
            clean_ex = parts[1].strip()

    # Normalize default trading_symbol
    trading_sym = clean_sym
    if clean_ex == "NSE" and not clean_sym.endswith("-EQ") and not any(clean_sym.startswith(x) for x in ("NIFTY", "BANKNIFTY", "FINNIFTY")):
        trading_sym = f"{clean_sym}-EQ"

    # 1. If token provided directly
    if provided_token and str(provided_token).strip():
        token_str = str(provided_token).strip()
        return token_str, trading_sym

    # 2. Check Redis cache
    if redis:
        try:
            cache_key = f"alice:instrument:{clean_ex}:{clean_sym}"
            cached = await redis.get(cache_key)
            if cached:
                token_val = cached.decode("utf-8") if isinstance(cached, bytes) else str(cached)
                if ":" in token_val:
                    tok, tr_sym = token_val.split(":", 1)
                    return tok.strip(), tr_sym.strip()
                elif token_val.strip():
                    return token_val.strip(), trading_sym
        except Exception as err:
            logger.warning(f"Redis instrument lookup failed for {clean_ex}:{clean_sym}: {err}")

    # 3. Check static known instruments
    known = KNOWN_INSTRUMENTS.get((clean_ex, clean_sym))
    if known:
        return known[0], known[1]

    if clean_ex == "NSE" and not clean_sym.endswith("-EQ"):
        known_eq = KNOWN_INSTRUMENTS.get(("NSE", f"{clean_sym}-EQ"))
        if known_eq:
            return known_eq[0], known_eq[1]

    # Nothing resolved: explicit token, Redis cache, and the static table
    # all missed. Fail closed instead of returning clean_sym itself (the
    # production NFO instrumentId bug - see InstrumentResolutionError).
    raise InstrumentResolutionError(
        f"Unable to resolve broker instrument ID for {clean_ex} instrument '{clean_sym}' - "
        "not found in Redis cache or the known-instruments table. Run POST /broker/instruments/sync "
        f"for {clean_ex} if this is a real, currently-tradable contract."
    )


async def resolve_instrument_id(
    redis: Optional[aioredis.Redis],
    exchange: str,
    symbol: str,
    provided_token: Optional[str] = None,
) -> str:
    """
    Resolves symbol and exchange into a numeric Alice Blue instrumentId / token.
    """
    token, _ = await resolve_instrument_info(redis, exchange, symbol, provided_token)
    return token


async def load_and_cache_contract_master(redis: Optional[aioredis.Redis], exchange: str = "NSE") -> int:
    """
    Fetches official Alice Blue contract master CSV and caches all instruments into Redis.
    """
    url = f"https://v2api.aliceblueonline.com/restpy/static/contract_master/{exchange.upper()}.csv"
    logger.info(f"Downloading Alice Blue {exchange} contract master from {url}...")
    
    count = 0
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.error(f"Failed to download contract master: HTTP {resp.status_code}")
                return 0

            # OBSERVE foundation: also retain lot size / expiry / strike from the same response text. Guarded so a terms
            # problem can never affect the symbol->token cache below.
            try:
                from app.services.instruments.terms import store as terms_store
                terms_store.load_alice(exchange, resp.text)
            except Exception as terms_err:
                logger.warning(f"Instrument terms not loaded for {exchange}: {type(terms_err).__name__}")

            reader = csv.DictReader(io.StringIO(resp.text))
            for row in reader:
                sym = row.get("Symbol", "").strip().upper()
                token = row.get("Token", "").strip()
                tr_sym = row.get("Trading Symbol", "").strip()
                
                if sym and token:
                    count += 1
                    # Cache in memory
                    KNOWN_INSTRUMENTS[(exchange.upper(), sym)] = (token, tr_sym or f"{sym}-EQ")
                    if tr_sym:
                        KNOWN_INSTRUMENTS[(exchange.upper(), tr_sym.upper())] = (token, tr_sym)

                    # Cache in Redis with 24h TTL
                    if redis:
                        try:
                            await redis.set(
                                f"alice:instrument:{exchange.upper()}:{sym}",
                                f"{token}:{tr_sym or sym}",
                                ex=86400,
                            )
                        except Exception:
                            pass

            logger.info(f"Successfully loaded and cached {count} instruments for {exchange}.")
    except Exception as e:
        logger.error(f"Error loading Alice Blue contract master: {e}")

    return count

