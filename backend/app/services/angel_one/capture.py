"""
Redaction for the supervised Angel One read-only capture (scripts/angel_capture.py).

The capture exists only to learn the SHAPE of three read responses (order book,
positions, RMS/funds) so that angel_one/contract.py can be filled in from facts.
It records, per endpoint: HTTP status, the top-level keys, and for each row the
key names with their value TYPES. The only values kept are those of an explicit
allowlist of enum-like keys (status, product type, ...), which identify the
vocabulary the contract needs. Every other value - amounts, quantities, ids,
names, tokens - is replaced by its type, so the artefact can be reviewed and
shared without exposing the account.
"""

from typing import Any, Dict, List

# Keys whose VALUES are a small fixed vocabulary, not account data.
ENUM_KEYS = frozenset({
    "status", "orderstatus", "producttype", "product", "ordertype", "transactiontype",
    "exchange", "exch_seg", "variety", "duration", "instrumenttype", "optiontype", "positiontype",
    # Groww's vocabularies (Annexures page)
    "order_status", "order_type", "transaction_type", "segment", "amo_status", "validity", "instrument_type",
    "trade_status", "iskeyactive",
})
# Keys that look enum-like but may carry free text or identifiers: never kept.
NEVER_KEEP = frozenset({
    "text", "message", "jwttoken", "refreshtoken", "feedtoken", "orderid", "uniqueorderid", "fillid",
    "clientcode", "name", "email", "mobileno", "panno", "tradingsymbol", "symboltoken", "symbolname",
    "remark", "token", "tokenrefid", "sessionname", "groww_order_id", "order_reference_id", "trading_symbol",
})
MAX_ROWS = 50
# Non-sensitive paging counters/flags: their integer/boolean VALUES are kept, because the page base and the
# full-page behaviour can only be learned from them. Absent keys record nothing; no name here is a claim
# about any broker's API.
PAGING_KEYS = frozenset({
    "page", "page_no", "page_number", "pagenumber", "page_size", "pagesize", "total", "total_count", "totalcount",
    "total_pages", "totalpages", "count", "limit", "offset", "has_more", "has_next", "next_page",
})


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "numeric-string" if value.replace(".", "", 1).replace("-", "", 1).isdigit() else "string"
    if isinstance(value, list):
        return "list"
    return "object"


def redact_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in row.items():
        lk = str(key).lower()
        if lk in ENUM_KEYS and lk not in NEVER_KEEP and isinstance(value, str) and len(value) <= 40:
            out[key] = {"type": "string", "value": value}
        else:
            out[key] = {"type": _type_name(value)}
    return out


def _redact_rows(rows: List[Any]) -> Dict[str, Any]:
    """row_count is the TRUE number of rows (a full page shows up here even though only MAX_ROWS are described)."""
    described: List[Dict[str, Any]] = [redact_row(r) for r in rows[:MAX_ROWS] if isinstance(r, dict)]
    vocab: Dict[str, List[str]] = {}
    for r in described:
        for k, meta in r.items():
            if "value" in meta:
                vocab.setdefault(k, [])
                if meta["value"] not in vocab[k]:
                    vocab[k].append(meta["value"])
    return {"row_count": len(rows), "rows": described, "observed_vocabulary": {k: sorted(v) for k, v in vocab.items()}}


def _paging_values(container: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: v for k, v in container.items()
        if str(k).lower() in PAGING_KEYS and isinstance(v, (bool, int)) and not isinstance(v, float) and abs(int(v)) <= 1_000_000
    }


def redact_response(http_status: int, body: Any) -> Dict[str, Any]:
    """A shareable description of one endpoint response."""
    result: Dict[str, Any] = {"http_status": http_status, "top_level_keys": [], "data_kind": "none"}
    if not isinstance(body, dict):
        result["data_kind"] = f"non-object ({_type_name(body)})"
        return result
    result["top_level_keys"] = sorted(body.keys())
    for flag in ("status", "success"):
        if flag in body and isinstance(body[flag], bool):
            result[f"{flag}_flag"] = body[flag]
    data = body.get("data")
    if data is None and isinstance(body.get("payload"), (dict, list)):
        # Groww nests its data under `payload` (and an order list / position list one level further down).
        data = body["payload"]
        result["data_source"] = "payload"
    if isinstance(data, dict):
        lists = {k: _redact_rows(v) for k, v in data.items() if isinstance(v, list)}
        if lists:
            result["lists"] = lists
        paging = _paging_values(body) | _paging_values(data)
        if paging:
            result["paging_values"] = paging
    if isinstance(data, list):
        result["data_kind"] = "list"
        result["row_count"] = len(data)
        rows: List[Dict[str, Any]] = [redact_row(r) for r in data[:MAX_ROWS] if isinstance(r, dict)]
        result["rows"] = rows
        vocab: Dict[str, List[str]] = {}
        for r in rows:
            for k, meta in r.items():
                if "value" in meta:
                    vocab.setdefault(k, [])
                    if meta["value"] not in vocab[k]:
                        vocab[k].append(meta["value"])
        result["observed_vocabulary"] = {k: sorted(v) for k, v in vocab.items()}
    elif isinstance(data, dict):
        result["data_kind"] = "object"
        result["fields"] = redact_row(data)
    elif data is not None:
        result["data_kind"] = _type_name(data)
    return result
