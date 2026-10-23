"""
R4f: per-order planner checks (segment, instrument mapping, product, order type) behind CANONICAL_MAPPING_ENFORCED.

The checks decide, for ONE Master order and ONE Child, whether that Child's broker can take the order at all. A failing Child
is SKIPPED with its own reason on its own copy_trades row; every other Child is unaffected. The flag defaults to false and,
when false, nothing is computed and execution is exactly as before. With the flag on the checks fail closed. No broker is
called and no order is placed anywhere in this file (the clients are mocks).
"""
import ast
import dataclasses
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import Settings, settings
from app.models.accounts import Account, AccountType
from app.models.audit import AuditLog
from app.models.trades import CopyTradeStatus
from app.services.alice_blue.types import OrderResultCategory
from app.services.brokers.capabilities import CapabilityProfile, SegmentCaps, Verification
from app.services.copy_engine import child_outcomes as co
from app.services.groww.instruments import GrowwInstrumentStore
from app.services.instruments import planner
from app.services.instruments.catalogs import GrowwCatalog
from app.services.instruments.planner import check_child_order
from conftest import TestingSessionLocal
from strategy_helpers import link_strategy
from test_child_execution_tracking import client_returning, run, trades_by_follower
from test_duplicate_create_db_backstop import KINDS, make_event, ok, pipeline
from test_instrument_catalogs import INFY, NOW, REL, groww_row, groww_store

ALICE, GROWW = "alicebluenew", "groww"


def check(**over):
    base = dict(master_broker=ALICE, child_broker=ALICE, exchange="NSE", symbol="RELIANCE", product_type="INTRADAY",
                order_type="LIMIT", instrument_token="2885", now=NOW)
    base.update(over)
    return check_child_order(**base)


def code(skip):
    return None if skip is None else (skip.code, skip.detail)


def restricted(broker_id, products, order_types, exchanges=(("NSE", "CASH"), ("BSE", "CASH"))):
    segs = tuple(SegmentCaps(ex, seg, frozenset(products), frozenset(order_types), Verification.VERIFIED_DOCS)
                 for ex, seg in exchanges)
    return CapabilityProfile(broker_id=broker_id, features={}, segments=segs, gates={})


def with_profiles(monkeypatch, **per_broker):
    real = planner.build_profile
    monkeypatch.setattr(planner, "build_profile", lambda b: per_broker.get(b) or real(b))


# ══ the flag ═════════════════════════════════════════════════════════════════════════════

def test_enforcement_is_off_by_default():
    assert Settings(_env_file=None).CANONICAL_MAPPING_ENFORCED is False
    assert settings.CANONICAL_MAPPING_ENFORCED is False


# ══ 1. segment ═══════════════════════════════════════════════════════════════════════════

def test_a_segment_the_childs_broker_lists_is_supported():
    assert check() is None                                                        # Alice NSE cash
    assert check(exchange="BSE", symbol="SBIN", instrument_token="500112") is None   # Alice BSE cash (listed, unverified)
    assert check(exchange="NFO", symbol="NIFTY", instrument_token="74056") is None  # Alice NFO (OPT / FUT listed)
    assert check(child_broker=GROWW, instrument_token=None, master_isin=REL, catalogs=[GrowwCatalog(groww_store(groww_row()))]) is None


@pytest.mark.parametrize("child,exchange,detail", [
    (GROWW, "NFO", "NFO_NOT_LISTED_FOR_groww"),
    (GROWW, "BFO", "BFO_NOT_LISTED_FOR_groww"),
    (ALICE, "MCX", "MCX_NOT_LISTED_FOR_alicebluenew"),
    ("angelone", "NFO", "NFO_NOT_LISTED_FOR_angelone"),
])
def test_a_segment_the_childs_broker_does_not_list_is_not_supported(child, exchange, detail):
    assert code(check(child_broker=child, exchange=exchange)) == ("SEGMENT_NOT_SUPPORTED", detail)


