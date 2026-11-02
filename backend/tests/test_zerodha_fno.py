"""
Zerodha in the cross-broker F&O design: as a CHILD of an Alice Blue Master and as a MASTER whose F&O orders carry a canonical contract.
OFFLINE: fakes only, no broker contacted, nothing here is live evidence. Zerodha is disabled in production and stays behind every gate:
ZERODHA_ENABLED, ZERODHA_CHILD_TRADING_ENABLED, ALICE_ZERODHA_CANARY_ENABLED (or CANARY_PAIRS) and CROSS_BROKER_FNO_PAIRS.

Proven here:
  * an Alice Blue Master's F&O order is given a canonical contract from Alice's OWN contract master (by token), resolved in ZERODHA'S OWN
    dump to its exact tradingsymbol, and placed on NFO with the carry product NRML - never with Alice's token or Alice's symbol;
  * the Child is sized in whole lots (FIXED = N lots, MULTIPLIER = the Master's lots x the multiplier, rounded down) from Zerodha's lot size;
  * every refusal keeps the original skip (unlisted pair, no exact row, ambiguous row, unloaded catalog);
  * a Zerodha Master's NFO order gets the same contract key, only when ZERODHA_FNO_QUANTITY_UNIT is UNITS, and its status/quantity are untouched.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import CopySetting, QuantityMode
from app.models.trades import CopyTrade
from app.services.copy_engine import derivative_routing
from app.services.instruments import terms
from app.services.instruments.contracts import make_contract
from app.services.zerodha import instruments as zi
from app.services.zerodha.derivatives import with_contract_key
from app.services.alice_blue.types import OrderRecord, OrderSide
from conftest import TestingSessionLocal
from test_alice_cross_broker_resolution import store_with
from test_order_handling_matrix import _event
from test_zerodha_equity_flow import StatefulKite
from test_zerodha_equity_lifecycle import copy_to_zerodha, zerodha_child, zerodha_enabled  # noqa: F401
from zerodha_helpers import FakeKite, make_client, make_store, ok, seed_session

NIFTY_CE_TOKEN = "50001"            # NIFTY 26-Nov-2026 23500 CE in the Alice contract master fixture (lot 75)
ZERODHA_SYMBOL = "NIFTY26NOV23500CE"


@pytest.fixture
def fno_open(zerodha_enabled, monkeypatch):
    """The gates, a FRESH Alice contract master, Zerodha's own dump as the process store, and the pair."""
    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))
    store = make_store()
    monkeypatch.setattr(zi, "store", store)
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "alicebluenew>zerodha")
    return store


def alice_fno_event(master, oid, *, token=NIFTY_CE_TOKEN, qty=75, side="BUY", order_type="LIMIT", product="LONGTERM"):
    return _event(master, oid, side=side, order_type=order_type, product=product, exchange="NFO", symbol="NIFTY", qty=qty, price="2.35", token=token)


async def policy(child, mode, fixed=1, multiplier=None):
    async with TestingSessionLocal() as s:
        row = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == child.id))).scalar_one()
        row.quantity_mode, row.fixed_qty = mode, fixed
        await s.commit()


async def send(db_session, mock_redis, store, *, event_kwargs=None, mode=QuantityMode.MULTIPLIER, fixed=1, fake=None):
    master, child = await zerodha_child(db_session)
    await policy(child, mode, fixed)
    fake = fake or StatefulKite()
    client = make_client(mock_redis, fake, account_id=str(child.id), store=store)
    await seed_session(mock_redis, account_id=str(child.id))
    from test_zerodha_equity_lifecycle import new_pipeline
    res = await new_pipeline(mock_redis).execute_replication(event=alice_fno_event(master, "AF-1", **(event_kwargs or {})), clients={str(child.id): client})
    return fake, child, (res.get("results") or {}).get(str(child.id))


async def skip_reason(child):
    async with TestingSessionLocal() as s:
        rows = (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == child.id))).scalars().all()
    return [r.failure_reason for r in rows if r.failure_reason]


# ── Alice Master F&O -> Zerodha Child ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_alice_f_and_o_order_is_placed_at_zerodha_with_zerodhas_own_symbol_nfo_and_nrml(db_session, mock_redis, fno_open):
    fake, child, out = await send(db_session, mock_redis, fno_open)
    assert out and out["status"] == "SUCCESS", out
    (req,) = fake.forms("POST")
    assert (req["tradingsymbol"], req["exchange"], req["transaction_type"], req["quantity"], req["product"], req["order_type"]) == \
           (ZERODHA_SYMBOL, "NFO", "BUY", "75", "NRML", "LIMIT")
    assert "NIFTY" != req["tradingsymbol"] and NIFTY_CE_TOKEN not in str(req)                                  # not Alice's symbol, not Alice's token


