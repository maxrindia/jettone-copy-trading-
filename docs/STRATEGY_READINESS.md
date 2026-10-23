# Strategy readiness diagnostics (R6)

Read-only visibility into two questions the operator should be able to answer before relying on a strategy:

1. **Will each Child's broker pair be accepted by the order-time gate (R5)?** Assignment to a strategy is not execution support.
2. **How much of the Master's recent instrument activity has any mapping evidence (R4i)?** Mapping is diagnostic only.

Nothing here places, modifies or cancels an order, calls a broker, changes a role or writes a row. The R5 gate, order placement,
instrument identity semantics and every default are unchanged. The logic is `app/services/strategy_readiness.py`; the pair
decision itself always comes from `brokers/pairs.py` (there is no second policy).

## Where it appears (all additive, backward compatible)

| Surface | New field |
|---|---|
| `GET /api/v1/strategies`, `GET /api/v1/strategies/{id}` | `readiness` on the strategy; `broker_pair` on each nested member |
| `GET .../members`, `POST .../members`, `PUT .../members/{account_id}` | `broker_pair` on the member |
| `GET .../switch-master/preflight` and the `preflight` of a switch | non-blocking `BROKER_PAIR_NOT_SUPPORTED` entries in `warnings` |
| `GET /api/v1/strategies/{id}/readiness?hours=24` (new) | pair readiness, `mapping_coverage`, merged `warnings`, `scope` |

`broker_pair`: `master_broker`, `child_broker` (canonical ids; a NULL stored broker is Alice Blue, as everywhere), `supported`
(`true`, `false`, or `null` only when the strategy has no Master), `code` (`BROKER_PAIR_NOT_SUPPORTED`), `requirement` (what is
missing: `PAIR_NOT_ALLOWLISTED`, `MASTER_BROKER_UNKNOWN`, `CHILD_ROLE_NOT_OPEN`, `EVIDENCE_BELOW_LIVE`, ...), `detail`, `message`.

`readiness.broker_pairs_ready` means only that every trading-enabled Child's pair is supported. It does **not** mean orders will be
placed: `observe_only` shows the live-trading switch, and sessions, risk and instrument checks still apply. A Child whose pair is
unsupported stays a member (and is shown as such); its orders are skipped at order time.

The Master switch is never blocked or changed by these warnings. A switch that would leave a Child unsupported under the new
Master's broker simply says so, naming the Child (and the old Master when it stays as a Child) with the missing requirement.

## Mapping coverage (`mapping_coverage`)

Computed from the Master account's own recent orders (window `hours`, default 24, capped at 168; newest 500 orders), looked up in
the local NSE snapshot configured by `INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH` (cached; no network). It does not need the shadow job.

- `source`: `NOT_CONFIGURED`, `FILE_UNUSABLE` (with a `source_error` code, never the path) or `LOADED` (with snapshot provenance and `stale`).
- `status`: `AVAILABLE`, `NO_RECENT_ORDERS` (coverage unknown, **not** "fully covered") or `NO_MASTER`.
- `verified_identities`: **always 0.** No authorised evidence yet shows that Alice's `instrumentId` equals the contract-master
  `Token`, so even a candidate is `MASTER_TOKEN_LINK_UNVERIFIED`. `candidates` counts them separately.
- `by_state` / `groups` keep the situations distinct: `identity_unverified` (candidates), `no_mapping_found` (token not in the snapshot),
  `source_unavailable` (no usable file), `source_stale`, `no_master_token`, `exchange_not_covered` (BSE and derivatives), and
  `needs_review` (conflict, deleted, invalid ISIN, unsupported series, symbol disagreement).
- Warning codes: `MAPPING_SOURCE_UNAVAILABLE`, `MAPPING_SNAPSHOT_STALE`, `MAPPING_CONFLICT`, `MAPPING_IDENTITY_UNVERIFIED`,
  `MAPPING_NO_MASTER_TOKEN`, `MAPPING_NOT_FOUND`, `MAPPING_EXCHANGE_NOT_COVERED`, `MAPPING_UNSUPPORTED_SERIES`, `MAPPING_DELETED`,
  `MAPPING_INVALID_ISIN`, `MAPPING_SYMBOL_DISAGREES`, `MAPPING_COVERAGE_UNKNOWN`, `MAPPING_COVERAGE_TRUNCATED`.

No ISIN, token, credential, session value or new account id is added to any of this output.

## Not changed

Membership and Master-switch rules (`CHILD_BROKER_UNSUPPORTED`, `NEW_MASTER_BROKER_UNSUPPORTED`), the R5 order-time gate, the planner,
`CANONICAL_MAPPING_ENFORCED`, `INSTRUMENT_SHADOW_ENABLED`, and live trading. Whether adding a not-yet-supported pair to a strategy
should eventually be refused outright (as the design document suggests with a 409) is an open decision; today it is visible, not blocked.
