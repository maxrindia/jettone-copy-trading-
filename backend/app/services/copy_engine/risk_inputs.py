"""Real data inputs for RiskEngine checks 7 (daily loss) and 8 (open exposure).

Both are approximations until actual fill prices are captured post-trade:
- Daily loss: net signed value of today's SUCCESS copy trades per follower,
  using estimated_price. TODO(fill-price): switch to actual fill prices
  recorded from the broker order book once captured.
- Open exposure: sum of |net qty| x average_price over live broker positions,
  cached in Redis for a short TTL to avoid hammering the broker API.
"""
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trades import CopyTrade, CopyTradeStatus, OrderSide
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.instruments import resolve_instrument_info
# Imported as a module (not the symbol) so runtime patching of
# app.core.database.AsyncSessionLocal (tests) is honored on every call.
from app.core import database as _database

logger = logging.getLogger("risk_inputs")

OPEN_EXPOSURE_CACHE_PREFIX = "risk:exposure:"
OPEN_EXPOSURE_CACHE_TTL_SEC = 10


async def get_daily_loss_today(
    follower_account_id,
    db: Optional[AsyncSession] = None,
) -> Decimal:
    """
    Approximate realized P&L for today as -1 * net signed traded value.
    A negative result means losses; RiskEngine compares against max_daily_loss.
    TODO(fill-price): replace estimated_price with actual fill price.
    """
    owns_session = db is None
    if db is None:
        async with _database.AsyncSessionLocal() as session:
            return await get_daily_loss_today(follower_account_id, session)

    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(CopyTrade).where(
        CopyTrade.follower_account_id == follower_account_id,
        CopyTrade.status == CopyTradeStatus.SUCCESS,
        CopyTrade.completed_at >= start_of_day,
    )
    result = await db.execute(stmt)
    trades = result.scalars().all()

    net = Decimal("0")
    for t in trades:
        value = Decimal(str(t.quantity)) * Decimal(str(t.estimated_price or "0"))
        net += value if t.side == OrderSide.BUY else -value

    # Losses are expressed as positive exposure against the daily-loss limit:
    # a follower that bought more than it sold today has deployed capital at risk.
    loss = net if net > Decimal("0") else Decimal("0")
    return loss


async def get_open_exposure(client: Optional[AliceBlueClient], redis=None) -> Decimal:
    """
    Current open exposure from the broker position book, Redis-cached.
    Returns Decimal("0") when no client is available (e.g. tests/mock flows).
    """
    if client is None:
        return Decimal("0")

    cache_key = f"{OPEN_EXPOSURE_CACHE_PREFIX}{client.account_id}"
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached:
                return Decimal(str(json.loads(cached)))
        except Exception as exc:
            logger.warning(f"Exposure cache read failed: {exc}")

    try:
        positions = await client.get_positions()
    except Exception as exc:
        logger.error(f"Open exposure fetch failed for {client.account_id}: {exc}")
        raise

    exposure = sum(
        (Decimal(abs(p.quantity)) * p.average_price for p in positions),
        Decimal("0"),
    )

    if redis is not None:
        try:
            await redis.set(cache_key, str(exposure), ex=OPEN_EXPOSURE_CACHE_TTL_SEC)
        except Exception as exc:
            logger.warning(f"Exposure cache write failed: {exc}")

    return exposure


# ── Follower SELL position safety ─────────────────────────────────────────
# Production incident this exists for: a master BUY was replicated to a
# follower whose child BUY never filled (stale LIMIT), leaving the follower
# flat. The master then squared off with a SELL, which was replicated
# normally - and FILLED - putting the follower SHORT a stock it never owned
# (CANBK, 2026-09-22, follower held -1 for ~38 minutes). Nothing in the copy
# engine consulted the follower's actual holdings before selling. Alice Blue
# blocked one such SELL ("Intraday Direct Sell not allowed") and allowed
# another on the same day, so the broker's own guard is not dependable.


class PositionSafetyError(Exception):
    """
    Base for every condition under which follower position cannot be
    established with confidence. Each subclass carries the reason_code the
    pipeline records in AuditLog/SystemEvent, so call sites never have to
    map exception types to strings themselves.

    Every one of these means "reject the SELL" - never "assume zero" and
    never "assume it is fine". A zero position and an unavailable position
    look identical downstream if this distinction is lost, which is exactly
    the failure mode this whole module guards against.
    """

    reason_code = "POSITION_SAFETY_FAILED"


class PositionFetchError(PositionSafetyError):
    reason_code = "POSITION_FETCH_FAILED"


