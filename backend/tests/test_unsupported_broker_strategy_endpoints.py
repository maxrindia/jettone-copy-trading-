"""
Operations that exist only for Alice Blue must refuse a Groww / Angel One account with a CONTROLLED error, never an
AttributeError and never an Alice Blue client built for another broker's account:

  * GET  /strategies/{id}/members/{account}/positions   (Alice position detail)
  * POST /strategies/{id}/members/{account}/square-off  (places a REAL order through Alice Blue)
  * POST /accounts/callback                             (Alice Blue OAuth code exchange)
  * the Master-switch preflight                         (reads each account's raw Alice order book)

Alice Blue accounts take exactly the paths they always did. Offline: stubs and mocked transports only.
"""
import pytest
from sqlalchemy import select

from app.models.accounts import Account
from app.models.trades import Order
import groww_helpers as G
import angel_helpers as A
from conftest import TestingSessionLocal
from test_strategy_master_switching import (  # noqa: F401  (fixtures and helpers of the switching suite)
    AUTH, _Broker, _account, _codes, _preflight, _provider, api_client, trio,
)

SQUARE_OFF = {"symbol": "SBIN-EQ", "exchange": "NSE", "quantity": 5, "side": "BUY", "product_type": "INTRADAY"}


class NoAliceClientMayBeBuilt:
    """Stands in for AliceBlueClient in app.api.* - constructing one for a non-Alice account is the bug."""
    constructed = 0

    def __init__(self, *a, **k):
        type(self).constructed += 1
        raise AssertionError("an AliceBlueClient was built for a non-Alice account")


async def set_broker(account, broker):
    """Turn an Alice-style test account into a vault-credential one, as a real Groww / Angel One account is: its
    broker changes and it has NO Alice api_credential row."""
    from app.models.accounts import ApiCredential
    async with TestingSessionLocal() as s:
        row = await s.get(Account, account.id)
        row.broker = broker
        for cred in (await s.execute(select(ApiCredential).where(ApiCredential.account_id == account.id))).scalars().all():
            await s.delete(cred)
        await s.commit()


# ── member positions / square-off ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["groww", "angelone"])
async def test_member_positions_for_a_non_alice_broker_is_a_controlled_409(api_client, trio, monkeypatch, broker):
    await set_broker(trio.vikash, broker)
    NoAliceClientMayBeBuilt.constructed = 0
    monkeypatch.setattr("app.api.strategies.AliceBlueClient", NoAliceClientMayBeBuilt)
    resp = await api_client.get(f"/api/v1/strategies/{trio.strategy.id}/members/{trio.vikash.id}/positions", headers=AUTH)
    assert resp.status_code == 409
    assert "no API credentials" not in resp.text                                # the old, misleading advice for vault brokers
    assert broker in resp.text.lower() or "supported" in resp.text.lower()      # names the real problem: the broker
    assert NoAliceClientMayBeBuilt.constructed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["groww", "angelone"])
async def test_square_off_for_a_non_alice_broker_is_refused_and_places_nothing(api_client, trio, monkeypatch, broker):
    await set_broker(trio.vikash, broker)
    NoAliceClientMayBeBuilt.constructed = 0
    monkeypatch.setattr("app.api.strategies.AliceBlueClient", NoAliceClientMayBeBuilt)
    resp = await api_client.post(f"/api/v1/strategies/{trio.strategy.id}/members/{trio.vikash.id}/square-off",
                                 json=SQUARE_OFF, headers=AUTH)
    assert resp.status_code == 409
    assert NoAliceClientMayBeBuilt.constructed == 0
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(Order).where(Order.account_id == trio.vikash.id))).scalars().all() == []   # no pre-written order row


@pytest.mark.asyncio
async def test_an_alice_member_still_reaches_its_client(api_client, trio, monkeypatch):
    calls = []

    class FakeAlice:
        def __init__(self, **kw):
            calls.append(kw["account_id"])

        async def get_positions_detail(self, include_closed=False):
            return []

        async def get_order_book(self):
            return []
    monkeypatch.setattr("app.api.strategies.AliceBlueClient", FakeAlice)
    resp = await api_client.get(f"/api/v1/strategies/{trio.strategy.id}/members/{trio.vikash.id}/positions", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert calls == [str(trio.vikash.id)]


@pytest.mark.asyncio
async def test_an_alice_member_without_credentials_keeps_its_original_message(api_client, db_session, trio, monkeypatch):
    from app.models.accounts import ApiCredential
    async with TestingSessionLocal() as s:
        cred = (await s.execute(select(ApiCredential).where(ApiCredential.account_id == trio.vikash.id))).scalar_one()
        await s.delete(cred)
        await s.commit()
    resp = await api_client.get(f"/api/v1/strategies/{trio.strategy.id}/members/{trio.vikash.id}/positions", headers=AUTH)
    assert resp.status_code == 409 and "has no API credentials" in resp.json()["detail"]


# ── OAuth callback ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_alice_oauth_callback_refuses_a_groww_account(api_client, trio, monkeypatch):
    await set_broker(trio.vikash, "groww")
    NoAliceClientMayBeBuilt.constructed = 0
    monkeypatch.setattr("app.api.accounts.AliceBlueClient", NoAliceClientMayBeBuilt)
    resp = await api_client.post("/api/v1/accounts/callback", headers=AUTH,
                                 json={"auth_code": "x", "account_id": str(trio.vikash.id)})
    assert resp.status_code == 409
    assert NoAliceClientMayBeBuilt.constructed == 0


# ── Master-switch preflight ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("make", [lambda r: G.make_client(r, G.FakeGroww()), lambda r: A.make_client(r, A.FakeAngel())],
                         ids=["groww", "angel_one"])
async def test_preflight_blocks_with_an_accurate_message_when_a_child_cannot_be_verified(db_session, trio, mock_redis, make):
    """A Groww / Angel One child in the switch scope cannot have its order book read the Alice way. The preflight
    already failed closed through a caught AttributeError; it now says so explicitly and never reads 'nothing open'."""
    key = str(trio.vikash.id)
    unsupported = make(mock_redis)
    trio.brokers[key] = unsupported
    pre = await _preflight(db_session, trio, trio.arun)
    assert not pre.allowed
    assert ("BROKER_VERIFICATION_UNAVAILABLE", key) in _codes(pre)
    blocker = next(b for b in pre.blockers if b.account_id == key and b.code == "BROKER_VERIFICATION_UNAVAILABLE")
    assert "does not support the order-book verification" in blocker.message
    assert "Alice Blue order book could not be fetched" not in blocker.message
    assert unsupported._http is not None and not hasattr(unsupported, "get_order_book_raw")


@pytest.mark.asyncio
async def test_preflight_for_an_all_alice_strategy_is_unchanged(db_session, trio):
    pre = await _preflight(db_session, trio, trio.vikash)
    assert pre.allowed and pre.blockers == []


@pytest.mark.asyncio
async def test_a_groww_account_can_still_never_become_a_master(api_client, db_session, trio):
    groww = await _account(db_session, "groww_new")
    await set_broker(groww, "groww")
    trio.brokers[str(groww.id)] = _Broker()
    api_client.brokers.update(trio.brokers)
    resp = await api_client.post(f"/api/v1/strategies/{trio.strategy.id}/switch-master",
                                 json={"new_master_account_id": str(groww.id)}, headers=AUTH)
    assert resp.status_code == 409 and "can only be a Child account" in resp.text
