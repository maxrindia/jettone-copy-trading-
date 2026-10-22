"""
A runtime-paused (or disabled / emergency-stopped) Child never receives a new MARKET copy order.

Bug. LIMIT and SL start with an explicit guard. The MARKET path only reached the pause through the risk gate, which runs
only for a Child WITH copy settings, so a paused Child without settings was copied (1 broker send, confirmed against the
code before R3). The MARKET path now has the same guard in the same first position: nothing runs for such a Child - no
funds fetch, sizing, risk evaluation, SELL guard, claim, Order pre-write or broker call. The pause state is the one the
fanout already resolved from the stop provider; there is no extra Redis read.

Known limitation, deliberate: the guard also blocks a position-reducing copy (an exit) for a paused Child, exactly as
LIMIT and SL do. There is no exit bypass.

Mocked brokers, in-memory database, no network.
"""
from dataclasses import replace
from decimal import Decimal

import pytest

from app.models.accounts import CopySetting, QuantityMode
from app.models.trades import CopyTradeStatus
from app.services.copy_engine import child_outcomes as co
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import link_strategy
from test_child_execution_tracking import account, client_returning, ok, result, run, summaries, trades_by_follower
from test_duplicate_create_db_backstop import KINDS, bypassed_redis, make_event, pipeline, rows

from app.services.alice_blue.types import OrderResultCategory

SKIPPED = {"status": "SKIPPED", "reason": "TRADING_DISABLED"}


def paused_pipeline(*account_ids):
    """The real mechanism: a stop provider returning (global_active, paused follower ids)."""
    async def provider():
        return False, {str(a) for a in account_ids}

    return CopyEnginePipeline(validator=EventValidator(redis=bypassed_redis()), stop_provider=provider)


async def child(db_session, master, name, *, settings=False):
    acc = await account(db_session, name)
    if settings:
        db_session.add(CopySetting(follower_account_id=acc.id, copy_enabled=True, quantity_mode=QuantityMode.FIXED, fixed_qty=5))
        await db_session.commit()
    return acc


# ══ the bug: a paused Child is not copied, with or without copy settings ═════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("with_settings", [False, True], ids=["no-settings", "with-settings"])
async def test_a_paused_child_gets_no_market_copy_and_leaves_only_a_skipped_row(db_session, sample_accounts, with_settings):
    f = await child(db_session, sample_accounts["master"], "PAUSED", settings=with_settings)
    await link_strategy(db_session, sample_accounts["master"], [f])
    client = client_returning(ok("BRK_NEVER"))
    mid = f"MP_PAUSED_{with_settings}"
    out = await run(make_event("MARKET", mid), {str(f.id): client}, paused_pipeline(f.id))
    assert out[str(f.id)] == SKIPPED
    client.place_order.assert_not_awaited()
    trades, orders = await rows(mid)
    assert orders == []                                                              # no Order pre-write
    (t,) = trades                                                                    # R3: the decision is recorded
    assert t.status == CopyTradeStatus.SKIPPED and t.quantity == 0 and t.client_order_id is None
    assert t.submitted_at is None and t.broker_order_id is None
    assert co.parse_reason(t.failure_reason) == ("TRADING_DISABLED", "")


