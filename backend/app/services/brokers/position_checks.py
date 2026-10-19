"""Consistency checks on a parsed position book, shared by the non-Alice adapters."""
from typing import Iterable, Optional, Tuple

from app.services.alice_blue.types import Position


def duplicate_position_key(positions: Iterable[Position]) -> Optional[Tuple[str, str, str]]:
    """The (exchange, instrument, product) of the first instrument listed more than once, else None.

    A position book lists one row per instrument and product. Two rows for the same one cannot be
    told apart (stale vs current? long and short legs? a paging overlap?), and the SELL gate sums every
    matching row, so such a book is ambiguous and must be treated as unknown, never combined.
    """
    seen = set()
    for pos in positions:
        symbol = str(getattr(pos, "symbol", "") or "").upper().strip()
        if symbol.endswith("-EQ"):
            symbol = symbol[:-3]
        key = (
            str(getattr(pos, "exchange", "") or "").upper().strip(),
            symbol,
            str(getattr(pos, "product_type", "") or "").upper().strip(),
        )
        if key in seen:
            return key
        seen.add(key)
    return None