class UnknownProductTypeError(PositionSafetyError):
    reason_code = "UNKNOWN_PRODUCT_TYPE"


# Broker product aliases -> Jettone's canonical product bucket. Mirrors the
# established mapping already used by AliceBlueClient's square-off product
# parse, with one deliberate difference: there is NO "anything else -> MIS"
# default here. For a safety gate an unrecognized product string must fail
# closed rather than be silently netted against a MIS position.
#
# Both spellings of each bucket are required because Alice Blue is not
# self-consistent across endpoints: the position book reports product "MIS"
# while the order book reports "INTRADAY" for that same position (verified
# live in production 2026-09-22), and place_order sends "INTRADAY".
_PRODUCT_ALIASES: Dict[str, str] = {
    "MIS": "MIS",
    "INTRADAY": "MIS",
    "CNC": "CNC",
    "LONGTERM": "CNC",
    "DELIVERY": "CNC",
    "NRML": "NRML",
    "MTF": "NRML",
    "CARRYFORWARD": "NRML",
    "MARGIN": "NRML",
}


def normalize_product_type(raw: Any) -> str:
    """Canonical product bucket, or UnknownProductTypeError. Never guesses."""
    key = str(raw or "").upper().strip()
    normalized = _PRODUCT_ALIASES.get(key)
    if normalized is None:
        raise UnknownProductTypeError(
            f"Unrecognized product type {raw!r} - cannot establish a trustworthy position."
        )
    return normalized


# Derivative-only exchanges. A carry-forward (non-intraday) derivative order is
# placed with product LONGTERM (Jettone's CNC bucket) but Alice Blue reports the
# resulting position as NRML - confirmed live 2026-10-14 for one NFO NIFTY option:
# order book `product: LONGTERM`, position book `product: NRML`, same instrumentId.
# There is no delivery on these exchanges, so CNC and NRML are the same holding
# there. Intraday (MIS) stays separate everywhere, and equity exchanges keep the
# strict CNC/NRML/MIS separation.
DERIVATIVE_EXCHANGES = frozenset({"NFO", "BFO", "MCX", "CDS"})


def normalize_product_for_exchange(raw: Any, exchange: Any) -> str:
    """normalize_product_type(), with CNC folded into NRML on derivative
    exchanges only. Unknown products still raise (fail closed)."""
    bucket = normalize_product_type(raw)
    if bucket == "CNC" and str(exchange or "").upper().strip() in DERIVATIVE_EXCHANGES:
        return "NRML"
    return bucket