@pytest.mark.parametrize("exchange,detail", [(None, "MISSING_EXCHANGE"), ("", "MISSING_EXCHANGE"), ("  ", "MISSING_EXCHANGE"),
                                             ("XYZ", "UNKNOWN_EXCHANGE:XYZ"), ("nse_cm", "UNKNOWN_EXCHANGE:NSE_CM")])
def test_a_missing_or_unknown_exchange_fails_closed(exchange, detail):
    assert code(check(exchange=exchange)) == ("SEGMENT_NOT_SUPPORTED", detail)


@pytest.mark.parametrize("broker", ["zerodha", "5paisa", "", "nonsense"])
def test_an_unknown_or_unsupported_child_broker_fails_closed_without_a_profile(broker):
    skip = check(child_broker=broker)
    if broker == "":                                  # an empty stored broker is the historical Alice Blue default
        assert skip is None
    else:
        assert skip is not None and skip.code == "SEGMENT_NOT_SUPPORTED"


def test_the_exchange_is_matched_case_insensitively():
    assert check(exchange="nfo", symbol="NIFTY", instrument_token="74056") is None


# ══ 2. instrument mapping ════════════════════════════════════════════════════════════════

def test_same_broker_a_token_maps_any_instrument():
    assert check(exchange="NFO", symbol="NIFTY", instrument_token="74056") is None
    assert check(exchange="NFO", symbol="NIFTY", instrument_token="  74056 ") is None


def test_same_broker_a_plain_equity_symbol_maps_without_a_token_as_it_does_today():
    assert check(instrument_token=None) is None
    assert check(symbol="RELIANCE-EQ", instrument_token="") is None
    assert check(exchange="BSE", symbol="SBIN", instrument_token=None) is None


@pytest.mark.parametrize("token", [None, "", "   ", 74056])
def test_same_broker_a_derivative_without_a_token_is_ambiguous_and_skipped(token):
    assert code(check(exchange="NFO", symbol="NIFTY", instrument_token=token)) == \
        ("INSTRUMENT_UNMAPPED", "NO_INSTRUMENT_TOKEN_FOR_DERIVATIVE")


@pytest.mark.parametrize("symbol,detail", [("NIFTY", "SYMBOL_NOT_RESOLVABLE:NOT_AN_EQUITY_INDEX"),
                                           ("RELI ANCE", "SYMBOL_NOT_RESOLVABLE:MALFORMED_SYMBOL"),
                                           ("", "SYMBOL_NOT_RESOLVABLE:MISSING_SYMBOL"), (None, "SYMBOL_NOT_RESOLVABLE:MISSING_SYMBOL")])
def test_same_broker_an_unresolvable_equity_symbol_without_a_token_is_skipped(symbol, detail):
    assert code(check(symbol=symbol, instrument_token=None)) == ("INSTRUMENT_UNMAPPED", detail)


def test_a_different_broker_needs_an_isin_level_identity_and_fails_closed_without_one():
    assert code(check(child_broker=GROWW)) == ("INSTRUMENT_UNMAPPED", "MASTER_ISIN_UNAVAILABLE")
    # the Master's token is never a substitute for an identity at another broker
    assert code(check(child_broker=GROWW, instrument_token="2885")) == ("INSTRUMENT_UNMAPPED", "MASTER_ISIN_UNAVAILABLE")


def test_an_unknown_master_broker_fails_closed_for_every_child():
    assert code(check(master_broker=None)) == ("INSTRUMENT_UNMAPPED", "MASTER_BROKER_UNKNOWN")
    assert code(check(master_broker="nonsense", child_broker=GROWW)) == ("INSTRUMENT_UNMAPPED", "MASTER_BROKER_UNKNOWN")


def groww_check(store, isin=REL, symbol="RELIANCE", **over):
    return check(child_broker=GROWW, instrument_token=None, symbol=symbol, master_isin=isin, catalogs=[GrowwCatalog(store)], **over)


def test_a_cross_broker_instrument_maps_only_on_a_definitive_catalog_match():
    assert groww_check(groww_store(groww_row())) is None                                   # exact ISIN match


