"""
Broker-neutral interfaces (R1 of docs/architecture/multi-broker-copy-trading, 02 §2.2).

Structural `Protocol`s describing what the engine already asks of a broker client. They are declared AFTER the
fact: the existing Alice Blue, Groww and Angel One clients provide the methods they list without any change,
and nothing in the pipeline depends on these types yet (wiring is a later, separately scoped phase).

Two things these types do and do not prove
------------------------------------------
* `isinstance(client, SomeProtocol)` (they are `runtime_checkable`) checks that the NAMES exist. It says nothing
  about signatures: a class with the right names and wrong signatures passes (a bare mock does not, because the
  names are looked up statically). Signature conformance of the real clients is checked
  separately, with `inspect.signature`, in tests/test_broker_interfaces.py, including the call shapes the
  pipeline really uses.
* Structural conformance is not eligibility. Groww's and Angel One's clients expose `get_order_book`, so they
  satisfy `MasterOrderSource`, yet neither may be a Master: that role is decided by the registry gates and the
  contracts' `master_verified` flag, and recorded in the capability profile (brokers/profiles.py).

Known, unavoidable client differences (documented, not "fixed" in R1)
--------------------------------------------------------------------
* `place_order`: Alice names its parameter `order`; Groww and Angel One name it `req`. The pipeline always passes
  it positionally, so the Protocol makes it positional-only.
* `modify_order(product_type=...)`: the signature default is `ProductType.MIS` on Alice Blue but `None` on Groww
  and Angel One. The pipeline's modify calls never pass a product. Alice Blue's method body IGNORES the
  parameter - it is kept for logging context and no product field is sent in the modify payload - so the `MIS`
  default has no effect on what Alice receives. Groww's body does not use it either. Angel One's body REQUIRES a
  product and raises `AngelOneError` when it is None, so the current pipeline cannot modify an Angel One order;
  that must be resolved and tested before Angel One is enabled (it is disabled today). R1 changes neither the
  pipeline nor the clients.
* `cancel_order`: Angel One has an extra optional `variety` (default "NORMAL").
* `get_funds`: Alice Blue has an extra optional `max_cache_age_sec` (default 10).
* Failed reads: `get_positions_or_none` returns None on failure (an empty list is a successful empty book);
  `get_order_book` has no explicit "unknown" result and relies on exceptions or the client's own conventions.
  Treating a failed read as "no orders" is what the design forbids; the poller skips on exceptions.

Order book versus order lookup
------------------------------
`MasterOrderSource.get_order_book` READS THE WHOLE ORDER BOOK. `OrderStatusProvider` would QUERY ONE SPECIFIC
ORDER (by broker id or by client reference). No client exposes a public per-order query today. Groww has a private
helper, `GrowwClient._lookup_by_reference`, used internally to resolve an ambiguous create; being private it is
not a public capability and is not treated as one here (profile feature `ORDER_LOOKUP` is NOT_SUPPORTED).
`InstrumentMapper` is implemented by Groww and Angel One only; Alice Blue resolves through module functions.
"""
from decimal import Decimal
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from app.services.alice_blue.types import (
    FundsData,
    OrderRecord,
    OrderRequest,
    OrderResult,
    OrderType,
    Position,
    ProductType,
)


@runtime_checkable
class MasterOrderSource(Protocol):
    """Master detection by reading the broker's whole order book (what `MasterOrderPoller` calls)."""

    async def get_order_book(self) -> List[OrderRecord]: ...


@runtime_checkable
class ChildOrderExecutor(Protocol):
    """Place, modify and cancel a Child order. `place_order` returns an `OrderResult` whose `category` already
    separates SUCCESS / REJECTED / TIMEOUT / NETWORK_ERROR / UNKNOWN. The pipeline calls `place_order`
    positionally and `modify_order` / `cancel_order` with keyword arguments."""

    async def place_order(self, order: OrderRequest, /) -> OrderResult: ...

    async def cancel_order(
        self, broker_order_id: str, symbol: str = "", exchange: str = "NSE",
    ) -> OrderResult: ...

    async def modify_order(
        self,
        broker_order_id: str,
        quantity: int,
        price: Decimal,
        trigger_price: Decimal = Decimal("0"),
        order_type: OrderType = OrderType.LIMIT,
        symbol: str = "",
        exchange: str = "NSE",
        product_type: Optional[ProductType] = None,
    ) -> OrderResult: ...


@runtime_checkable
class PositionProvider(Protocol):
    """`None` means the broker could not be read; `[]` means a successful read of an empty book. The SELL
    guard depends on that distinction."""

    async def get_positions_or_none(self) -> Optional[List[Position]]: ...


@runtime_checkable
class FundsProvider(Protocol):
    async def get_funds(self) -> FundsData: ...


@runtime_checkable
class InstrumentMapper(Protocol):
    """A client that maps instruments itself with its own broker's instrument master. Alice Blue does not
    implement this (it trusts the Master's token and uses module-level resolvers); Groww and Angel One do.
    `get_available_long` already branches on this method's presence."""

    async def resolve_instrument(self, exchange: str, symbol: str) -> Tuple[str, str]: ...


@runtime_checkable
class OrderStatusProvider(Protocol):
    """Query ONE specific order, by broker order id or by the caller's client reference (not the whole order
    book; see `MasterOrderSource`). NO client exposes a public method like this today. Unknown-outcome recovery
    therefore relies on the order book, the sweeper, and (Groww) a private reference lookup (design 03 §3.6)."""

    async def get_order(self, broker_order_id: str) -> Optional[OrderRecord]: ...

    async def find_by_client_reference(self, reference: str) -> Optional[OrderRecord]: ...