async def get_available_long(
    client: Optional[AliceBlueClient],
    exchange: str,
    symbol: str,
    product_type: Any,
    provided_token: Optional[str] = None,
) -> int:
    """
    The follower's broker-confirmed LONG quantity for ONE instrument and ONE
    product, as reported by the Alice Blue position book. Returns 0 for a
    flat or net-short position; raises PositionSafetyError (or
    InstrumentResolutionError) for anything that cannot be determined with
    confidence, so the caller can fail closed.

    provided_token: the master event's own broker-provided instrument id
    (MasterTradeEvent.instrument_id), when known. Passed straight through to
    resolve_instrument_info, which already treats an explicit token as
    authoritative and skips symbol-based lookup entirely. This matters most
    for NFO options/futures, where a bare underlying name like "NIFTY" is
    ambiguous - many strikes/expiries share it - and symbol-only resolution
    can silently land on the wrong contract (confirmed in production:
    "NIFTY" resolved to a cached NIFTY26JUN29P33000 while the master was
    actually trading NIFTY 29th SEP 23900 CE).

    The broker position book is the ONLY source consulted. Jettone's own
    Order ledger is deliberately NOT used, not even as a fallback: it cannot
    see externally-placed follower orders (two such CANBK buys were found in
    production that Jettone had no rows for), it is not day-scoped so it
    accumulates across sessions while the broker flattens MIS daily, and its
    symbol keys are split by the -EQ suffix. Measured against the live
    broker book on 2026-09-22 it was wrong for every single symbol.

    Instrument identity, in priority order:

    1. PRIMARY - numeric instrument token, when BOTH the resolved query
       token AND a given position row's own instrument_id are available.
       Confirmed live (2026-09-23): for one real NFO option, the order book
       reports tradingSymbol "NIFTY" while the position book reports
       "NIFTY29SEP26C23900" for the SAME contract - different strings, same
       instrument - but instrumentId ("74056") is identical and stable
       across both endpoints. Symbol-only matching would silently reject a
       real, coverable SELL for any instrument whose position-book spelling
       differs from its order-book spelling, which options/futures do by
       design (many strikes/expiries share one bare underlying name).
    2. FALLBACK - resolve_instrument_info's canonical symbol, or the
       caller's own spelling (both upper-cased), used only when either side
       lacks a token - this is the ENTIRE original matching behavior,
       unchanged, and is what every existing equity case still uses today
       (equities happen to also carry an instrument_id now, but nothing
       breaks if a row or a resolution ever doesn't have one).

    Whichever identity matched, the row must ALSO match on exchange and on
    normalize_product_type() exactly - token identity alone is deliberately
    never sufficient. The same instrument in a different product must never
    count: e.g. NIFTY token 74056 held under NRML must never license a SELL
    requested under MIS or CNC for that same token. This is what keeps
    MIS/CNC/NRML holdings in the same instrument from ever being combined,
    exactly as before - only the SYMBOL-vs-TOKEN identity check changed.

    An unrecognized product string on a row that otherwise matches this
    instrument raises rather than being skipped: we cannot tell whether it
    represents a long in the product being sold. Unrecognized products on
    rows for OTHER instruments are irrelevant and are ignored, so one
    exotic unrelated holding cannot block every SELL.
    """
    if client is None:
        raise PositionFetchError("No broker client available for this follower.")

    # Raises InstrumentResolutionError when the instrument cannot be
    # identified at all - propagated deliberately; the caller maps it to
    # INSTRUMENT_RESOLUTION_FAILED and rejects.
    #
    # A client class that maps instruments itself (Angel One) resolves them with
    # its own broker's instrument master and never sees provided_token - that id
    # belongs to the MASTER's broker and means nothing to this one. Looked up on
    # the class so a mock client can never masquerade as one; Alice Blue's client
    # has no such method, so its resolution is exactly as before.
    if getattr(type(client), "resolve_instrument", None) is not None:
        _token, canonical_symbol = await client.resolve_instrument(exchange, symbol)
    else:
        _token, canonical_symbol = await resolve_instrument_info(
            client.redis, exchange, symbol, provided_token=provided_token,
        )

    clean_exchange = str(exchange or "NSE").upper().strip()
    target_product = normalize_product_for_exchange(product_type, clean_exchange)
    accepted_symbols = {
        str(canonical_symbol or "").upper().strip(),
        str(symbol or "").upper().strip(),
    }
    accepted_symbols.discard("")
    query_token = str(_token or "").strip()

    # get_positions() itself never raises - it swallows network errors and
    # non-200 responses into an empty list, matching "no positions" (a
    # long-standing, still-relied-upon contract for its OTHER callers). That
    # is exactly the ambiguity this safety gate cannot tolerate: an empty
    # list from THAT method could mean "genuinely flat" or "couldn't check
    # at all". get_positions_or_none() shares the identical fetch/parse path
    # but returns None specifically for the fetch-failed case, so a fetch
    # failure is never silently read as "follower holds 0".
    try:
        positions = await client.get_positions_or_none()
    except Exception as exc:
        raise PositionFetchError(f"Broker position book fetch failed: {exc}") from exc

    if positions is None:
        raise PositionFetchError("Broker position book fetch failed (network/API error).")

    available = 0
    for pos in positions:
        # Exchange applies before either identity check, unconditionally.
        pos_exchange = str(getattr(pos, "exchange", "") or "").upper().strip()
        if pos_exchange and pos_exchange != clean_exchange:
            continue

        pos_instrument_id = str(getattr(pos, "instrument_id", "") or "").strip()
        if query_token and pos_instrument_id:
            # PRIMARY: token identity. A mismatch here is final for this row
            # - it must NOT then be given a second chance via symbol
            # matching, since that would reintroduce exactly the ambiguity
            # (a shared bare underlying name) this identity is meant to
            # resolve.
            if pos_instrument_id != query_token:
                continue
        else:
            # FALLBACK: original symbol-based matching, unchanged, used
            # only when either side lacks a token.
            pos_symbol = str(getattr(pos, "symbol", "") or "").upper().strip()
            if pos_symbol not in accepted_symbols:
                continue

        # Product match is required regardless of which identity matched -
        # the same instrument in a different product must never count.
        # Raises for an unrecognized product on a row for THIS instrument.
        # On derivative exchanges CNC/LONGTERM and NRML are one holding (see
        # DERIVATIVE_EXCHANGES); MIS is always separate.
        if normalize_product_for_exchange(getattr(pos, "product_type", None), pos_exchange or clean_exchange) != target_product:
            continue
        available += int(getattr(pos, "quantity", 0) or 0)

    # A net-short position offers nothing to sell against; it must never
    # license a further SELL that would deepen the short.
    return max(0, available)