@pytest.mark.parametrize("rows,expected", [
    ([], "CATALOG_NOT_LOADED:CATALOG_NOT_LOADED"),                                          # an empty store is not loaded
    ([groww_row(symbol="INFY", isin=INFY)], "UNMAPPED:NOT_FOUND"),                          # missing
    ([groww_row(), groww_row(isin=REL)], "AMBIGUOUS:MULTIPLE_CANDIDATES_FOR_ISIN"),         # ambiguous: two rows, one ISIN
    ([groww_row(), groww_row(isin=INFY)], "AMBIGUOUS:SYMBOL_SHARED_WITH_OTHER_ISIN"),       # ambiguous: symbol shared
    ([groww_row(isin=INFY)], "UNMAPPED:ISIN_CONFLICT"),                                     # the symbol carries a different ISIN
    ([groww_row(isin="")], "UNMAPPED:CANDIDATE_LACKS_ISIN"),                                # the broker row has no ISIN
    ([groww_row(), groww_row(isin="")], "UNMAPPED:CANDIDATE_LACKS_ISIN"),                   # an ISIN-less competitor
])
def test_a_missing_or_ambiguous_cross_broker_mapping_fails_closed(rows, expected):
    store = groww_store(*rows) if rows else GrowwInstrumentStore()
    skip = groww_check(store)
    assert code(skip) == ("INSTRUMENT_UNMAPPED", expected)


def test_a_stale_catalog_and_an_invalid_master_identity_fail_closed():
    store = groww_store(groww_row())
    from datetime import timedelta
    assert code(groww_check(store, now=NOW + timedelta(days=2))) == ("INSTRUMENT_UNMAPPED", "MASTER_DATA_STALE:CATALOG_OLDER_THAN_LIMIT")
    assert code(groww_check(store, isin="INE002A01019")) == ("INSTRUMENT_UNMAPPED", "MASTER_IDENTITY_UNMAPPED:MALFORMED_ISIN")
    assert code(groww_check(store, symbol="NIFTY")) == ("INSTRUMENT_UNMAPPED", "MASTER_IDENTITY_UNMAPPED:NOT_AN_EQUITY_INDEX")


def test_a_cross_broker_child_with_no_catalog_entry_fails_closed():
    skip = check(child_broker=GROWW, instrument_token=None, master_isin=REL, catalogs=[])
    assert code(skip) == ("INSTRUMENT_UNMAPPED", "NO_CATALOG_FOR_groww")


# ══ 3. product ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("product", ["MIS", "INTRADAY", "CNC", "LONGTERM", "DELIVERY", "NRML", "MTF", "CARRYFORWARD", "margin"])
def test_every_known_product_spelling_passes_for_alice(product):
    assert check(product_type=product) is None


@pytest.mark.parametrize("product", ["BO", "CO", "", None, "SOMETHING"])
def test_an_unknown_product_is_never_guessed(product):
    skip = check(product_type=product)
    assert skip is not None and skip.code == "PRODUCT_NOT_SUPPORTED" and skip.detail.startswith("UNKNOWN_PRODUCT:")


def test_a_product_the_listed_segment_does_not_carry_is_not_supported(monkeypatch):
    with_profiles(monkeypatch, **{ALICE: restricted(ALICE, {"MIS"}, {"LIMIT", "MARKET", "SL", "SLM"})})
    assert check(product_type="INTRADAY") is None
    assert code(check(product_type="DELIVERY")) == ("PRODUCT_NOT_SUPPORTED", "CNC_NOT_LISTED_FOR_alicebluenew_NSE")
    assert code(check(product_type="NRML")) == ("PRODUCT_NOT_SUPPORTED", "NRML_NOT_LISTED_FOR_alicebluenew_NSE")


def test_the_derivative_product_fold_still_applies():
    assert check(exchange="NFO", symbol="NIFTY", instrument_token="74056", product_type="LONGTERM") is None   # CNC -> NRML on NFO


# ══ 4. order type ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("order_type", ["MARKET", "LIMIT", "SL", "SLM", "SL-M", "sl_m", "market"])
def test_every_known_order_type_passes_for_alice(order_type):
    assert check(order_type=order_type) is None


