"""
GET /accounts reports each account's CURRENT strategy role (from the strategy
membership registry), not the account_type label it was created with. The
Demat Accounts page renders this, so it must agree with the Dashboard's
strategy rows (which read the strategy's Master).
"""
import pytest

from app.models.accounts import AccountType
from strategy_helpers import link_strategy
from test_strategy_master_switching import AUTH, _account, api_client  # noqa: F401  (api_client is a fixture)


async def _roles_by_name(api_client):
    resp = await api_client.get("/api/v1/accounts", headers=AUTH)
    assert resp.status_code == 200, resp.text
    return {a["display_name"]: a for a in resp.json()}


@pytest.mark.asyncio
async def test_role_comes_from_the_strategy_not_the_creation_label(api_client, db_session):
    # The Master was created as a FOLLOWER-type account (the real situation).
    master = await _account(db_session, "alpha", AccountType.FOLLOWER, copy_setting=False)
    child = await _account(db_session, "beta", AccountType.FOLLOWER)
    loose = await _account(db_session, "gamma", AccountType.MASTER, copy_setting=False)
    await link_strategy(db_session, master, [child], name="Intraday")

    by_name = await _roles_by_name(api_client)
    assert by_name["alpha"]["account_type"] == "FOLLOWER"          # unchanged legacy label
    assert by_name["alpha"]["strategy_role"] == "MASTER"
    assert by_name["alpha"]["strategy_name"] == "Intraday"
    assert by_name["beta"]["strategy_role"] == "CHILD"
    # A MASTER-typed account in no strategy is not shown as a Master.
    assert by_name["gamma"]["account_type"] == "MASTER"
    assert by_name["gamma"]["strategy_role"] is None
    assert by_name["gamma"]["strategy_name"] is None