@pytest.mark.asyncio
@pytest.mark.parametrize("product,expected", [("LONGTERM", "NRML"), ("INTRADAY", "MIS")])
async def test_the_carry_and_intraday_products_map_to_zerodhas_derivative_products(db_session, mock_redis, fno_open, product, expected):
    fake, _, out = await send(db_session, mock_redis, fno_open, event_kwargs={"product": product})
    assert out["status"] == "SUCCESS" and fake.forms("POST")[0]["product"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,fixed,master_qty,expected", [
    (QuantityMode.FIXED, 1, 75, "75"), (QuantityMode.FIXED, 2, 75, "150"), (QuantityMode.FIXED, 1, 750, "75"),
    (QuantityMode.MULTIPLIER, 1, 150, "150"),
])
async def test_the_zerodha_child_is_sized_in_whole_lots(db_session, mock_redis, fno_open, mode, fixed, master_qty, expected):
    fake, _, out = await send(db_session, mock_redis, fno_open, mode=mode, fixed=fixed, event_kwargs={"qty": master_qty})
    assert out["status"] == "SUCCESS" and fake.forms("POST")[0]["quantity"] == expected


@pytest.mark.asyncio
async def test_without_the_pair_listed_the_original_refusal_stands_and_nothing_is_sent(db_session, mock_redis, fno_open, monkeypatch):
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "")
    fake, child, out = await send(db_session, mock_redis, fno_open)
    assert fake.forms("POST") == [] and await skip_reason(child) == ["INSTRUMENT_UNMAPPED: CROSS_BROKER_DERIVATIVE_UNMAPPED:alicebluenew->zerodha:NFO"]


@pytest.mark.asyncio
@pytest.mark.parametrize("token,verdict", [("50004", "UNMAPPED"), ("60001", "UNMAPPED")])
async def test_a_contract_zerodhas_dump_does_not_hold_exactly_is_never_traded(db_session, mock_redis, fno_open, token, verdict):
    """50004 is NIFTY 23600 CE and 60001 BANKNIFTY 51500 CE: both exist in Alice's master, neither in the Zerodha fixture dump."""
    fake, child, out = await send(db_session, mock_redis, fno_open, event_kwargs={"token": token})
    (reason,) = await skip_reason(child)
    assert fake.forms("POST") == [] and f"CROSS_BROKER_FNO_{verdict}" in reason


@pytest.mark.asyncio
async def test_an_alice_token_with_no_row_in_alices_master_has_no_contract_and_is_refused(db_session, mock_redis, fno_open):
    fake, child, out = await send(db_session, mock_redis, fno_open, event_kwargs={"token": "99999999"})
    (reason,) = await skip_reason(child)
    assert fake.forms("POST") == [] and "CROSS_BROKER_FNO_CONTRACT_UNAVAILABLE" in reason


@pytest.mark.asyncio
async def test_an_unloaded_zerodha_catalog_blocks_the_copy(db_session, mock_redis, fno_open, monkeypatch):
    from app.services.zerodha.instruments import ZerodhaInstrumentStore

    monkeypatch.setattr(zi, "store", ZerodhaInstrumentStore())
    fake, child, out = await send(db_session, mock_redis, fno_open)
    (reason,) = await skip_reason(child)
    assert fake.forms("POST") == [] and "CROSS_BROKER_FNO_CATALOG_NOT_LOADED" in reason


