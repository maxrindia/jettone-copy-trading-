"""
A registered Groww (or Angel One) client does not implement the Alice-Blue-only raw order/position reads the P&L
service uses. That used to raise AttributeError inside the shared dashboard endpoints and turn
GET /accounts/metrics, /dashboard/summary and /dashboard/realized-timeline into HTTP 500 for EVERY account
(production, 2026-10-19: 169 failures in 21 minutes after a Groww account was added).

Now such an account gets an explicit UNAVAILABLE result with the note PNL_NOT_SUPPORTED_FOR_BROKER; nothing is
fabricated, no broker request is made for it, and every other account's figures are exactly what they were.
Offline: fake clients, mocked transports, in-memory DB.
"""
import uuid

import pytest

from app.core.trading_day import trading_day
from app.models.accounts import Account, AccountType
from app.services.pnl.service import PNL_NOT_SUPPORTED_NOTE
from conftest import TestingSessionLocal
import angel_helpers as A
import groww_helpers as G
from pnl_live_fixtures import FakeBrokerClient, MASTER_ORDER_BOOK, MASTER_POSITIONS, on_day
from test_dashboard_api import (  # noqa: F401  (fixtures and helpers of the dashboard suite)
    AUTH_HEADER, _install, _master_and_child, _strategy, api_client,
)
from test_pnl_service import ids, make_service  # noqa: F401


async def groww_account(session, name="Vikash-1"):
    acc = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name=name, alice_user_id=f"G_{name}",
                  broker="groww", is_active=True, session_valid=False)
    session.add(acc)
    await session.commit()
    return acc


def groww(mock_redis):
    fake = G.FakeGroww()
    return G.make_client(mock_redis, fake), fake


def angel(mock_redis):
    fake = A.FakeAngel()
    return A.make_client(mock_redis, fake), fake


# ── PnlService ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("make", [groww, angel], ids=["groww", "angel_one"])
async def test_a_client_without_the_raw_reads_is_unavailable_and_others_are_untouched(sample_accounts, mock_redis, make):
    master_id, child_id = ids(sample_accounts)
    unsupported, fake = make(mock_redis)
    clients = {master_id: FakeBrokerClient(MASTER_ORDER_BOOK, MASTER_POSITIONS), child_id: unsupported}
    summaries = await make_service(mock_redis, clients).summarize_accounts([master_id, child_id])      # must not raise

    ok = summaries[master_id].as_api_fields()
    assert (ok["realized_pnl"], ok["unrealized_pnl"], ok["total_pnl"], ok["pnl_status"]) == ("-1.52", "0.00", "-1.52", "OK")
    bad = summaries[child_id].as_api_fields()
    assert bad["pnl_status"] == "UNAVAILABLE" and bad["pnl_notes"] == [PNL_NOT_SUPPORTED_NOTE]
    assert bad["realized_pnl"] is None and bad["unrealized_pnl"] is None and bad["total_pnl"] is None   # nothing fabricated
    assert fake.requests == []                                                  # and no broker request was made for it


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [groww, angel], ids=["groww", "angel_one"])
async def test_the_realized_timeline_of_an_unsupported_client_is_unavailable_not_an_error(sample_accounts, mock_redis, make):
    master_id, child_id = ids(sample_accounts)
    unsupported, fake = make(mock_redis)
    clients = {master_id: FakeBrokerClient(MASTER_ORDER_BOOK, MASTER_POSITIONS), child_id: unsupported}
    timelines = await make_service(mock_redis, clients).realized_timelines([master_id, child_id])
    assert timelines[master_id].status in ("OK", "PARTIAL") and timelines[master_id].points
    assert timelines[child_id].status == "UNAVAILABLE" and timelines[child_id].points == []
    assert timelines[child_id].notes == [PNL_NOT_SUPPORTED_NOTE]
    assert fake.requests == []


class OrdersOnly:
    """Has the order-book read but not the position read."""
    async def get_order_book_raw(self):
        return []


@pytest.mark.asyncio
async def test_a_summary_needs_both_reads_but_a_timeline_needs_only_the_order_book(sample_accounts, mock_redis):
    master_id, child_id = ids(sample_accounts)
    clients = {master_id: OrdersOnly(), child_id: OrdersOnly()}
    summaries = await make_service(mock_redis, clients).summarize_accounts([master_id])
    assert summaries[master_id].as_api_fields()["pnl_notes"] == [PNL_NOT_SUPPORTED_NOTE]
    timelines = await make_service(mock_redis, clients).realized_timelines([child_id])
    assert PNL_NOT_SUPPORTED_NOTE not in timelines[child_id].notes              # it has what a timeline needs


class NotCallable:
    get_order_book_raw = None
    get_positions_raw = "not a function"


@pytest.mark.asyncio
async def test_a_non_callable_attribute_is_not_a_capability(sample_accounts, mock_redis):
    master_id, _ = ids(sample_accounts)
    s = await make_service(mock_redis, {master_id: NotCallable()}).summarize_accounts([master_id])
    assert s[master_id].as_api_fields()["pnl_notes"] == [PNL_NOT_SUPPORTED_NOTE]