@pytest.mark.parametrize("order_type", ["ICEBERG", "STOPLIMIT", "", None])
def test_an_unknown_order_type_is_never_guessed_or_substituted(order_type):
    skip = check(order_type=order_type)
    assert skip is not None and skip.code == "ORDER_TYPE_NOT_SUPPORTED" and skip.detail.startswith("UNKNOWN_ORDER_TYPE:")


def test_an_order_type_the_segment_does_not_carry_is_skipped_not_substituted(monkeypatch):
    with_profiles(monkeypatch, **{ALICE: restricted(ALICE, {"MIS", "CNC", "NRML"}, {"LIMIT", "MARKET"})})
    assert check(order_type="LIMIT") is None
    assert code(check(order_type="SLM")) == ("ORDER_TYPE_NOT_SUPPORTED", "SLM_NOT_LISTED_FOR_alicebluenew_NSE_MIS")
    assert code(check(order_type="SL-M")) == ("ORDER_TYPE_NOT_SUPPORTED", "SLM_NOT_LISTED_FOR_alicebluenew_NSE_MIS")


def test_the_first_failing_rule_wins_in_the_documented_order(monkeypatch):
    # segment, then instrument, then product, then order type
    assert check(child_broker=GROWW, exchange="NFO", product_type="BO", order_type="ICEBERG").code == "SEGMENT_NOT_SUPPORTED"
    assert check(child_broker=GROWW, product_type="BO", order_type="ICEBERG").code == "INSTRUMENT_UNMAPPED"
    assert check(product_type="BO", order_type="ICEBERG").code == "PRODUCT_NOT_SUPPORTED"
    assert check(order_type="ICEBERG").code == "ORDER_TYPE_NOT_SUPPORTED"


def test_the_checks_are_deterministic_and_do_no_io():
    a = check(child_broker=GROWW, exchange="NFO")
    assert a == check(child_broker=GROWW, exchange="NFO")
    tree = ast.parse(Path(planner.__file__).read_text(encoding="utf-8"))
    names = " ".join((n.module or "") if isinstance(n, ast.ImportFrom) else " ".join(x.name for x in n.names)
                     for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))).lower()
    for banned in ("redis", "httpx", "requests", "sqlalchemy", "client", "database", "socket"):
        assert banned not in names, banned


# ══ child_outcomes: a skip may carry a detail ════════════════════════════════════════════

def test_a_skip_detail_is_recorded_and_older_skips_are_unchanged():
    assert co.pre_send_outcome({"status": "SKIPPED", "reason": "SEGMENT_NOT_SUPPORTED", "detail": "NFO_NOT_LISTED_FOR_groww"}) == \
        (CopyTradeStatus.SKIPPED, "SEGMENT_NOT_SUPPORTED", "NFO_NOT_LISTED_FOR_groww")
    assert co.pre_send_outcome({"status": "SKIPPED", "reason": "TRADING_DISABLED"}) == (CopyTradeStatus.SKIPPED, "TRADING_DISABLED", "")
    assert co.pre_send_outcome({"status": "SKIPPED", "reason": co.DUPLICATE_EVENT, "detail": "x"}) is None
    assert co.describe_result({"status": "SKIPPED", "reason": "PRODUCT_NOT_SUPPORTED", "detail": "d"}) == ("SKIPPED", "PRODUCT_NOT_SUPPORTED")


# ══ the pipeline: mixed Children, flag on and off ════════════════════════════════════════

async def broker_account(db, name, broker):
    acc = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name=name, broker=broker,
                  alice_user_id=f"AB_R4F_{name}_{uuid.uuid4().hex[:6]}", is_active=True)
    db.add(acc)
    await db.commit()
    return acc