@pytest.mark.asyncio
async def test_cash_equity_to_zerodha_is_unchanged_by_all_of_this(db_session, mock_redis, fno_open):
    from test_zerodha_equity_lifecycle import master_event

    master, child = await zerodha_child(db_session)
    fake = FakeKite()
    out, _, _ = await copy_to_zerodha(mock_redis, master, child, fake, master_event(master, "CASH-1", side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE"))
    (req,) = fake.calls("/orders/regular", "POST")
    assert out["status"] == "SUCCESS" and fake.form(req)["tradingsymbol"] == "SBIN" and fake.form(req)["product"] == "MIS"


# ── a Zerodha Master's F&O order carries the canonical contract ───────────────────────────────────────────────────────────────

def record(symbol=ZERODHA_SYMBOL, exchange="NFO", qty=75, filled=0, status="OPEN"):
    return OrderRecord(broker_order_id="Z1", client_order_id=None, symbol=symbol, exchange=exchange, side=OrderSide.BUY, quantity=qty, filled_quantity=filled,
                       average_price=Decimal("0"), status=status, order_timestamp=datetime.now(timezone.utc), order_type="LIMIT", product_type="NRML",
                       price=Decimal("2.35"))


def test_a_zerodha_nfo_order_gets_the_canonical_contract_only_when_the_unit_is_configured(monkeypatch):
    store = make_store()
    expected = make_contract("CE", "NFO", "NIFTY", "2026-11-26", "23500").key
    assert with_contract_key(record(), store).contract_key is None                                              # default: closed
    monkeypatch.setattr(settings, "ZERODHA_FNO_QUANTITY_UNIT", "LOTS")
    assert with_contract_key(record(), store).contract_key is None                                              # only UNITS is supported
    monkeypatch.setattr(settings, "ZERODHA_FNO_QUANTITY_UNIT", "units")
    out = with_contract_key(record(), store)
    assert out.contract_key == expected and (out.status, out.quantity, out.exchange, out.symbol) == ("OPEN", 75, "NFO", ZERODHA_SYMBOL)


@pytest.mark.parametrize("rec", [
    record(exchange="NSE", symbol="SBIN"), record(symbol="NOSUCHCONTRACT"), record(qty=70), record(qty=75, filled=30), record(status="UNSUPPORTED_VARIETY_AMO"),
], ids=["cash", "unknown-instrument", "not-a-lot-multiple", "part-fill-not-a-lot-multiple", "unsupported-status"])
def test_nothing_is_attached_to_an_order_it_cannot_prove_it_understands(monkeypatch, rec):
    monkeypatch.setattr(settings, "ZERODHA_FNO_QUANTITY_UNIT", "UNITS")
    assert with_contract_key(rec, make_store()).contract_key is None


@pytest.mark.asyncio
async def test_the_order_book_read_attaches_the_key_without_changing_the_fill_sync_view(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "ZERODHA_FNO_QUANTITY_UNIT", "UNITS")
    fake = StatefulKite()
    fake.book["2601010000A1"] = dict(status="COMPLETE", qty=75, filled=75, side="BUY", otype="LIMIT", price=Decimal("2.35"), trigger=Decimal("0"),
                                     symbol=ZERODHA_SYMBOL, product="NRML", tag="JTx", exchange="NFO")
    client = make_client(mock_redis, fake, account_id="acc-z")
    await seed_session(mock_redis, account_id="acc-z")
    (rec,) = await client.get_order_book()
    assert rec.contract_key == make_contract("CE", "NFO", "NIFTY", "2026-11-26", "23500").key and rec.status == "COMPLETE" and rec.filled_quantity == 75


# ── the routing registry: Zerodha is now a routable Child and a lot provider, nothing else changed ──────────────────────────────

def test_zerodha_is_registered_as_a_routable_child_with_a_symbol_identity_and_its_own_lot_provider(monkeypatch):
    assert "zerodha" in derivative_routing.CHILD_RESOLVERS and "zerodha" in derivative_routing.SYMBOL_IDENTITY_CHILDREN
    monkeypatch.setattr(zi, "store", make_store())
    assert derivative_routing.child_lot("zerodha", "NFO", ZERODHA_SYMBOL, None) == 75
    assert derivative_routing.child_lot("zerodha", "NSE", "SBIN", None) is None            # cash: no lot sizing
    assert derivative_routing.child_lot("zerodha", "NFO", "NOSUCH", None) is None          # unknown: sizing unchanged
    assert derivative_routing.child_lot("angelone", "NFO", "X", "1") is None               # no provider: sizing unchanged


def test_the_alice_master_contract_key_comes_only_from_alices_own_contract_master(monkeypatch):
    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))
    key = make_contract("CE", "NFO", "NIFTY", "2026-11-26", "23500").key
    assert derivative_routing.master_contract_key("alicebluenew", "NFO", NIFTY_CE_TOKEN) == key
    assert derivative_routing.master_contract_key("alicebluenew", "NFO", "50005") == make_contract("FUT", "NFO", "NIFTY", "2026-11-26").key
    for args in (("groww", "NFO", NIFTY_CE_TOKEN), ("alicebluenew", "NSE", NIFTY_CE_TOKEN), ("alicebluenew", "NFO", None), ("alicebluenew", "NFO", "424242")):
        assert derivative_routing.master_contract_key(*args) is None