@pytest.mark.asyncio
async def test_alice_style_clients_keep_exactly_their_results(sample_accounts, mock_redis):
    """The check must be invisible to a client that has the reads - including test doubles built from MagicMock."""
    from unittest.mock import AsyncMock, MagicMock
    master_id, child_id = ids(sample_accounts)
    mock_client = MagicMock()
    mock_client.get_order_book_raw = AsyncMock(return_value=on_day(MASTER_ORDER_BOOK, trading_day()))
    mock_client.get_positions_raw = AsyncMock(return_value=MASTER_POSITIONS)
    s = await make_service(mock_redis, {master_id: mock_client}).summarize_accounts([master_id])
    assert PNL_NOT_SUPPORTED_NOTE not in s[master_id].as_api_fields()["pnl_notes"]
    mock_client.get_order_book_raw.assert_awaited()


# ── the three shared endpoints (these returned HTTP 500 in production) ──────────────────────────

async def world(mock_redis, *, groww_in_strategy=False):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        extra = await groww_account(session)
        members = [(child, 1.0, True)] + ([(extra, 1.0, True)] if groww_in_strategy else [])
        await _strategy(session, "master", master, members)
    client, fake = groww(mock_redis)
    clients[str(extra.id)] = client
    _install(clients, live_master_id=str(master.id))
    return master, child, extra, fake


async def baseline(api_client, mock_redis, path):
    """The same endpoint with NO Groww account anywhere (fresh accounts, same Alice data)."""
    return (await api_client.get(path, headers=AUTH_HEADER)).json()


@pytest.mark.asyncio
async def test_accounts_metrics_answers_200_and_other_accounts_are_unchanged(api_client, db_session, mock_redis):
    master, child, extra, fake = await world(mock_redis)
    resp = await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    m = resp.json()["metrics"]
    assert (m[str(master.id)]["total_pnl"], m[str(master.id)]["pnl_status"]) == ("-1.52", "OK")
    assert (m[str(child.id)]["total_pnl"], m[str(child.id)]["pnl_status"]) == ("-1.48", "OK")
    g = m[str(extra.id)]
    assert g["pnl_status"] == "UNAVAILABLE" and g["pnl_notes"] == [PNL_NOT_SUPPORTED_NOTE]
    assert g["total_pnl"] is None and g["realized_pnl"] is None and g["unrealized_pnl"] is None
    assert fake.requests == []                                                  # no Groww request was made to build it


@pytest.mark.asyncio
async def test_dashboard_summary_answers_200_and_never_invents_a_combined_figure(api_client, db_session, mock_redis):
    master, child, extra, fake = await world(mock_redis)
    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    pnl = resp.json()["pnl"]
    per = {a["account_id"]: a for a in pnl["accounts"]}
    assert per[str(master.id)]["total_pnl"] == "-1.52" and per[str(child.id)]["total_pnl"] == "-1.48"
    assert per[str(extra.id)]["total_pnl"] is None
    assert pnl["accounts_unavailable"] == 1
    assert pnl["combined_total"] is None                                        # existing contract: no partial sum over an unverified account
    assert fake.requests == []


@pytest.mark.asyncio
async def test_realized_timeline_answers_200_and_an_alice_only_strategy_is_unchanged(api_client, db_session, mock_redis):
    await world(mock_redis)
    resp = await api_client.get("/api/v1/dashboard/realized-timeline", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    strat = list(resp.json()["strategies"].values())[0]
    assert strat["status"] == "OK" and strat["points"][-1]["cumulative_realized"] == "-3.00"


@pytest.mark.asyncio
async def test_a_strategy_that_contains_an_unsupported_account_is_flagged_partial_not_an_error(api_client, db_session, mock_redis):
    """A Groww account can only be in a strategy through legacy data (adding one is refused by the API now).
    The dashboard's existing rule for any follower it cannot verify applies unchanged: the follower is left out of
    the merge (never guessed) and the strategy is flagged PARTIAL, so the Master's real curve is still shown."""
    await world(mock_redis, groww_in_strategy=True)
    resp = await api_client.get("/api/v1/dashboard/realized-timeline", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    strat = list(resp.json()["strategies"].values())[0]
    assert strat["status"] == "PARTIAL"                                         # flagged, not silently "OK"
    # The Alice child's -1.48 is merged in as before only when it is usable; here it is, the Groww member is not:
    assert strat["points"][-1]["cumulative_realized"] == "-3.00"                # Master -1.52 + Alice child -1.48; Groww adds nothing


@pytest.mark.asyncio
async def test_the_alice_figures_equal_those_of_a_world_without_any_unsupported_account(api_client, db_session, mock_redis):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))
    without = (await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)).json()["metrics"]
    client, _ = groww(mock_redis)
    async with TestingSessionLocal() as session:
        extra = await groww_account(session)
    clients[str(extra.id)] = client
    _install(clients, live_master_id=str(master.id))
    with_groww = (await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)).json()["metrics"]
    for account_id in (str(master.id), str(child.id)):
        a, b = dict(without[account_id]), dict(with_groww[account_id])
        a.pop("as_of"), b.pop("as_of")
        assert a == b                                                           # byte-identical apart from the clock
    assert str(extra.id) in with_groww and str(extra.id) not in without