@pytest_asyncio.fixture
async def mixed(db_session, sample_accounts, monkeypatch):
    """Master (Alice) with three Children: A (Alice, accepted), G (Groww), B (Alice, whose funds fetch fails)."""
    monkeypatch.setattr("app.services.copy_engine.pipeline.can_be_child", lambda broker: True)   # route a Groww Child
    master = sample_accounts["master"]
    a = await broker_account(db_session, "A", ALICE)
    g = await broker_account(db_session, "G", GROWW)
    b = await broker_account(db_session, "B", ALICE)
    await link_strategy(db_session, master, [a, g, b])
    clients = {
        str(a.id): client_returning(ok("BRK_A")),
        str(g.id): client_returning(ok("BRK_G")),
        str(b.id): client_returning(funds=RuntimeError("funds down")),
    }
    return dict(a=a, g=g, b=b, clients=clients)


def enforced(monkeypatch, on=True):
    monkeypatch.setattr(settings, "CANONICAL_MAPPING_ENFORCED", on)


async def skip_audits(master_order_id):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(AuditLog).where(AuditLog.event_type == "COPY_SKIPPED",
                                                       AuditLog.master_order_id == master_order_id))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_an_unsupported_segment_skips_only_that_child(db_session, mixed, monkeypatch, kind):
    enforced(monkeypatch)
    mid = f"R4F_SEG_{kind}"
    event = dataclasses.replace(make_event(kind, mid), exchange="NFO", instrument_id="74056")
    out = await run(event, mixed["clients"])
    a, g, b = mixed["a"], mixed["g"], mixed["b"]
    assert out[str(g.id)] == {"status": "SKIPPED", "reason": "SEGMENT_NOT_SUPPORTED", "detail": "NFO_NOT_LISTED_FOR_groww"}
    assert out[str(a.id)]["status"] == "SUCCESS"                                          # the eligible Child is unaffected
    assert out[str(b.id)] == {"status": "REJECTED", "reasons": ["FUNDS_FETCH_FAILED"]}   # and so is its own, different outcome
    assert mixed["clients"][str(g.id)].place_order.await_count == 0 and mixed["clients"][str(g.id)].get_funds.await_count == 0
    assert mixed["clients"][str(a.id)].place_order.await_count == 1

    by = await trades_by_follower(mid)
    (tg,), (ta,), (tb,) = by[g.id], by[a.id], by[b.id]
    assert tg.status == CopyTradeStatus.SKIPPED and tg.quantity == 0 and tg.client_order_id is None and tg.submitted_at is None
    assert co.parse_reason(tg.failure_reason) == ("SEGMENT_NOT_SUPPORTED", "NFO_NOT_LISTED_FOR_groww")
    assert ta.status == CopyTradeStatus.SUBMITTED and tb.status == CopyTradeStatus.REJECTED
    (audit,) = await skip_audits(mid)
    assert audit.account_id == g.id and audit.reason_code == "SEGMENT_NOT_SUPPORTED" and audit.quantity == 0
    assert audit.metadata_payload["child_broker"] == GROWW and audit.metadata_payload["detail"] == "NFO_NOT_LISTED_FOR_groww"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_an_unmapped_cross_broker_instrument_skips_only_that_child(db_session, mixed, monkeypatch, kind):
    """With no ISIN-level Master identity (none exists yet) a cross-broker Child is unmapped, fail closed."""
    enforced(monkeypatch)
    mid = f"R4F_UNM_{kind}"
    out = await run(make_event(kind, mid), mixed["clients"])
    a, g = mixed["a"], mixed["g"]
    assert out[str(g.id)] == {"status": "SKIPPED", "reason": "INSTRUMENT_UNMAPPED", "detail": "MASTER_ISIN_UNAVAILABLE"}
    assert out[str(a.id)]["status"] == "SUCCESS" and mixed["clients"][str(g.id)].place_order.await_count == 0
    (tg,) = (await trades_by_follower(mid))[g.id]
    assert co.parse_reason(tg.failure_reason) == ("INSTRUMENT_UNMAPPED", "MASTER_ISIN_UNAVAILABLE")


