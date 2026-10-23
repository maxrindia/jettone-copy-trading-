# Broker-pair support gate (R5)

A new Child order is copied only when the **Master broker -> Child broker** pair is explicitly supported. Anything else is
skipped for that Child alone with `BROKER_PAIR_NOT_SUPPORTED`, before any funds call, risk check, send claim, order pre-write or
broker call. The policy is `app/services/brokers/pairs.py`.

## Supported pairs

| Master -> Child | Supported | Why |
|---|---|---|
| Alice Blue -> Alice Blue | **Yes** | The production path. Both roles are `VERIFIED_LIVE` and both registry gates are open; the Master's token is native to the Child's broker; duplicate-send protection, ambiguous-send handling, per-Child outcomes, restart recovery and position reconciliation are implemented and tested for it. |
| Alice Blue -> Groww | No | The Child is only `VERIFIED_DOCS` (login alone is live). No ISIN-level Master identity exists (the R4i crosswalk candidate is `MASTER_TOKEN_LINK_UNVERIFIED`). No supervised live smoke order or recorded owner approval. |
| Alice Blue -> Angel One | No | Nothing is rated above `UNVERIFIED`; `modify_order` needs a product the pipeline never passes. |
| Groww or Angel One as Master | No | Master detection is not implemented or verified for either. |
| Any other broker | No | No adapter. |

## How a pair is decided (all must hold)

1. The pair is on the allowlist `SUPPORTED_PAIRS`.
2. The registry still opens the Master role for the Master broker and the Child role for the Child broker.
3. The capability profile still rates `MASTER_DETECTION` and `ORDER_STATUS` (Master) and `CHILD_PLACEMENT`, `MODIFY`, `CANCEL`,
   `ORDER_STATUS`, `POSITIONS` and `FUNDS` (Child) as `VERIFIED_LIVE`.

An unknown broker, an unknown Master (it could not be read), a missing profile or any error while checking is unsupported. Steps
2 and 3 can only **end** the support of a listed pair; nothing can make an unlisted pair supported. A catalog adapter, a
capability profile, an enabled flag or a usable response contract is not execution support.

Four different things, never to be confused: a broker **catalog** is available; **capability metadata** exists; an **execution
adapter** is implemented; the **pair** is verified end to end (placement, lifecycle, reconciliation, instrument identity, live
smoke, owner approval). Only the last one belongs on the allowlist.

## Where it runs

In the LIMIT, SL and MARKET per-Child processors, immediately after the `TRADING_DISABLED` guard and before the optional planner
check (`CANONICAL_MAPPING_ENFORCED`), funds, risk, the SELL guard, the send claim, the order pre-write and the broker call. The
skipped Child gets its normal per-Child outcome row (`copy_trades` status `SKIPPED`, code `BROKER_PAIR_NOT_SUPPORTED`, detail such as
`PAIR_NOT_ALLOWLISTED:alicebluenew->groww`) and an advisory `COPY_SKIPPED` audit row. The other Children continue. Restart-recovery
fanouts use the same processors, so they are gated too. The gate is always on (it only restricts); there is no flag.

## What it deliberately does not gate

Modify, cancel, SL-trigger repricing and an operator's square-off act on orders that already exist, or reduce a position. Blocking
them would strand a live Child order, so they are unchanged. Existing flag-based role gates (`CHILD_BROKER_NOT_READY`) still apply
first at routing time.

## Enabling a pair later

Add it to `SUPPORTED_PAIRS` in the same change as its evidence, after the acceptance criteria in
`docs/architecture/multi-broker-copy-trading/README.md` are met (both roles `VERIFIED_LIVE`, instrument mapping for the claimed
segments with an ISIN-level Master identity, SELL guard on the Child broker, funds, pricing, idempotency, reconciliation, a
supervised live smoke order and recorded owner approval). Membership and Master-switch APIs do not yet return
`BROKER_PAIR_NOT_SUPPORTED`; today the pair is enforced at order time.

## Tests

`tests/test_broker_pair_gate.py` covers the policy, the pipeline behaviour for all three order types and the isolation rules. Tests
that drive a Groww or Angel One Child through the pipeline use the `cross_broker_pairs_enabled` fixture (`tests/conftest.py`), which
lists those pairs and waives the gate's own role and evidence checks for that test only.