@pytest.mark.asyncio
async def test_the_guard_runs_before_every_other_step(db_session, sample_accounts, monkeypatch):
    """Funds, sizing, the SELL guard, the claim and the Order pre-write are all booby-trapped: none may run."""
    f = await child(db_session, sample_accounts["master"], "ORDER")
    await link_strategy(db_session, sample_accounts["master"], [f])
    client = client_returning(ok("BRK_NEVER"))

    def boom(*args, **kwargs):
        raise AssertionError("a step after the pause guard ran for a paused Child")

    async def aboom(*args, **kwargs):
        raise AssertionError("a step after the pause guard ran for a paused Child")

    from app.services.copy_engine.calculator import QuantityCalculator
    monkeypatch.setattr(QuantityCalculator, "calculate_quantity", staticmethod(boom))
    monkeypatch.setattr(CopyEnginePipeline, "_enforce_sell_position_safety", aboom)
    monkeypatch.setattr(CopyEnginePipeline, "save_or_update_order", aboom)
    monkeypatch.setattr(CopyEnginePipeline, "_evaluate_risk_exit_aware", aboom)
    monkeypatch.setattr("app.services.copy_engine.pipeline.claim_child_trade", aboom)
    monkeypatch.setattr("app.services.copy_engine.pipeline.get_daily_loss_today", aboom)
    for side in ("BUY", "SELL"):                                                     # a SELL must not reach the SELL guard either
        out = await run(replace(make_event("MARKET", f"MP_ORDER_{side}"), side=side), {str(f.id): client}, paused_pipeline(f.id))
        assert out[str(f.id)] == SKIPPED
    client.get_funds.assert_not_awaited()
    client.get_positions.assert_not_awaited()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_inactive_or_emergency_stopped_child_is_skipped_before_its_settings_are_even_read(db_session, sample_accounts):
    """Called directly with a stale Account (the fanout filters these out, but the function must not rely on it). The
    copy_settings relationship is NOT loaded here, so touching it would fail: the guard returns first."""
    pl = pipeline()
    event = make_event("MARKET", "MP_DIRECT")
    for flag in ("is_active", "is_emergency_stopped"):
        acc = await account(db_session, f"DIRECT_{flag}")
        if flag == "is_active":
            acc.is_active = False
        else:
            acc.is_emergency_stopped = True
        client = client_returning(ok("BRK_NEVER"))
        out = await pl._process_single_market_follower(
            follower=acc, multiplier=Decimal("1.0"), event=event, client=client, lot_size=1, strategy_id=None,
            global_emergency_stop=False, follower_runtime_paused=False)
        assert out == SKIPPED
        client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_alias_path_used_by_the_reliability_tests_is_covered_too(db_session, sample_accounts):
    acc = await account(db_session, "ALIAS")
    client = client_returning(ok("BRK_NEVER"))
    out = await pipeline()._process_single_follower(
        follower=acc, event=make_event("MARKET", "MP_ALIAS"), client=client, lot_size=1, global_emergency_stop=False,
        follower_runtime_paused=True)
    assert out == SKIPPED
    client.place_order.assert_not_awaited()


# ══ an active Child is unchanged ═════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("with_settings,expected_qty", [(False, 10), (True, 5)], ids=["no-settings", "with-settings"])
async def test_an_active_child_is_copied_exactly_as_before(db_session, sample_accounts, with_settings, expected_qty):
    f = await child(db_session, sample_accounts["master"], "ACTIVE", settings=with_settings)
    await link_strategy(db_session, sample_accounts["master"], [f])
    client = client_returning(ok("BRK_ACTIVE"))
    mid = f"MP_ACTIVE_{with_settings}"
    out = await run(make_event("MARKET", mid), {str(f.id): client}, paused_pipeline())      # a provider that pauses nobody
    assert out[str(f.id)]["status"] == "SUCCESS" and client.place_order.await_count == 1
    assert client.place_order.call_args.args[0].quantity == expected_qty
    trades, orders = await rows(mid)
    assert trades[0].status == CopyTradeStatus.SUBMITTED and trades[0].broker_order_id == "BRK_ACTIVE" and len(orders) == 1


@pytest.mark.asyncio
async def test_pausing_one_child_does_not_affect_the_others_in_the_same_fanout(db_session, sample_accounts):
    master = sample_accounts["master"]
    active, paused_plain, paused_cfg = (await child(db_session, master, "A"), await child(db_session, master, "P1"),
                                        await child(db_session, master, "P2", settings=True))
    await link_strategy(db_session, master, [active, paused_plain, paused_cfg])
    clients = {str(a.id): client_returning(ok(f"BRK_{a.display_name}")) for a in (active, paused_plain, paused_cfg)}
    mid = "MP_MIXED"
    out = await run(make_event("MARKET", mid), clients, paused_pipeline(paused_plain.id, paused_cfg.id))
    assert out[str(active.id)]["status"] == "SUCCESS"
    assert out[str(paused_plain.id)] == SKIPPED and out[str(paused_cfg.id)] == SKIPPED
    assert clients[str(active.id)].place_order.await_count == 1
    assert clients[str(paused_plain.id)].place_order.await_count == 0 and clients[str(paused_cfg.id)].place_order.await_count == 0
    by = await trades_by_follower(mid)
    assert by[active.id][0].status == CopyTradeStatus.SUBMITTED
    assert by[paused_plain.id][0].status == CopyTradeStatus.SKIPPED and by[paused_cfg.id][0].status == CopyTradeStatus.SKIPPED
    (summary,) = await summaries(mid)
    kids = summary.metadata_payload["children"]
    assert kids[str(paused_plain.id)] == {"status": "SKIPPED", "code": "TRADING_DISABLED", "copy_trade_status": "SKIPPED"}
    assert kids[str(active.id)]["status"] == "SUCCESS"


