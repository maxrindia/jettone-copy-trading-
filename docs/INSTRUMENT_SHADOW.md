# Instrument shadow comparison (R4c)

An **observe-only** comparison of the instruments on persisted Master orders against the broker instrument catalogs. It
never calls a broker, never places or changes an order, and nothing in the copy pipeline reads it.

## Settings (all optional; add to `.env` yourself - `.env.example` is not touched)

| Setting | Default | Notes |
|---|---|---|
| `INSTRUMENT_SHADOW_ENABLED` | `false` | Off: no task is created and no event is written. |
| `INSTRUMENT_SHADOW_INTERVAL_SECONDS` | `900` | Raised to at least 300. |
| `INSTRUMENT_SHADOW_WINDOW_HOURS` | `24` | Capped at 168. |
| `INSTRUMENT_SHADOW_MAX_ORDERS` | `500` | The newest N orders on a strategy Master examined each cycle (capped at 500). |

When enabled, the job starts after the engine, waits 60 seconds, then repeats every interval. A failed cycle is logged and
the next one still runs. It is cancelled on shutdown.

## What it records

One `INSTRUMENT_SHADOW_COMPARE` system event per (Master instrument, broker) **only when the result is new or changed
today**. The event holds identifiers and verdicts only: broker, exchange, symbol, canonical key and confidence, the
existing comparator verdict and reason, candidate count, catalog loaded/`snapshot_loaded_at`, attribute differences and
unverified attributes. No token, credential or raw broker response. The evidence level is fixed at `VERIFIED_DOCS`.

Deduplication compares each result with the LATEST event for the same broker and instrument today (bounded to today's events of this type), so a change is recorded, and a change back to an earlier state is recorded again. If that cannot be read, nothing is written that cycle.
No migration is used; events are not pruned, so reports are windowed.

## Endpoints (internal token required, GET only, nothing is written)

- `GET /api/v1/instruments/compare?exchange=&symbol=&isin=` compares one instrument. A supplied ISIN can give a real `MATCH`.
- `GET /api/v1/instruments/shadow-report?hours=&limit=&offset=` returns the current state (latest event per broker and
  instrument), counts by group, verdict and reason, per-broker coverage, live catalog state, notes and a page of events.
  `hours` is capped at 168, `limit` at 500.

Groups are the existing verdicts, grouped: `matched`, `attribute_mismatch`, `identity_conflict`,
`identity_unconfirmed` (symbol-only or a candidate without an ISIN), `ambiguous`, `catalog_not_loaded`,
`catalog_stale`, `segment_not_supported`, `unmapped`.

## What the report cannot tell you yet

- **Persisted Master orders have no ISIN.** The job normalises them from exchange and symbol only, so a Master identity is
  always symbol-only and can never be a definitive `MATCH`. Candidate counts show presence in a catalog, not parity.
  A `matched` count above zero from the job would be a defect.
- **Alice Blue**: catalog not loaded (its in-process table mixes placeholders and has no ISIN).
- **Angel One**: no production loader populates the catalog; its data has no ISIN, lot or tick size.
- **Groww**: equity rows on NSE/BSE only; a missing instrument may have been filtered out at load.
- **FUT/OPT** are unmapped for every broker.
