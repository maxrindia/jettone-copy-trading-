"""
Offline NSE token -> ISIN crosswalk (R4h prototype).

Builds an immutable, provenance-tagged snapshot from ONE official NSE security-master file that the OPERATOR supplies
(`NSE_CM_security_DDMMYYYY.csv` or `.csv.gz`) and answers `(exchange, token)` lookups. It is a prototype for testing identity
resolution: nothing imports it, nothing in the trading path can reach it, and it can neither download data nor place orders.

Operator use
------------
Download the day's file yourself from the exchange (reference location, never fetched by this code:
`OFFICIAL_URL_TEMPLATE`), then:

    python -m app.services.instruments.nse_crosswalk path\\to\\NSE_CM_security_22102026.csv.gz [--lookup 10794]

Please check NSE's Terms of Use / Data Usage policy for automated retrieval before scripting the download.

What a lookup means
-------------------
The key is `("NSE", FinInstrmId)`. `FinInstrmId` is the identifier that, in the files examined (R4g), equals Alice Blue's
contract-master `Token` for the same NSE instrument; its formal definition has NOT been confirmed from NSE documentation, and
`instrumentId == Token` for a real Master order is not yet confirmed either. There is deliberately no symbol-keyed lookup: the
same symbol exists in several series with different ISINs (MOTHERSON EQ / D1, ELECTCAST EQ / W1).

Statuses (a result carries an ISIN only for MATCH; first failing rule wins)
  UNMAPPED         exchange not covered, malformed token, token not in the snapshot, or its row was rejected
  STALE_SNAPSHOT   the snapshot is older than the freshness rule allows (or dated in the future)
  CONFLICT         the file holds differing rows for the token; ALL are kept as evidence and no winner is chosen
  DELETED          the exchange's deletion flag (DelFlg) is Y
  INVALID_ISIN     blank, placeholder / malformed, or a failed ISO 6166 check digit
  MATCH            one active record with a valid ISIN in a fresh snapshot

Freshness: a snapshot is stale when its date is in the future, or when MORE THAN ONE weekday (Mon-Fri, counted in IST) has
passed since it. Friday's file is therefore fresh on Monday; Thursday's is not. There is no exchange holiday calendar in this
project, so a holiday can cause a false STALE (fail closed) but never a false "fresh".
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.services.instruments.normalizer import is_valid_isin

EXCHANGE = "NSE"
# Documentation only: the operator downloads the file; this module never requests it.
OFFICIAL_URL_TEMPLATE = "https://nsearchives.nseindia.com/content/cm/NSE_CM_security_{DDMMYYYY}.csv.gz"

REQUIRED_COLUMNS = ("FinInstrmId", "TckrSymb", "SctySrs", "ISIN", "DelFlg")
OPTIONAL_COLUMNS = ("FinInstrmNm", "NewBrdLotQty", "UpdDt")
MAX_AGE_TRADING_DAYS = 1
MAX_DECOMPRESSED_BYTES = 200_000_000
UPDATE_TOLERANCE_DAYS = 1
IST = timezone(timedelta(hours=5, minutes=30))          # India has no daylight saving time
NSE_EPOCH = datetime(1980, 1, 1)                       # UpdDt counts seconds from here (inferred from the sample, not documented)

_FILENAME_RE = re.compile(r"^NSE_CM_security_(\d{2})(\d{2})(\d{4})\.csv(\.gz)?$", re.I)
_TOKEN_RE = re.compile(r"^[1-9][0-9]{0,9}$")
_ISIN_SHAPE_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


class CrosswalkFileError(Exception):
    """The supplied file cannot be used as a snapshot at all (nothing is loaded)."""


class CrosswalkStatus(str, Enum):
    MATCH = "MATCH"
    UNMAPPED = "UNMAPPED"
    INVALID_ISIN = "INVALID_ISIN"
    CONFLICT = "CONFLICT"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    DELETED = "DELETED"


@dataclass(frozen=True)
class SecurityRecord:
    exchange: str
    token: str
    symbol: str
    series: str
    name: str
    isin: str                      # as supplied (trimmed, upper-case); may be blank / a placeholder
    lot_size: Optional[int]
    deleted: bool

    def identity(self) -> Tuple[str, str, str, bool, Optional[int]]:
        return (self.symbol, self.series, self.isin, self.deleted, self.lot_size)


@dataclass(frozen=True)
class SnapshotProvenance:
    source_filename: str
    source_url: Optional[str]                  # declared by the operator, never fetched
    snapshot_date: date
    sha256: str                                # of the file exactly as supplied (the .gz bytes for a .gz)
    content_sha256: str                        # of the decompressed CSV bytes
    compressed: bool
    loaded_at: datetime
    rows_total: int
    rows_indexed: int
    rows_rejected: int
    rejected_by_reason: Tuple[Tuple[str, int], ...]
    identical_duplicates: int
    conflicting_tokens: int
    invalid_isin_rows: int
    deleted_rows: int
    symbol_series_collisions: int              # (symbol, series) pairs carried by more than one token (diagnostic only)
    observed_update_date: Optional[date]       # from the newest UpdDt; an inference, used only as a sanity check


@dataclass(frozen=True)
class CrosswalkResult:
    status: CrosswalkStatus
    reason: str
    token: Any
    exchange: str
    isin: Optional[str] = None                 # set ONLY when status is MATCH
    record: Optional[SecurityRecord] = None    # diagnostics; a non-MATCH result must not be used as an identity
    conflicting: Tuple[SecurityRecord, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status is CrosswalkStatus.MATCH


def weekdays_since(snapshot_date: date, today: date) -> int:
    """Mon-Fri days in (snapshot_date, today]; negative when the snapshot is dated after today."""
    if snapshot_date > today:
        return -1
    n, d = 0, snapshot_date
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def isin_problem(isin: str) -> Optional[str]:
    """None for a valid ISIN, else why not."""
    if not isin:
        return "BLANK_ISIN"
    if not _ISIN_SHAPE_RE.match(isin):
        return "PLACEHOLDER_OR_MALFORMED_ISIN"
    return None if is_valid_isin(isin) else "BAD_CHECK_DIGIT"


class NseCrosswalk:
    """Immutable token-indexed snapshot. Build it with `load_nse_security_master`."""

    def __init__(self, records: Mapping[Tuple[str, str], SecurityRecord],
                 conflicts: Mapping[Tuple[str, str], Tuple[SecurityRecord, ...]],
                 rejected_tokens: Mapping[Tuple[str, str], str], provenance: SnapshotProvenance) -> None:
        self._records = MappingProxyType(dict(records))
        self._conflicts = MappingProxyType(dict(conflicts))
        self._rejected = MappingProxyType(dict(rejected_tokens))
        self.provenance = provenance

    def __len__(self) -> int:
        return len(self._records)

    def age_trading_days(self, now: datetime) -> int:
        return weekdays_since(self.provenance.snapshot_date, _ist_today(now))

    def is_stale(self, now: datetime) -> bool:
        age = self.age_trading_days(now)
        return age < 0 or age > MAX_AGE_TRADING_DAYS

    def lookup(self, token: Any, *, now: datetime, exchange: str = EXCHANGE) -> CrosswalkResult:
        ex = str(exchange or "").strip().upper()
        if ex != EXCHANGE:
            return CrosswalkResult(CrosswalkStatus.UNMAPPED, "EXCHANGE_NOT_COVERED", token, ex)
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            return CrosswalkResult(CrosswalkStatus.UNMAPPED, "MALFORMED_TOKEN", token, ex)
        age = self.age_trading_days(now)
        if age < 0:
            return CrosswalkResult(CrosswalkStatus.STALE_SNAPSHOT, "FUTURE_SNAPSHOT", token, ex)
        if age > MAX_AGE_TRADING_DAYS:
            return CrosswalkResult(CrosswalkStatus.STALE_SNAPSHOT, f"AGE_{age}_TRADING_DAYS", token, ex)
        key = (ex, token)
        if key in self._conflicts:
            return CrosswalkResult(CrosswalkStatus.CONFLICT, "DIFFERING_ROWS_FOR_TOKEN", token, ex, conflicting=self._conflicts[key])
        record = self._records.get(key)
        if record is None:
            rejected = self._rejected.get(key)
            reason = f"ROW_REJECTED:{rejected}" if rejected else "TOKEN_NOT_IN_SNAPSHOT"
            return CrosswalkResult(CrosswalkStatus.UNMAPPED, reason, token, ex)
        if record.deleted:
            return CrosswalkResult(CrosswalkStatus.DELETED, "DELFLG_Y", token, ex, record=record)
        problem = isin_problem(record.isin)
        if problem:
            return CrosswalkResult(CrosswalkStatus.INVALID_ISIN, problem, token, ex, record=record)
        return CrosswalkResult(CrosswalkStatus.MATCH, "OK", token, ex, isin=record.isin, record=record)


def _ist_today(now: datetime) -> date:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("`now` must be a timezone-aware datetime (the clock is injected, never read implicitly)")
    return now.astimezone(IST).date()


def _filename_date(name: str) -> Tuple[Optional[date], bool]:
    m = _FILENAME_RE.match(name)
    if not m:
        return None, False
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day), bool(m.group(4))
    except ValueError as exc:
        raise CrosswalkFileError(f"the date in the filename is not a real date: {name}") from exc


def _read_bytes(path: Path) -> Tuple[bytes, bytes, bool]:
    """(supplied bytes, decompressed bytes, was_gzip). Whether it is gzip is decided by the content, then checked against the name."""
    try:
        supplied = path.read_bytes()
    except OSError as exc:
        raise CrosswalkFileError(f"cannot read the file: {exc}") from exc
    is_gz = supplied[:2] == b"\x1f\x8b"
    if path.name.lower().endswith(".gz") != is_gz:
        raise CrosswalkFileError("FILENAME_CONTENT_MISMATCH: the .gz extension and the file content disagree")
    if not is_gz:
        return supplied, supplied, False
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(supplied)) as fh:
            content = fh.read(MAX_DECOMPRESSED_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise CrosswalkFileError(f"the gzip content is unreadable: {exc}") from exc
    if len(content) > MAX_DECOMPRESSED_BYTES:
        raise CrosswalkFileError("the decompressed file exceeds the size limit")
    return supplied, content, True


def load_nse_security_master(
    path: Any,
    *,
    snapshot_date: Optional[date] = None,
    source_url: Optional[str] = None,
    loaded_at: Optional[datetime] = None,
    verify_update_timestamps: bool = True,
) -> NseCrosswalk:
    """Parse an operator-supplied local file into a snapshot. Raises CrosswalkFileError when the file cannot be used."""
    text_path = str(path)
    if re.match(r"^[a-z][a-z0-9+.\-]*://", text_path, re.I):
        raise CrosswalkFileError("a URL was given; this utility reads a local file the operator has downloaded and never fetches")
    p = Path(text_path)
    if not p.is_file():
        raise CrosswalkFileError(f"not a file: {p.name}")
    from_name, _ = _filename_date(p.name)
    if from_name and snapshot_date and from_name != snapshot_date:
        raise CrosswalkFileError(f"INCONSISTENT_SNAPSHOT_DATE: filename says {from_name}, the operator says {snapshot_date}")
    resolved = snapshot_date or from_name
    if resolved is None:
        raise CrosswalkFileError("the snapshot date is unknown: use the official file name or pass snapshot_date explicitly")

    supplied, content, compressed = _read_bytes(p)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CrosswalkFileError("the file is not valid UTF-8 text") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = reader.fieldnames or []
    if len(set(header)) != len(header):
        raise CrosswalkFileError("DUPLICATE_HEADER_COLUMNS")
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise CrosswalkFileError(f"MISSING_REQUIRED_COLUMNS: {', '.join(missing)}")

    rejected_reason: Dict[str, int] = {}
    rejected_tokens: Dict[Tuple[str, str], str] = {}
    by_token: Dict[str, List[SecurityRecord]] = {}
    rows_total = 0
    newest_update = -1

    def reject(reason: str, token: Optional[str] = None) -> None:
        rejected_reason[reason] = rejected_reason.get(reason, 0) + 1
        if token is not None and _TOKEN_RE.match(token):
            rejected_tokens.setdefault((EXCHANGE, token), reason)

    for row in reader:
        rows_total += 1
        if None in row or any(row.get(c) is None for c in REQUIRED_COLUMNS):
            reject("MALFORMED_ROW")
            continue
        token = row["FinInstrmId"]
        if not _TOKEN_RE.match(token):
            reject("MALFORMED_TOKEN")
            continue
        symbol, series = row["TckrSymb"].strip(), row["SctySrs"].strip()
        if not symbol:
            reject("MISSING_SYMBOL", token)
            continue
        if not series:
            reject("MISSING_SERIES", token)
            continue
        flag = row["DelFlg"].strip().upper()
        if flag not in ("Y", "N"):
            reject("UNKNOWN_DELFLG", token)
            continue
        lot_raw = (row.get("NewBrdLotQty") or "").strip()
        upd_raw = (row.get("UpdDt") or "").strip()
        if upd_raw.isdigit():
            newest_update = max(newest_update, int(upd_raw))
        by_token.setdefault(token, []).append(SecurityRecord(
            exchange=EXCHANGE, token=token, symbol=symbol, series=series, name=(row.get("FinInstrmNm") or "").strip(),
            isin=row["ISIN"].strip().upper(), lot_size=int(lot_raw) if lot_raw.isdigit() else None, deleted=(flag == "Y"),
        ))

    if rows_total == 0:
        raise CrosswalkFileError("the file has a header but no data rows")

    observed: Optional[date] = None
    if newest_update >= 0:
        try:
            observed = (NSE_EPOCH + timedelta(seconds=newest_update)).date()
        except OverflowError:
            observed = None
        if verify_update_timestamps and (observed is None or abs((observed - resolved).days) > UPDATE_TOLERANCE_DAYS):
            raise CrosswalkFileError(
                f"UPDATE_TIMESTAMPS_INCONSISTENT: the newest UpdDt decodes to {observed}, the snapshot is dated {resolved}")

    records: Dict[Tuple[str, str], SecurityRecord] = {}
    conflicts: Dict[Tuple[str, str], Tuple[SecurityRecord, ...]] = {}
    identical_duplicates = 0
    for token in sorted(by_token, key=int):
        rows = by_token[token]
        if len({r.identity() for r in rows}) > 1:
            conflicts[(EXCHANGE, token)] = tuple(rows)
            continue
        identical_duplicates += len(rows) - 1
        records[(EXCHANGE, token)] = rows[0]

    pairs: Dict[Tuple[str, str], int] = {}
    for r in records.values():
        pairs[(r.symbol, r.series)] = pairs.get((r.symbol, r.series), 0) + 1
    provenance = SnapshotProvenance(
        source_filename=p.name, source_url=source_url, snapshot_date=resolved,
        sha256=hashlib.sha256(supplied).hexdigest(), content_sha256=hashlib.sha256(content).hexdigest(), compressed=compressed,
        loaded_at=loaded_at if loaded_at is not None else datetime.now(timezone.utc),
        rows_total=rows_total, rows_indexed=len(records), rows_rejected=sum(rejected_reason.values()),
        rejected_by_reason=tuple(sorted(rejected_reason.items())), identical_duplicates=identical_duplicates,
        conflicting_tokens=len(conflicts), invalid_isin_rows=sum(1 for r in records.values() if isin_problem(r.isin)),
        deleted_rows=sum(1 for r in records.values() if r.deleted),
        symbol_series_collisions=sum(1 for n in pairs.values() if n > 1), observed_update_date=observed,
    )
    return NseCrosswalk(records, conflicts, rejected_tokens, provenance)


def _result_json(r: CrosswalkResult) -> Dict[str, Any]:
    return {"status": r.status.value, "reason": r.reason, "token": r.token, "exchange": r.exchange, "isin": r.isin,
            "record": None if r.record is None else r.record.__dict__, "conflicting": [c.__dict__ for c in r.conflicting]}


def main(argv: Optional[Sequence[str]] = None, *, now: Optional[datetime] = None) -> int:
    ap = argparse.ArgumentParser(description="Offline NSE token -> ISIN crosswalk (reads a local file; never downloads).")
    ap.add_argument("file", help="local NSE_CM_security_DDMMYYYY.csv or .csv.gz you downloaded")
    ap.add_argument("--lookup", action="append", default=[], metavar="TOKEN", help="token to look up (repeatable)")
    ap.add_argument("--snapshot-date", help="YYYY-MM-DD, only when the file name does not carry the date")
    ap.add_argument("--source-url", help="where you downloaded it from (recorded, never fetched)")
    args = ap.parse_args(argv)
    try:
        snap = date.fromisoformat(args.snapshot_date) if args.snapshot_date else None
        cw = load_nse_security_master(args.file, snapshot_date=snap, source_url=args.source_url)
    except (CrosswalkFileError, ValueError) as exc:
        print(f"UNUSABLE: {exc}", file=sys.stderr)
        return 2
    clock = now or datetime.now(timezone.utc)
    p = cw.provenance
    out: Dict[str, Any] = {"provenance": {**p.__dict__, "snapshot_date": p.snapshot_date.isoformat(), "loaded_at": p.loaded_at.isoformat(),
                                          "observed_update_date": p.observed_update_date.isoformat() if p.observed_update_date else None},
                           "stale": cw.is_stale(clock), "lookups": [_result_json(cw.lookup(t, now=clock)) for t in args.lookup]}
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