@pytest.mark.asyncio
async def test_a_same_broker_derivative_without_a_token_is_skipped_but_with_one_it_is_copied(db_session, mixed, monkeypatch):
    enforced(monkeypatch)
    a, b = mixed["a"], mixed["b"]
    clients = {str(a.id): client_returning(ok("BRK_A")), str(b.id): client_returning(ok("BRK_B"))}
    bare = dataclasses.replace(make_event("LIMIT", "R4F_NOTOKEN"), exchange="NFO", symbol="NIFTY", instrument_id=None)
    out = await run(bare, clients)
    for child in (a, b):
        assert out[str(child.id)] == {"status": "SKIPPED", "reason": "INSTRUMENT_UNMAPPED", "detail": "NO_INSTRUMENT_TOKEN_FOR_DERIVATIVE"}
        assert clients[str(child.id)].place_order.await_count == 0
    tokened = dataclasses.replace(bare, master_order_id="R4F_TOKEN", instrument_id="74056")
    clients = {str(a.id): client_returning(ok("BRK_A")), str(b.id): client_returning(ok("BRK_B"))}
    out = await run(tokened, clients)
    assert out[str(a.id)]["status"] == "SUCCESS" and out[str(b.id)]["status"] == "SUCCESS"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,patch_event,profile,reason,detail", [
    ("LIMIT", dict(product_type="NRML"), ("MIS",), "PRODUCT_NOT_SUPPORTED", "NRML_NOT_LISTED_FOR_groww_NSE"),
    ("LIMIT", dict(product_type="BO"), ("MIS", "CNC", "NRML"), "PRODUCT_NOT_SUPPORTED", "UNKNOWN_PRODUCT:BO"),
    ("SL", dict(order_type="SLM"), ("MIS", "CNC", "NRML"), "ORDER_TYPE_NOT_SUPPORTED", "SLM_NOT_LISTED_FOR_groww_NSE_MIS"),
    ("SL", dict(order_type="SL"), ("MIS", "CNC", "NRML"), "ORDER_TYPE_NOT_SUPPORTED", "SL_NOT_LISTED_FOR_groww_NSE_MIS"),
])
async def test_an_unsupported_product_or_order_type_skips_only_that_child(db_session, mixed, monkeypatch, kind, patch_event, profile, reason, detail):
    """The instrument is treated as mapped (an ISIN-level identity is simulated) so product / order type are what decides."""
    enforced(monkeypatch)
    monkeypatch.setattr(planner, "_instrument_problem", lambda **kw: None)
    with_profiles(monkeypatch, **{GROWW: restricted(GROWW, set(profile), {"LIMIT", "MARKET"})})
    mid = f"R4F_{reason}_{kind}_{uuid.uuid4().hex[:4]}"
    out = await run(dataclasses.replace(make_event(kind, mid), **patch_event), mixed["clients"])
    a, g = mixed["a"], mixed["g"]
    if reason == "PRODUCT_NOT_SUPPORTED" and detail.startswith("UNKNOWN_PRODUCT"):
        assert out[str(a.id)]["status"] == "SKIPPED"                  # an unknown product is unknown to every Child's broker
        assert out[str(a.id)]["reason"] == reason
    else:
        assert out[str(a.id)]["status"] == "SUCCESS"                  # Alice lists it: unaffected
    assert out[str(g.id)] == {"status": "SKIPPED", "reason": reason, "detail": detail}
    assert mixed["clients"][str(g.id)].place_order.await_count == 0


