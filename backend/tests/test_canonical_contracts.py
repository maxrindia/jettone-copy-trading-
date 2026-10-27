"""Canonical contract identity and per-broker resolution: fixtures for all four brokers; unknown / stale / ambiguous mappings refuse."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.instruments.catalogs import ZerodhaCatalog
from app.services.instruments.contracts import (
    CanonicalContract, ResolutionVerdict as R, make_contract, resolve_for_broker,
)
from app.services.instruments.normalizer import normalize_equity
from app.services.zerodha.instruments import ZerodhaInstrumentStore
from zerodha_helpers import CSV_HEADER, CSV_ROWS

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)


def store(rows=None, at=NOW):
    s = ZerodhaInstrumentStore(clock=lambda: at)
    s.load_csv_text("\n".join([CSV_HEADER] + (CSV_ROWS if rows is None else rows)))
    return s


def opt(strike="23500", kind="CE", expiry="2026-11-26"):
    return make_contract(kind, "nfo", "nifty", expiry, strike)


# ── identity ────────────────────────────────────────────────────────────────

def test_keys_are_disjoint_per_kind_and_include_every_derivative_term():
    keys = {opt().key, opt(kind="PE").key, opt(strike="23600").key, opt(expiry="2026-12-31").key,
            make_contract("FUT", "NFO", "NIFTY", "2026-11-26").key, make_contract("EQ", "NSE", "NIFTY").key,
            make_contract("EQ", "BSE", "NIFTY").key}
    assert len(keys) == 7
    assert opt().key == "CE|NFO|NIFTY|2026-11-26|23500" and opt("23500.0").key == opt().key


@pytest.mark.parametrize("args", [
    ("CE", "NFO", "NIFTY", "2026-11-26", None), ("PE", "NFO", "NIFTY", "2026-11-26", "0"), ("FUT", "NFO", "NIFTY", "2026-11-26", "100"),
    ("FUT", "NFO", "NIFTY", None, None), ("EQ", "NFO", "SBIN", None, None), ("EQ", "NSE", "SBIN", "2026-11-26", None),
    ("XX", "NSE", "SBIN", None, None), ("CE", "NFO", "", "2026-11-26", "100"), ("CE", "NFO", "NIFTY", "26-11-2026", "100"),
    ("CE", "NFO", "NIFTY", "2026-11-26", "NaN"), ("CE", "MCX", "GOLD", "2026-11-26", "100"),
])
def test_malformed_contracts_are_refused(args):
    assert make_contract(*args) is None


# ── Zerodha ─────────────────────────────────────────────────────────────────

def test_zerodha_resolves_equity_and_derivatives_but_never_as_verified():
    s = store()
    eq = resolve_for_broker("zerodha", make_contract("EQ", "NSE", "SBIN"), now=NOW, zerodha_store=s)
    assert eq.verdict is R.RESOLVED_UNVERIFIED and eq.broker_symbol == "SBIN" and eq.broker_token == "779521" and not eq.verified
    ce = resolve_for_broker("zerodha", opt(), now=NOW, zerodha_store=s)
    assert ce.verdict is R.RESOLVED_UNVERIFIED and ce.broker_symbol == "NIFTY26NOV23500CE" and ce.lot_size == 75 and not ce.verified
    fut = resolve_for_broker("zerodha", make_contract("FUT", "NFO", "NIFTY", "2026-12-31"), now=NOW, zerodha_store=s)
    assert fut.broker_symbol == "NIFTY26DECFUT"


def test_zerodha_unknown_ambiguous_stale_and_unloaded_all_refuse():
    s = store()
    assert resolve_for_broker("zerodha", opt("99999"), now=NOW, zerodha_store=s).verdict is R.UNMAPPED
    assert resolve_for_broker("zerodha", make_contract("EQ", "NSE", "NOSUCH"), now=NOW, zerodha_store=s).verdict is R.UNMAPPED
    dup = store(CSV_ROWS + ["13568999,52998,NIFTY26NOV23500CE2,NIFTY,0,2026-11-26,23500,0.05,75,CE,NFO-OPT,NFO"])
    assert resolve_for_broker("zerodha", opt(), now=NOW, zerodha_store=dup).verdict is R.AMBIGUOUS
    old = store(at=NOW - timedelta(days=3))
    assert resolve_for_broker("zerodha", opt(), now=NOW, zerodha_store=old).verdict is R.STALE
    assert resolve_for_broker("zerodha", opt(), now=NOW, zerodha_store=ZerodhaInstrumentStore()).verdict is R.CATALOG_NOT_LOADED
    twice = store()
    twice.load_csv_text("\n".join([CSV_HEADER] + CSV_ROWS[:1]))              # a second load: completeness no longer provable
    assert resolve_for_broker("zerodha", opt(), now=NOW, zerodha_store=twice).verdict is R.STALE


def test_a_future_cannot_be_resolved_as_an_option_or_with_a_wrong_expiry():
    s = store()
    assert resolve_for_broker("zerodha", make_contract("FUT", "NFO", "NIFTY", "2027-01-28"), now=NOW, zerodha_store=s).verdict is R.UNMAPPED
    assert resolve_for_broker("zerodha", opt(kind="PE", strike="23500"), now=NOW, zerodha_store=s).broker_symbol == "NIFTY26NOV23500PE"


# ── the other three brokers: explicit, never a silent success ──────────────

@pytest.mark.parametrize("broker", ["groww", "angelone", "alicebluenew", "dhan"])
def test_other_brokers_never_resolve_derivatives_here(broker):
    got = resolve_for_broker(broker, opt(), now=NOW)
    assert got.verdict is R.NOT_SUPPORTED and got.broker_token is None and not got.verified


@pytest.mark.parametrize("broker", ["groww", "angelone", "alicebluenew"])
def test_equity_for_the_other_brokers_is_not_resolved_by_this_module(broker):
    assert resolve_for_broker(broker, make_contract("EQ", "NSE", "SBIN"), now=NOW).verdict is R.NOT_SUPPORTED


def test_invalid_input_and_resolver_errors_never_map():
    bad = CanonicalContract("CE", "NFO", "NIFTY", None, None)
    assert resolve_for_broker("zerodha", bad, now=NOW).verdict is R.INVALID_INPUT
    assert resolve_for_broker("zerodha", None, now=NOW).verdict is R.INVALID_INPUT           # type: ignore[arg-type]


# ── Zerodha catalog (equity shadow comparison) ─────────────────────────────

def test_zerodha_catalog_exposes_equity_only_symbol_matches():
    s = store()
    master = normalize_equity("NSE", "SBIN", None).instrument
    view = ZerodhaCatalog(s).view_for(master)
    assert [c.trading_symbol for c in view.candidates] == ["SBIN"] and view.candidates[0].isin is None
    assert view.candidates[0].lot_size == 1 and view.loaded_at == NOW
    nifty = normalize_equity("NFO", "NIFTY26NOVFUT", None).instrument
    assert nifty is None or not ZerodhaCatalog(s).view_for(nifty).candidates
    assert ZerodhaCatalog(ZerodhaInstrumentStore()).view_for(master).candidates is None
