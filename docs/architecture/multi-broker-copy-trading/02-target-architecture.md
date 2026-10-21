# 02 · Target architecture: one Master, N heterogeneous Children

Design principle: **additive boundaries around the existing pipeline.** The pipeline keeps its current
routing, pricing, SELL guard, funds, idempotency and reconciliation logic. What changes is *how it asks a broker
for something*: through narrow, capability-checked interfaces resolved per account, instead of duck-typed calls on
whatever client object is in the dict.

## 2.1 Target component diagram

```mermaid
flowchart TB
  subgraph UI["UI / API (Next.js → /api/v1 proxy → FastAPI)"]
    SM["Strategy management<br/>1 Master + N Children"]
    CAPUI["Capability display<br/>(per broker, role, segment)"]
  end

  subgraph Core["Engine core (broker-neutral)"]
    REG["Capability Registry<br/>broker × role × segment × feature<br/>+ verification status"]
    MEM["Membership Service<br/>role rules, one-strategy-per-account,<br/>compatibility check"]
    ING["Master Ingestion<br/>per-Master MasterOrderSource"]
    NORM["Normaliser<br/>→ NormalizedMasterEvent<br/>(canonical instrument key)"]
    ROUTE["Router / Fan-out<br/>per-Child eligibility"]
    PLAN["Per-Child Planner<br/>instrument map · qty · price rule"]
    RISK["Risk Engine<br/>SELL guard · funds · exposure · daily loss"]
    EXEC["Execution Coordinator<br/>idempotent submit, no blind resend"]
    REC["Reconciliation<br/>fill sync · stale sweep · position compare"]
  end

  subgraph Persist["Persistence"]
    PG[("Postgres<br/>orders · copy_trades (per-Child execution)<br/>audit_logs · system_events")]
    RD[("Redis<br/>sessions · dedupe · instrument cache")]
  end

  subgraph Adapters["Broker adapters (one per broker)"]
    A1["Alice Blue adapter<br/>Master+Child"]
    A2["Groww adapter<br/>Child (gated) · Master (unverified)"]
    A3["Angel One adapter<br/>disabled"]
    A4["Zerodha / 5paisa / …<br/>not implemented"]
  end

  SM --> MEM --> REG
  CAPUI --> REG
  ING --> NORM --> ROUTE --> PLAN --> RISK --> EXEC
  ROUTE --> REG
  PLAN --> REG
  EXEC --> PG
  ROUTE --> PG
  REC --> PG
  ING -->|MasterOrderSource| Adapters
  PLAN -->|InstrumentMapper| Adapters
  RISK -->|PositionProvider · FundsProvider| Adapters
  EXEC -->|ChildOrderExecutor| Adapters
  REC -->|OrderStatusProvider · PositionProvider| Adapters
  Adapters --> RD
```

### Fan-out rule
For each `NormalizedMasterEvent`, the Router builds **one independent work item per Child**. Each item runs in
its own task with its own capability check, instrument mapping, quantity, risk evaluation, `copy_trades` row and
broker call. A failure or outage in one item is recorded on that item only (existing `gather(return_exceptions=True)`
pattern, kept). No item's outcome changes another item's status, and no item is retried because a sibling failed.

## 2.2 UML class diagram (interfaces are design candidates)

```mermaid
classDiagram
  direction LR

  class BrokerAdapter {
    <<interface>>
    +broker_id: str
    +profile() CapabilityProfile
    +session_status() SessionStatus
  }
  class MasterOrderSource {
    <<interface>>
    +read_order_book() OrderBookSnapshot | Unknown
    +stream()? AsyncIterator~OrderUpdate~
  }
  class ChildOrderExecutor {
    <<interface>>
    +place(ChildOrderIntent) SubmitResult
    +modify(BrokerOrderRef, ModifyIntent) SubmitResult
    +cancel(BrokerOrderRef) SubmitResult
    +supports_client_reference() bool
  }
  class OrderStatusProvider {
    <<interface>>
    +get_order(BrokerOrderRef) OrderStatus | Unknown
    +find_by_client_reference(str) OrderStatus | NotFound | Unknown
  }
  class PositionProvider {
    <<interface>>
    +positions() PositionBook | Unknown
    +holdings()? HoldingBook | Unknown
  }
  class FundsProvider {
    <<interface>>
    +funds() Funds | Unknown
  }
  class InstrumentMapper {
    <<interface>>
    +to_canonical(BrokerInstrument) CanonicalInstrument | Unmapped
    +from_canonical(CanonicalInstrument) BrokerInstrument | Unmapped
  }

  class CapabilityProfile {
    +broker_id
    +roles: Map~Role, Verification~
    +segments: Map~SegmentKey, SegmentCaps~
    +features: Map~Feature, Verification~
    +gates: Map~str, bool~
  }
  class SegmentCaps {
    +exchange
    +segment  (CASH | FUT | OPT | COM | CUR)
    +products: Set
    +order_types: Set
    +verification: Verification
  }
  class Verification {
    <<enumeration>>
    VERIFIED_LIVE
    VERIFIED_DOCS
    UNVERIFIED
    NOT_SUPPORTED
  }
  class CanonicalInstrument {
    +exchange
    +segment
    +underlying
    +expiry?
    +strike?
    +option_type?
    +isin?
    +lot_size?
  }
  class NormalizedMasterEvent {
    +strategy_id
    +master_account_id
    +master_broker_id
    +master_order_id
    +event_kind (NEW | FILL | MODIFY | CANCEL | REJECT)
    +fill_sequence
    +side · quantity · order_type · product
    +limit_price · trigger_price · fill_price
    +instrument: CanonicalInstrument
  }
  class ChildOrderIntent {
    +child_account_id
    +child_broker_id
    +broker_instrument
    +side · quantity · order_type · product
    +price · trigger_price
    +client_reference?
    +copy_event_id
  }
  class SubmitResult {
    +outcome (ACCEPTED | REJECTED | UNKNOWN)
    +broker_order_id?
    +reason_code?
  }

  BrokerAdapter <|.. AliceBlueAdapter
  BrokerAdapter <|.. GrowwAdapter
  BrokerAdapter <|.. AngelOneAdapter
  AliceBlueAdapter ..|> MasterOrderSource
  AliceBlueAdapter ..|> ChildOrderExecutor
  AliceBlueAdapter ..|> OrderStatusProvider
  AliceBlueAdapter ..|> PositionProvider
  AliceBlueAdapter ..|> FundsProvider
  AliceBlueAdapter ..|> InstrumentMapper
  GrowwAdapter ..|> ChildOrderExecutor
  GrowwAdapter ..|> OrderStatusProvider
  GrowwAdapter ..|> PositionProvider
  GrowwAdapter ..|> FundsProvider
  GrowwAdapter ..|> InstrumentMapper
  GrowwAdapter ..|> MasterOrderSource : gated, unverified
  AngelOneAdapter ..|> ChildOrderExecutor : contract unverified
  BrokerAdapter --> CapabilityProfile
  CapabilityProfile --> SegmentCaps
  SegmentCaps --> Verification
  NormalizedMasterEvent --> CanonicalInstrument
  ChildOrderIntent --> SubmitResult
```