# ══ LIMIT and SL: unchanged ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["LIMIT", "SL"])
@pytest.mark.parametrize("with_settings", [False, True], ids=["no-settings", "with-settings"])
async def test_limit_and_sl_still_skip_a_paused_child_and_still_copy_an_active_one(db_session, sample_accounts, kind, with_settings):
    master = sample_accounts["master"]
    paused, active = await child(db_session, master, "LP", settings=with_settings), await child(db_session, master, "LA", settings=with_settings)
    await link_strategy(db_session, master, [paused, active])
    clients = {str(paused.id): client_returning(ok("BRK_NEVER")), str(active.id): client_returning(ok("BRK_OK"))}
    out = await run(make_event(kind, f"MP_{kind}_{with_settings}"), clients, paused_pipeline(paused.id))
    assert out[str(paused.id)] == SKIPPED and out[str(active.id)]["status"] == "SUCCESS"
    clients[str(paused.id)].place_order.assert_not_awaited()
    assert clients[str(active.id)].place_order.await_count == 1


# ══ duplicate-event and unresolved-send safeguards stay intact ═══════════════════════════════

@pytest.mark.asyncio
async def test_a_skipped_pause_decision_can_be_reclaimed_once_after_redis_loss_and_then_is_a_normal_claim(db_session, sample_accounts):
    f = await child(db_session, sample_accounts["master"], "RECLAIM")
    await link_strategy(db_session, sample_accounts["master"], [f])
    mid = "MP_RECLAIM"
    paused = client_returning(ok("BRK_NEVER"))
    await run(make_event("MARKET", mid), {str(f.id): paused}, paused_pipeline(f.id))
    (first,) = (await trades_by_follower(mid))[f.id]
    assert first.status == CopyTradeStatus.SKIPPED

    live = client_returning(ok("BRK_AFTER_UNPAUSE"), ok("BRK_EXTRA"))
    out = await run(make_event("MARKET", mid), {str(f.id): live}, pipeline())          # Redis lost, pause lifted
    assert out[str(f.id)]["status"] == "SUCCESS" and live.place_order.await_count == 1
    (t,) = (await trades_by_follower(mid))[f.id]
    assert t.id == first.id and t.retry_count == 1 and t.status == CopyTradeStatus.SUBMITTED
    again = await run(make_event("MARKET", mid), {str(f.id): live}, pipeline())
    assert again[str(f.id)] == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"} and live.place_order.await_count == 1


@pytest.mark.asyncio
async def test_with_real_redis_dedupe_a_pause_decision_stands_and_a_redelivery_sends_nothing(db_session, mock_redis, sample_accounts):
    f = await child(db_session, sample_accounts["master"], "DEDUP")
    await link_strategy(db_session, sample_accounts["master"], [f])

    async def provider():
        return False, {str(f.id)}

    pl = CopyEnginePipeline(validator=EventValidator(redis=mock_redis), stop_provider=provider)
    await pl.execute_replication(event=make_event("MARKET", "MP_DEDUP"), clients={str(f.id): client_returning(ok("BRK_NEVER"))})
    healthy = client_returning(ok("BRK_NEVER_2"))
    again = await CopyEnginePipeline(validator=EventValidator(redis=mock_redis)).execute_replication(
        event=make_event("MARKET", "MP_DEDUP"), clients={str(f.id): healthy})
    assert again == {"status": "rejected", "reason": "DUPLICATE_EVENT"}               # stopped by Redis, per event
    healthy.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_market_timeout_for_an_active_child_is_still_never_resent(db_session, sample_accounts):
    f = await child(db_session, sample_accounts["master"], "AMB")
    await link_strategy(db_session, sample_accounts["master"], [f])
    client = client_returning(result(OrderResultCategory.TIMEOUT), ok("BRK_SECOND"))
    mid = "MP_AMB"
    first = await run(make_event("MARKET", mid), {str(f.id): client}, paused_pipeline())
    assert first[str(f.id)]["status"] == "TIMEOUT" and client.place_order.await_count == 1
    again = await run(make_event("MARKET", mid), {str(f.id): client}, pipeline())
    assert again[str(f.id)] == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"} and client.place_order.await_count == 1
    (t,) = (await trades_by_follower(mid))[f.id]
    assert t.status == CopyTradeStatus.TIMEOUT


# ══ the documented limitation, pinned so a change is deliberate ══════════════════════════════

@pytest.mark.asyncio
async def test_limitation_a_position_reducing_copy_is_blocked_for_a_paused_child_too(db_session, sample_accounts):
    """No exit bypass: a SELL that would close the Child's long position is still skipped while the Child is paused
    (as on LIMIT and SL). A paused Child's exits are handled manually."""
    f = await child(db_session, sample_accounts["master"], "EXIT")
    await link_strategy(db_session, sample_accounts["master"], [f])
    client = client_returning(ok("BRK_NEVER"))
    out = await run(replace(make_event("MARKET", "MP_EXIT"), side="SELL"), {str(f.id): client}, paused_pipeline(f.id))
    assert out[str(f.id)] == SKIPPED
    client.place_order.assert_not_awaited()
