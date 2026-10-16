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
})
# Keys that look enum-like but may carry free text or identifiers: never kept.
NEVER_KEEP = frozenset({
    "text", "message", "jwttoken", "refreshtoken", "feedtoken", "orderid", "uniqueorderid", "fillid",
    "clientcode", "name", "email", "mobileno", "panno", "tradingsymbol", "symboltoken", "symbolname",
})
MAX_ROWS = 50


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