### Interface evaluation

| Candidate | Keep? | Why |
|---|---|---|
| `BrokerAdapter` | Yes, thin | Identity, capability profile, session status. Not a god-interface: each role is a separate interface, so a Child-only broker never has to fake Master methods. |
| `MasterOrderSource` | Yes | Wraps today's `get_order_book()` for `MasterOrderPoller` and the optional Alice WS stream. Must return **Unknown** (not empty) on a failed read. |
| `ChildOrderExecutor` | Yes | `place/modify/cancel` with a three-valued `SubmitResult`. `UNKNOWN` is first-class so no adapter can hide an ambiguous send as a retryable error. |
| `OrderStatusProvider` | Yes | Separate from the executor because fill sync and reconciliation need it without placing orders. `find_by_client_reference` only for brokers that send a reference (Groww today). |
| `PositionProvider` | Yes | Maps to `get_positions_or_none()`; `holdings()` optional and **unverified for every broker** in the repo. |
| `FundsProvider` | Yes | Maps to `get_funds()`; Unknown = fail closed. |
| `InstrumentMapper` | Yes — the key new piece | Cross-broker copies need canonical instrument identity; a Master broker's token means nothing to another broker (already acknowledged in `risk_inputs.py` ~263). |

Implementation note: the first step is to declare these as `typing.Protocol`s that the **existing** client classes
already satisfy (structural typing), so Alice Blue behaviour is untouched. Wrapper adapters are added only where a
method's semantics differ (e.g. Unknown vs empty).

## 2.3 Per-Child planner rules (in order, each fails closed)

1. Child membership enabled, account active, not emergency-stopped (existing).
2. Child broker's `role=CHILD` verification ≥ required level and its gate flag on (existing `can_be_child`, extended).
3. Strategy compatibility: Master broker → Child broker pair allowed (see 05).
4. Instrument: `InstrumentMapper.from_canonical` returns a mapped instrument for this Child broker; segment supported for this broker; else `SKIPPED: INSTRUMENT_UNMAPPED` / `SEGMENT_NOT_SUPPORTED`.
5. Product and order type supported for that segment on that broker; else `SKIPPED: PRODUCT_NOT_SUPPORTED` / `ORDER_TYPE_NOT_SUPPORTED`. No silent substitution (e.g. never turn SL-M into MARKET).

   **Decision D1:** a segment, product or order type the Child broker cannot handle skips **only that Child's
   execution for that order**, with the reason recorded on its `copy_trades` row and in `audit_logs`. It never
   blocks the order for other Children. At membership time the API and UI show a non-blocking warning listing the
   segments, products and order types this Child will not receive (`CHILD_SEGMENT_COVERAGE_WARNING`); the Child
   can still be added if its broker's Child role is enabled.
6. Quantity: existing `QuantityCalculator`; lot multiple must be exact where the broker exposes lot size; never rounded.
7. Price: LIMIT → exact Master limit price; MARKET → existing marketable-LIMIT rule with `MARKET_ORDER_BUFFER_PERCENT` (default 0). Tick-size validation is a known gap for every broker; it is not invented here.
8. Risk: SELL guard (Child's own position book via its `PositionProvider`), funds (`FundsProvider`), exposure, daily loss — all existing, all fail closed.
9. Persist `copy_trades` row (unique triple) **before** the broker call; then submit.
