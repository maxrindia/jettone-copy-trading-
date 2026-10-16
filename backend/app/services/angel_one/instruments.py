"""
Angel One instrument mapping (equity only)
==========================================
Angel One orders need Angel One's own `symboltoken`. A token from another
broker (e.g. the Alice Blue instrument id carried by a copied Master event) is
meaningless here and is NEVER used: the token is always resolved from Angel
One's instrument master by (exchange, symbol).

The instrument master is loaded from a file/records the owner supplies, or
downloaded ONLY from ANGEL_ONE_SCRIP_MASTER_URL when it is configured. Its field
names (token, symbol, name, exch_seg, instrumenttype) follow Angel One's
published scrip master but are treated as unverified: a record missing any of
them is skipped, and resolution refuses unless exactly one instrument matches.
F&O is not mapped (no lot-size validation exists), so derivatives fail closed.
"""

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.alice_blue.instruments import InstrumentResolutionError

_EXCHANGES = {"NSE": "NSE", "NSE_CM": "NSE", "BSE": "BSE", "BSE_CM": "BSE"}


class AngelInstrumentStore:
    def __init__(self) -> None:
        # (exchange, tradingsymbol upper) -> {token, ...}
        self._by_symbol: Dict[Tuple[str, str], set] = {}
        self.size = 0

    def load_records(self, records: Iterable[Dict[str, Any]]) -> int:
        added = 0
        for r in records:
            token = str(r.get("token") or "").strip()
            symbol = str(r.get("symbol") or "").strip().upper()
            exch = _EXCHANGES.get(str(r.get("exch_seg") or "").strip().upper())
            if not (token and symbol and exch):
                continue
            self._by_symbol.setdefault((exch, symbol), set()).add(token)
            added += 1
        self.size += added
        return added

    def load_file(self, path: str) -> int:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise InstrumentResolutionError("Angel One instrument master must be a JSON list.")
        return self.load_records(data)

    def resolve(self, exchange: str, symbol: str) -> Tuple[str, str]:
        """(symboltoken, tradingsymbol) for an equity, or InstrumentResolutionError."""
        exch = _EXCHANGES.get(str(exchange or "").strip().upper())
        if exch is None:
            raise InstrumentResolutionError(
                f"Angel One instrument mapping supports NSE/BSE equity only (got {exchange!r})."
            )
        if not self.size:
            raise InstrumentResolutionError("Angel One instrument master is not loaded.")
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise InstrumentResolutionError("Empty symbol.")
        found: List[Tuple[str, str]] = []
        for candidate in (sym, f"{sym}-EQ") if not sym.endswith("-EQ") else (sym,):
            for token in self._by_symbol.get((exch, candidate), ()):
                found.append((token, candidate))
        distinct = {t for t, _ in found}
        if len(distinct) != 1:
            raise InstrumentResolutionError(
                f"Angel One instrument for {exch}:{sym} is "
                + ("not found." if not distinct else f"ambiguous ({len(distinct)} matches).")
            )
        return found[0]


# Process-wide store; populated at startup only when Angel One is enabled.
store = AngelInstrumentStore()
