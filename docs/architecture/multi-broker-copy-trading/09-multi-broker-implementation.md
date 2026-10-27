# 09 Multi-broker implementation: what exists, what is verified, what blocks production

Status as of 2026-10-27. This is the implementation record for the dynamic Master/Child architecture. **Nothing here enables a pair.**
Alice Blue -> Alice Blue is still the only supported pair; every other combination is refused at membership, at the Master switch and at
order time (`BROKER_PAIR_NOT_SUPPORTED`). Implementation completeness is not production eligibility.

## 1. Architecture in one view

| Layer | Module | Role |
|---|---|---|
| Registry and gates | `brokers/registry.py` | Which broker may log in / be Master / be Child. Flags default off; Master also needs the contract's `master_verified`. |
| Capability evidence | `brokers/capabilities.py`, `brokers/profiles.py` | Per-feature `VERIFIED_LIVE / VERIFIED_DOCS / UNVERIFIED / NOT_SUPPORTED` with an evidence ceiling per broker; a contract flag can lower a rating, never raise it past the ceiling. |
| Pair gate | `brokers/pairs.py` | Explicit allowlist (`SUPPORTED_PAIRS`) plus live gates plus evidence. Unchanged. |
| Master detection | `brokers/master_source.py`, `brokers/order_book_read.py` | Polling-first read of the Master's order book as ORDERS / EMPTY / UNKNOWN / FAILED; `MasterSourceAdapter` refuses to read for a closed Master gate; `NormalizedMasterOrder` carries the broker id and a broker-aware dedupe scope. |
| Event identity | `brokers/identity.py`, `copy_engine/events.py`, `validator.py` | `master_scope()` is `""` for Alice Blue (every historical key byte-identical) and `"<broker>:<account>:"` otherwise, so native ids from different brokers or Masters cannot collide. Restart recovery rebuilds the same scope. |
| Child execution | `alice_blue/client.py`, `groww/client.py`, `angel_one/client.py`, `zerodha/client.py` | place / modify / cancel / order book / positions / funds. Ambiguous outcomes are TIMEOUT and are never resent. |
| Order lookup | `get_order`, `find_by_client_reference` on Groww and Zerodha | Read-only per-order queries (`OrderStatusProvider`). Alice Blue and Angel One have none. |
| Instruments | `instruments/catalogs.py`, `instruments/contracts.py`, per-broker stores | Equity shadow comparison for all four catalogs; canonical equity / future / option identity with per-broker resolution that refuses unknown, ambiguous, stale and unloaded catalogs. |
| Risk | `copy_engine/risk_inputs.py`, `instruments/terms.py` | SELL guard resolves with the Child's own instrument master; quantity / lot / freeze validation remains observe-only. |
| API and UI | `api/brokers.py` (`/brokers/pairs`, `/brokers/matrix`), `BrokerMatrixPanel.tsx`, `AddAccountModal.tsx` | Read-only compatibility matrix, per-role evidence, recorded Master-detection blockers; Zerodha interactive login. |

## 2. Zerodha (Kite Connect v3), new in this stage

Built only from Zerodha's official v3 documentation (`zerodha/contract.py` lists every source and every unclear point). Nothing was
observed against a real account; `live_verified` and `master_verified` are `False` in code.

* **Gating:** `ZERODHA_ENABLED`, `ZERODHA_CHILD_TRADING_ENABLED`, `ZERODHA_MASTER_ENABLED`, all default `false` (in `core/config.py`).
  Master additionally needs `ZerodhaContract.master_verified`, which only a supervised capture may set.
* **Login:** interactive. `GET /accounts/{id}/broker-login-url` returns the Kite login URL; the owner signs in and
  `POST /accounts/{id}/broker-login-complete` exchanges the single-use `request_token` (checksum SHA-256(api_key + request_token +
  api_secret)). The access token is stored encrypted in Redis until 06:00 IST the next day. The engine never logs in by itself
  (`AUTO_LOGIN_ON_STARTUP = False`); `POST /broker-login` answers 409 with the instruction.
* **Credentials:** the encrypted vault, fields `api_key`, `api_secret`.
* **Placement:** `regular` variety, `DAY` validity only. Product map: CNC/MIS for NSE/BSE, NRML/MIS for NFO; anything else is refused before
  any request. MARKET and SL-M send `market_protection=-1`. A deterministic 20-character `tag` (`JT` + SHA-256 prefix) is stored in Redis
  against the Jettone order id **before** the send.
