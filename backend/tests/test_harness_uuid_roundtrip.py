"""
Harness check: a UUID must round-trip through the test database whatever its hex digits are.

The models declare `sqlalchemy.dialects.postgresql.UUID` columns. On SQLite that type renders as the bare declared type `UUID`, which
SQLite gives NUMERIC affinity (a declared type containing none of CHAR / CLOB / TEXT / BLOB / INT / REAL / FLOA / DOUB). A NUMERIC column
converts any text that looks like a number into INTEGER or REAL on insert. A uuid4 hex string is 32 hex digits, and about one in a million are
"numeric looking" (all decimal digits, or digits followed by `e` and digits). Such an id is silently stored as a number (the largest
overflow to `inf`) and reading the row back fails with `UUID(hex=inf)` - an intermittent full-suite failure that no isolated rerun reproduces.
PostgreSQL has a native UUID type, so only the SQLite test database (never production) is affected. tests/conftest.py now renders UUID as
CHAR(32) on SQLite; these checks use crafted, valid uuid4 values so the problem is reproduced deterministically instead of by luck.
"""
import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

from app.models.accounts import Account, AccountType
from app.models.base import Base
from conftest import test_engine


def _uuid4_from(first12, tail16):
    """A valid RFC 4122 version-4 uuid: 12 free hex digits, version 4, 3 free, a variant digit in 89ab, then 15 free."""
    hex32 = first12 + "4" + tail16[:3] + tail16[3] + tail16[4:]
    value = uuid.UUID(hex=hex32)
    assert value.version == 4 and value.variant == uuid.RFC_4122 and value.hex == hex32, hex32
    return value


# 12 + 1 (version) + 3 + 1 (variant) + 15 = 32 hex digits
NUMERIC_LOOKING = {
    "overflows_to_inf": _uuid4_from("000000000000", "0008" + "0000000000e9999"),   # float(hex) == inf
    "finite_exponent": _uuid4_from("000000000000", "0008" + "000000000000e12"),      # float(hex) is finite
    "all_digits": _uuid4_from("123456789012", "4568" + "123456789012345"),           # becomes an INTEGER / REAL
    "all_digits_leading_zeros": _uuid4_from("000000000000", "0008" + "000000000000001"),
}


def _sqlite_affinity(declared: str) -> str:
    """SQLite's documented rules (https://www.sqlite.org/datatype3.html section 3.1), applied in order."""
    d = declared.upper()
    if "INT" in d:
        return "INTEGER"
    if "CHAR" in d or "CLOB" in d or "TEXT" in d:
        return "TEXT"
    if "BLOB" in d or not d:
        return "BLOB"
    if "REAL" in d or "FLOA" in d or "DOUB" in d:
        return "REAL"
    return "NUMERIC"


def test_the_crafted_ids_really_are_numeric_looking_valid_uuid4s():
    assert float(NUMERIC_LOOKING["overflows_to_inf"].hex) == float("inf")
    assert 0 < float(NUMERIC_LOOKING["finite_exponent"].hex) < float("inf")
    assert NUMERIC_LOOKING["all_digits"].hex.isdigit()


def test_no_uuid_column_gets_numeric_affinity_on_sqlite():
    """Every UUID column (primary and foreign keys) must be declared as a TEXT-affinity type in the SQLite test DDL."""
    offenders = []
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        for column in table.columns:
            if column.type.__class__.__name__.upper() != "UUID":
                continue
            m = re.search(r"^\s*" + re.escape(column.name) + r"\s+([A-Za-z0-9_()]+)", ddl, re.M)
            declared = m.group(1) if m else ""
            if _sqlite_affinity(declared) != "TEXT":
                offenders.append((table.name, column.name, declared, _sqlite_affinity(declared)))
    assert not offenders, offenders


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(NUMERIC_LOOKING))
async def test_a_numeric_looking_uuid_round_trips_unchanged(db_session, name):
    account_id = NUMERIC_LOOKING[name]
    account = Account(id=account_id, account_type=AccountType.MASTER, display_name=f"roundtrip {name}",
                      alice_user_id=f"AB_RT_{account_id.hex[:12]}", is_active=True)
    db_session.add(account)
    await db_session.commit()
    db_session.expire_all()
    await db_session.refresh(account)                      # the call that failed with UUID(hex=inf)
    assert account.id == account_id
    fetched = await db_session.get(Account, account_id)
    assert fetched is not None and fetched.id == account_id
    if test_engine.dialect.name == "sqlite":
        async with test_engine.connect() as conn:
            stored = (await conn.execute(text("select typeof(id) from accounts"))).scalars().all()
        assert stored == ["text"], stored                  # never converted to a number
