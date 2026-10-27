"""
Read-only broker-pair matrix (dynamic Master/Child P0).

GET /brokers/pairs answers, for every Master broker x Child broker the registry knows, whether the order-time pair gate (R5,
brokers/pairs.check_pair) accepts it right now, and if not, which requirement is missing. It uses the one existing policy through
strategy_readiness.pair_readiness - there is no second list here - and it reflects the CURRENT flags, contracts and evidence.
Nothing is written, no broker is called, no credential is read, and no pair is enabled by being listed.
"""
from typing import Any, Dict, List

from fastapi import APIRouter

from app.services import strategy_readiness
from app.services.brokers.pairs import SUPPORTED_PAIRS
from app.services.brokers.registry import all_broker_specs

router = APIRouter(prefix="/brokers", tags=["Brokers (read-only)"])


@router.get("/pairs")
async def get_broker_pairs() -> Dict[str, Any]:
    specs = all_broker_specs()
    ids = sorted(specs)
    brokers: List[Dict[str, Any]] = [
        {
            "id": b, "support": specs[b].support.value, "as_master": specs[b].capabilities.as_master,
            "as_child": specs[b].capabilities.as_child, "reason": specs[b].reason,
        }
        for b in ids
    ]
    pairs: List[Dict[str, Any]] = []
    for master in ids:
        for child in ids:
            p = strategy_readiness.pair_readiness(master, child)
            pairs.append({
                "master": master, "child": child, "supported": p["supported"] is True, "code": p["code"],
                "requirement": p["requirement"], "detail": p["detail"], "message": p["message"],
            })
    supported = [{"master": p["master"], "child": p["child"]} for p in pairs if p["supported"]]
    return {
        "success": True,
        "data": {
            "brokers": brokers,
            "pairs": pairs,
            "supported": supported,
            "allowlist": [{"master": m, "child": c, "evidence": e} for (m, c), e in sorted(SUPPORTED_PAIRS.items())],
            "note": ("A pair is supported only when it is on the explicit allowlist AND the registry role gates and capability "
                     "evidence still back it. Listing a pair here enables nothing; unsupported pairs are refused at membership, at "
                     "the Master switch, and skipped at order time as BROKER_PAIR_NOT_SUPPORTED."),
        },
    }