* **Ambiguity:** only a 4xx proves rejection. A 5xx (including 502 / 504), a timeout after send, or a success without an order id is
  TIMEOUT after exactly one read-only `tag` lookup; the create is never repeated. A token rejection clears the session and is not retried.
* **Modify:** refused for a part-filled order (the documentation does not say whether `quantity` is total or remaining).
* **Reads:** strict number parsing; interim statuses (`VALIDATION PENDING`, `OPEN PENDING`, ...) stay unmapped so an order is not copied
  until it is `OPEN`; a part-filled `OPEN` becomes `PARTIAL`; a row whose status and fills disagree fails the whole read. Other varieties,
  validities and exchanges surface as unsupported statuses the Master poller will not dispatch. Positions come from `data.net`; a missing
  list is "unknown", never "flat". `instrument_id` is always `None`, so another broker's token cannot be mistaken for Zerodha's.
* **Updates:** `zerodha/updates.py` parses WebSocket order frames and postbacks (constant-time checksum check). No socket is opened; an
  update is only ever a hint to re-read the order book.

## 3. Per-broker status

| Broker | Master | Child | Evidence |
|---|---|---|---|
| Alice Blue | Implemented and live (poll of the REST order book; the NorenWS order stream is silent) | Implemented and live | VERIFIED_LIVE |
| Groww | Adapter reads the order list; **not eligible** (`master_verified` false) | Implemented from documentation; gated off | VERIFIED_DOCS (login observed live 2026-10-19) |
| Angel One | Adapter reads the order book; **not eligible** | Implemented; gated off | UNVERIFIED |
| Zerodha | Order-book read implemented; **not eligible** | Implemented from documentation; gated off | VERIFIED_DOCS |

## 4. Master -> Child compatibility matrix (current)

`GET /api/v1/brokers/matrix` returns this live; the table is the default-flags result.

| Master \ Child | Alice Blue | Groww | Angel One | Zerodha |
|---|---|---|---|---|
| **Alice Blue** | Supported | Blocked | Blocked | Blocked |
| **Groww** | Blocked | Blocked | Blocked | Blocked |
| **Angel One** | Blocked | Blocked | Blocked | Blocked |
| **Zerodha** | Blocked | Blocked | Blocked | Blocked |

All fifteen blocked cells fail at `PAIR_NOT_ALLOWLISTED`. A test opens every Zerodha flag and sets `master_verified`, and the matrix still
reports only Alice -> Alice.

## 5. Implemented versus unverified

Implemented and tested offline: the four Master-source paths through one read contract; broker-aware event identity; Child adapters for
all four brokers; per-order lookup for Groww and Zerodha; canonical equity / future / option identity with Zerodha resolution; the
four-broker equity catalog comparison; the 4x4 matrix API and panel; Zerodha vault, login, builder, manager routing and account API.

Unverified (each is a recorded blocker, none is assumed):

1. **Master detection of orders placed in the broker's own app** for Groww, Angel One and Zerodha. Kite's pages are silent on whether
   `GET /orders`, postbacks and the order WebSocket include orders placed outside the API key.
2. **Zerodha static-IP requirement:** not stated on the pages read, so none is enforced or invented.
3. **Zerodha modify quantity semantics** for a part-filled order, and the per-segment product rules beyond CNC / MIS / NRML.
4. **Derivative mapping:** Zerodha's `name` column is documented only for equity; Groww's loader reads cash equity only; Angel One's
   scrip-master fields beyond token / symbol / name / exch_seg have no verified reference. Derivative resolution therefore returns
   `RESOLVED_UNVERIFIED` at best and never `verified`.
5. **Freeze quantity** has no authoritative source for any broker. Quantity validation stays observe-only and the two strict over-freeze
   `xfail` tests remain visible.
6. **Angel One** individual-order status lookup was not researched; `ORDER_LOOKUP` stays `NOT_SUPPORTED`.
7. **Master-switch preflight** reads Alice-convention raw rows. A Groww / Angel One / Zerodha account in the switch scope is blocked with
   `BROKER_VERIFICATION_UNAVAILABLE` (fail closed). Widening it to parsed records is a separate, deliberate change to a pinned behaviour.
8. **Live evidence for everything non-Alice** is absent: no supervised smoke order, no capture, no five-day observe run.

## 6. Production-readiness blockers (for any non-Alice pair)

The twelve acceptance criteria in the README apply unchanged. In addition, for Zerodha: a supervised capture of an externally placed order
in `GET /orders`; confirmation of the static-IP rule; the modify-semantics capture; and owner approval recorded against a specific pair.