@pytest.mark.asyncio
async def test_the_market_child_order_type_follows_the_execution_mode(db_session, mixed, monkeypatch):
    """A Master MARKET becomes a LIMIT for the Child in MARKETABLE_LIMIT mode (the default), a MARKET otherwise."""
    enforced(monkeypatch)
    monkeypatch.setattr(planner, "_instrument_problem", lambda **kw: None)
    with_profiles(monkeypatch, **{GROWW: restricted(GROWW, {"MIS"}, {"LIMIT"})})
    out = await run(make_event("MARKET", "R4F_MKT_LIMITMODE"), mixed["clients"])
    assert out[str(mixed["g"].id)]["status"] == "SUCCESS"            # a LIMIT is carried
    monkeypatch.setattr(settings, "MARKET_ORDER_EXECUTION_MODE", "MARKET")
    out = await run(make_event("MARKET", "R4F_MKT_MARKETMODE"), {k: client_returning(ok("X")) for k in mixed["clients"]})
    assert out[str(mixed["g"].id)] == {"status": "SKIPPED", "reason": "ORDER_TYPE_NOT_SUPPORTED",
                                      "detail": "MARKET_NOT_LISTED_FOR_groww_NSE_MIS"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_alice_to_alice_skips_nothing_with_enforcement_on(db_session, sample_accounts, sample_strategy, monkeypatch, kind):
    enforced(monkeypatch)
    f = sample_accounts["follower1"]
    for product in ("MIS", "INTRADAY", "CNC", "LONGTERM", "NRML"):
        client = client_returning(ok("BRK_OK"))
        event = dataclasses.replace(make_event(kind, f"R4F_A2A_{kind}_{product}"), product_type=product)
        out = await run(event, {str(f.id): client})
        assert out[str(f.id)]["status"] == "SUCCESS", (product, out[str(f.id)])
        assert client.place_order.await_count == 1
    nfo = dataclasses.replace(make_event(kind, f"R4F_A2A_NFO_{kind}"), exchange="NFO", instrument_id="74056", product_type="LONGTERM")
    client = client_returning(ok("BRK_NFO"))
    assert (await run(nfo, {str(f.id): client}))[str(f.id)]["status"] == "SUCCESS"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_with_the_flag_off_execution_is_exactly_as_before(db_session, mixed, monkeypatch, kind):
    """Flag off (the default): the planner is never consulted, the Groww Child is not skipped, and no skip audit is written."""
    assert settings.CANONICAL_MAPPING_ENFORCED is False

    def boom(**kw):
        raise AssertionError("the planner must not run when enforcement is off")

    monkeypatch.setattr("app.services.copy_engine.pipeline.check_child_order", boom)
    mid = f"R4F_OFF_{kind}"
    event = dataclasses.replace(make_event(kind, mid), exchange="NFO", instrument_id="74056")
    out = await run(event, mixed["clients"])
    a, g, b = mixed["a"], mixed["g"], mixed["b"]
    assert out[str(a.id)]["status"] == "SUCCESS" and out[str(g.id)]["status"] == "SUCCESS"      # Groww is NOT skipped
    assert out[str(b.id)] == {"status": "REJECTED", "reasons": ["FUNDS_FETCH_FAILED"]}
    assert mixed["clients"][str(g.id)].place_order.await_count == 1
    assert await skip_audits(mid) == []
    by = await trades_by_follower(mid)
    assert by[g.id][0].status == CopyTradeStatus.SUBMITTED


@pytest.mark.asyncio
async def test_a_planner_error_fails_closed_for_every_child_and_sends_nothing(db_session, mixed, monkeypatch):
    enforced(monkeypatch)

    def boom(**kw):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr("app.services.copy_engine.pipeline.check_child_order", boom)
    mid = "R4F_ERR"
    out = await run(make_event("LIMIT", mid), mixed["clients"])
    for child in ("a", "g", "b"):
        assert out[str(mixed[child].id)] == {"status": "SKIPPED", "reason": "INSTRUMENT_UNMAPPED", "detail": "PLANNER_CHECK_ERROR"}
        assert mixed["clients"][str(mixed[child].id)].place_order.await_count == 0


@pytest.mark.asyncio
async def test_the_master_broker_is_read_once_and_a_failure_fails_closed(db_session, sample_accounts, monkeypatch):
    pl = pipeline()
    master = sample_accounts["master"]
    assert await pl._master_broker_id(master.id) == ALICE
    monkeypatch.setattr("app.services.copy_engine.pipeline.AsyncSessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert await pl._master_broker_id(master.id) == ALICE                       # cached: an account's broker never changes
    assert await pipeline()._master_broker_id(master.id) is None                # not cached, database down: unknown -> fail closed


@pytest.mark.asyncio
async def test_an_unknown_master_account_has_no_broker(db_session):
    assert await pipeline()._master_broker_id(uuid.uuid4()) is None
    assert await pipeline()._master_broker_id("not-a-uuid") is None
