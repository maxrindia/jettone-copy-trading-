"""
An Alice Blue Child of another broker's Master: its instrument comes ONLY from an exact row of Alice's own contract master.

Offline: contract-master text in the committed column format (tests/test_instrument_terms.py), an in-memory TermsStore, mocked Child
clients and the `no_network` guard. Nothing here calls a broker. The per-symbol Redis cache and the static KNOWN_INSTRUMENTS table are
never consulted on this path: a bare NFO Symbol is the underlying (every strike and expiry share one key) and a bare NSE Symbol spans
every series group, so neither is an identity.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.alice_blue import instruments as alice_instruments
from app.services.copy_engine import pipeline as _pipeline  # noqa: F401  (import before planner: planner <-> copy_engine cycle)
from app.services.instruments import terms
from app.services.instruments.contracts import CanonicalContract, ResolutionVerdict as R, make_contract, resolve_for_broker
from app.services.instruments.planner import (
    ALICE_UNRESOLVED_PREFIX, INSTRUMENT_UNMAPPED, alice_cross_broker_token, alice_unresolved_skip,
)
from test_multi_broker_fanout import (  # noqa: F401  (fixtures are used by name)
    all_pairs_open_for_test, build_strategy, copies_of, entry, no_network, pipeline,
)
from test_order_handling_matrix import _event
from test_strategy_master_switching import _child_client

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)
EXPIRY = date(2026, 11, 26)
NFO_HEADER = ("Exch,Exchange Segment,Symbol,Token,Instrument Type,Option Type,Strike Price,Instrument Name,Formatted Ins Name,"
              "Trading Symbol,Expiry Date,Lot Size,Tick Size")
NSE_HEADER = ("Exch,Exchange Segment,Group Name,Symbol,Token,Instrument Type,Instrument Name,Formatted Ins Name,Trading Symbol,"
              "Lot Size,Tick Size")
NFO_ROWS = [
    "NFO,nse_fo,NIFTY,50001,OPTIDX,CE,23500,NIFTY,NIFTY NOV 23500 CE,NIFTY26NOV26C23500,2026-11-26,75,0.05",
    "NFO,nse_fo,NIFTY,50002,OPTIDX,CE,23500,NIFTY,NIFTY DEC 23500 CE,NIFTY31DEC26C23500,2026-12-31,75,0.05",
    "NFO,nse_fo,NIFTY,50003,OPTIDX,PE,23500,NIFTY,NIFTY NOV 23500 PE,NIFTY26NOV26P23500,2026-11-26,75,0.05",
    "NFO,nse_fo,NIFTY,50004,OPTIDX,CE,23600,NIFTY,NIFTY NOV 23600 CE,NIFTY26NOV26C23600,2026-11-26,75,0.05",
    "NFO,nse_fo,NIFTY,50005,FUTIDX,XX,-1,NIFTY,NIFTY NOV FUT,NIFTY26NOV26F,2026-11-26,75,0.05",
    "NFO,nse_fo,BANKNIFTY,60001,OPTIDX,CE,51500,BANKNIFTY,BANKNIFTY NOV 51500 CE,BANKNIFTY26NOV26C51500,2026-11-26,35,0.05",
    "NFO,nse_fo,BANKNIFTY,60002,OPTIDX,CE,51500,BANKNIFTY,BANKNIFTY NOV 51500 CE,BANKNIFTY26NOV26C51500X,2026-11-26,35,0.05",
    "NFO,nse_fo,FINNIFTY,70001,OPTIDX,CE,23000,FINNIFTY,FINNIFTY NOV 23000 CE,FINNIFTY26NOV26C23000,2026-11-26,abc,0.05",
]
NSE_ROWS = [
    "NSE,nse_cm,EQ,SBIN,3045,0,STATE BANK OF INDIA,SBIN-EQ,SBIN-EQ,1,0.05",
    "NSE,nse_cm,SM,GRAPHISAD,20454,0,GRAPHISADS LIMITED,GRAPHISAD-SM,GRAPHISAD-SM,1200,0.05",
    "NSE,nse_cm,EQ,DUPCO,90001,0,DUP COMPANY,DUPCO-EQ,DUPCO-EQ,1,0.05",
    "NSE,nse_cm,EQ,DUPCO,90002,0,DUP COMPANY,DUPCO-EQ,DUPCO-EQ,1,0.05",
    "NSE,nse_cm,EQ,BADLOT,90003,0,BAD LOT,BADLOT-EQ,BADLOT-EQ,x,0.05",
]


def store_with(*, loaded_at=NOW, nfo=True, nse=True):
    s = terms.TermsStore()
    if nfo:
        s.load_alice("NFO", "\n".join([NFO_HEADER] + NFO_ROWS) + "\n", loaded_at=loaded_at)
    if nse:
        s.load_alice("NSE", "\n".join([NSE_HEADER] + NSE_ROWS) + "\n", loaded_at=loaded_at)
    return s


def alice(contract, store=None, now=NOW):
    return resolve_for_broker("alicebluenew", contract, now=now, alice_store=store if store is not None else store_with())


def option(kind="CE", strike="23500", expiry=EXPIRY, underlying="NIFTY"):
    return make_contract(kind, "NFO", underlying, expiry, strike)


# ── exact derivative identity ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_exact_option_resolves_to_alices_own_row_and_stays_unverified():
    got = alice(option())
    assert got.verdict is R.RESOLVED_UNVERIFIED and got.broker_token == "50001" and got.broker_symbol == "NIFTY26NOV26C23500"
    assert got.lot_size == 75 and got.verified is False                     # a derivative mapping is never "verified" without live evidence


def test_an_exact_future_resolves():
    got = alice(make_contract("FUT", "NFO", "NIFTY", EXPIRY))
    assert got.verdict is R.RESOLVED_UNVERIFIED and got.broker_token == "50005"


@pytest.mark.parametrize("contract,why", [
    (option(expiry=date(2026, 11, 27)), "wrong expiry"),
    (option(strike="23550"), "wrong strike"),
    (option(kind="PE", strike="23600"), "wrong option type (only a CE exists at 23600)"),
    (make_contract("FUT", "NFO", "NIFTY", date(2026, 12, 31)), "no future for that expiry"),
    (option(underlying="NIFTYNXT50"), "unknown underlying"),
])
def test_a_near_miss_is_unmapped_never_the_closest_contract(contract, why):
    got = alice(contract)
    assert got.verdict is R.UNMAPPED and got.broker_token is None, why


def test_two_exact_rows_are_ambiguous():
    got = alice(option(underlying="BANKNIFTY", strike="51500"))
    assert got.verdict is R.AMBIGUOUS and got.broker_token is None


def test_a_matching_row_with_invalid_metadata_is_refused():
    got = alice(option(underlying="FINNIFTY", strike="23000"))
    assert got.verdict is R.UNMAPPED and "invalid metadata" in got.reason and got.broker_token is None


def test_a_stale_or_unloaded_master_never_maps():
    assert alice(option(), store_with(loaded_at=NOW - timedelta(days=2))).verdict is R.STALE
    assert alice(option(), terms.TermsStore()).verdict is R.CATALOG_NOT_LOADED


def test_an_unknown_or_non_nfo_derivative_exchange_is_invalid_input():
    assert make_contract("CE", "MCX", "CRUDEOIL", EXPIRY, "6000") is None
    got = alice(CanonicalContract("CE", "MCX", "CRUDEOIL", EXPIRY, Decimal("6000")))
    assert got.verdict is R.INVALID_INPUT and got.broker_token is None


def test_the_static_table_and_the_bare_underlying_key_are_never_used(monkeypatch):
    # The static table names a NIFTY 24500 CE with a hand-written token, and a bare "NIFTY" key would point at some contract;
    # neither exists in this master, so the resolver refuses instead of returning either token.
    monkeypatch.setitem(alice_instruments.KNOWN_INSTRUMENTS, ("NFO", "NIFTY"), ("99999", "NIFTY-WHATEVER"))
    assert ("NFO", "NIFTY 24500 CE") in alice_instruments.KNOWN_INSTRUMENTS
    got = alice(option(strike="24500"))
    assert got.verdict is R.UNMAPPED and got.broker_token is None


# ── exact equity identity ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_equity_resolves_only_to_the_eq_series_row():
    assert alice(make_contract("EQ", "NSE", "SBIN")).broker_token == "3045"
    sm_only = alice(make_contract("EQ", "NSE", "GRAPHISAD"))                  # listed only in the SM group (lot 1200)
    assert sm_only.verdict is R.UNMAPPED and sm_only.broker_token is None
    assert alice(make_contract("EQ", "NSE", "DUPCO")).verdict is R.AMBIGUOUS
    assert alice(make_contract("EQ", "NSE", "BADLOT")).verdict is R.UNMAPPED


def test_bse_is_not_supported_because_its_columns_are_unverified():
    assert alice(make_contract("EQ", "BSE", "SBIN")).verdict is R.NOT_SUPPORTED


# ── the token handed to an Alice Child of another broker ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("symbol", ["SBIN", "SBIN-EQ", " sbin "])
def test_the_child_gets_alices_own_token_whatever_the_masters_spelling(symbol):
    assert alice_cross_broker_token(exchange="NSE", symbol=symbol, now=NOW, store=store_with()) == "3045"


@pytest.mark.parametrize("exchange,symbol,reason", [
    ("NFO", "NIFTY26NOV26C23500", "CROSS_BROKER_DERIVATIVE:NFO"),
    ("MCX", "CRUDEOIL", "CROSS_BROKER_DERIVATIVE:MCX"),
    ("", "SBIN", "CROSS_BROKER_DERIVATIVE:MISSING_EXCHANGE"),
    ("NSE", "GRAPHISAD", "ALICE_EQUITY_UNMAPPED"),
    ("NSE", "DUPCO", "ALICE_EQUITY_AMBIGUOUS"),
    ("NSE", "", "ALICE_EQUITY_INVALID_SYMBOL"),
    ("BSE", "SBIN", "ALICE_EQUITY_NOT_SUPPORTED"),
])
def test_anything_not_exactly_resolved_becomes_a_non_numeric_marker(exchange, symbol, reason):
    marker = alice_cross_broker_token(exchange=exchange, symbol=symbol, now=NOW, store=store_with())
    assert marker.startswith(ALICE_UNRESOLVED_PREFIX + reason) and not marker.isdigit()
    skip = alice_unresolved_skip(marker)
    assert skip.code == INSTRUMENT_UNMAPPED and skip.detail.startswith(reason)


def test_missing_and_stale_masters_become_markers():
    assert alice_cross_broker_token(exchange="NSE", symbol="SBIN", now=NOW, store=terms.TermsStore()).startswith(
        ALICE_UNRESOLVED_PREFIX + "ALICE_EQUITY_CATALOG_NOT_LOADED")
    assert alice_cross_broker_token(exchange="NSE", symbol="SBIN", now=NOW, store=store_with(loaded_at=NOW - timedelta(days=2))
                                    ).startswith(ALICE_UNRESOLVED_PREFIX + "ALICE_EQUITY_STALE")


def test_only_markers_are_skipped():
    assert alice_unresolved_skip("3045") is None and alice_unresolved_skip(None) is None and alice_unresolved_skip("12345") is None


@pytest.mark.asyncio
async def test_a_marker_that_reached_the_alice_client_would_still_be_refused_there():
    """Defence in depth: AliceBlueClient uses a provided id as is and then refuses a non-numeric one before building a request."""
    marker = ALICE_UNRESOLVED_PREFIX + "ALICE_EQUITY_UNMAPPED:x"
    token, _ = await alice_instruments.resolve_instrument_info(redis=None, exchange="NSE", symbol="SBIN", provided_token=marker)
    assert token == marker and not token.isdigit()


# ── through the pipeline ──────────────────────────────────────────────────────────────────────────────────────────────────────

OTHER_MASTERS = ("groww", "zerodha", "angelone")


@pytest.fixture
def live_store(monkeypatch):
    def use(store):
        monkeypatch.setattr(terms, "store", store)
        return store
    return use


async def _copy(db_session, mock_redis, master_broker, child_broker, oid, symbol="SBIN"):
    master, children = await build_strategy(db_session, master_broker, [child_broker], f"ax-{oid}")
    child = children[0]
    client = _child_client("BRK_A")
    event = _event(master, oid, side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE", symbol=symbol, qty=10,
                   price="800", token="12345")
    res = await pipeline(mock_redis).execute_replication(event=event, clients={str(child.id): client})
    return res["results"][str(child.id)], client, child


@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker", OTHER_MASTERS)
@pytest.mark.parametrize("symbol", ["SBIN", "SBIN-EQ"])
async def test_an_alice_child_gets_its_own_exact_token(db_session, mock_redis, all_pairs_open_for_test, live_store, master_broker, symbol):
    live_store(store_with(loaded_at=datetime.now(timezone.utc)))
    out, client, _ = await _copy(db_session, mock_redis, master_broker, "alicebluenew", f"AX-{master_broker}-{symbol}", symbol)
    assert out["status"] == "SUCCESS", out
    assert client.place_order.await_args.args[0].instrument_token == "3045"


@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker", OTHER_MASTERS)
@pytest.mark.parametrize("case,symbol,detail", [
    ("not loaded", "SBIN", "ALICE_EQUITY_CATALOG_NOT_LOADED"),
    ("stale", "SBIN", "ALICE_EQUITY_STALE"),
    ("non-EQ series only", "GRAPHISAD", "ALICE_EQUITY_UNMAPPED"),
    ("ambiguous", "DUPCO", "ALICE_EQUITY_AMBIGUOUS"),
])
async def test_an_unresolved_alice_child_is_skipped_and_nothing_is_sent(
        db_session, mock_redis, all_pairs_open_for_test, live_store, master_broker, case, symbol, detail):
    now = datetime.now(timezone.utc)
    live_store({"not loaded": terms.TermsStore(), "stale": store_with(loaded_at=now - timedelta(days=2))}.get(case)
               or store_with(loaded_at=now))
    out, client, child = await _copy(db_session, mock_redis, master_broker, "alicebluenew", f"AU-{master_broker}-{case}", symbol)
    assert out["status"] == "SKIPPED" and out["reason"] == INSTRUMENT_UNMAPPED and out["detail"].startswith(detail), out
    assert client.place_order.await_count == 0
    assert len(await copies_of(child)) == 1                                   # the skip is recorded for that Child


@pytest.mark.asyncio
async def test_alice_to_alice_is_unchanged_and_needs_no_contract_master(db_session, mock_redis, all_pairs_open_for_test, live_store):
    live_store(terms.TermsStore())                                            # nothing loaded: Alice -> Alice must not care
    out, client, _ = await _copy(db_session, mock_redis, "alicebluenew", "alicebluenew", "AA-SAME")
    assert out["status"] == "SUCCESS", out
    assert client.place_order.await_args.args[0].instrument_token == "12345"  # the Master's own token, as before
