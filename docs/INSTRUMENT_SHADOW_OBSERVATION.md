# Instrument shadow observation runbook (R4d)

How to run and judge a one-week observation of the R4c shadow comparison. It describes the **evidence** needed before anyone
writes a proposal to enforce instrument mapping. It does not start an observation, and it enables nothing.

Read with [`INSTRUMENT_SHADOW.md`](INSTRUMENT_SHADOW.md), which describes the job and the endpoints, and with section G below, which
adds what R4i (NSE crosswalk evidence), R5 (broker-pair gate) and R6 (strategy readiness) changed. Where G and an earlier section
differ on a point G covers, G governs.

---

## A. Purpose and boundaries

- R4c is **observe-only**. It compares the instruments on persisted Master orders with in-memory broker catalogs and records
  results. It makes no broker call, places no order, and nothing in the copy pipeline reads it.
- Following this runbook does **not** authorize mapping enforcement, a pipeline change, enabling Live Trading, or a deployment.
- **No observation has started.** This repository holds no observation results, and none may be written into it or invented.
  Every table below is a blank template.
- **Keep real data out of Git.** Raw report exports, daily logs, incident logs and the final decision contain real instrument
  symbols from real trading. Store them in a private folder outside the repository (for example
  `C:\Users\<you>\Documents\jettone-evidence\instrument-shadow\<period-id>\`), never under the repo, `.claude/` or any synced
  public location. Only this runbook and its blank templates belong in Git.
- Never paste the internal API token, a session cookie or any credential into an evidence file.

---

## B. Start conditions

A period can count toward readiness only if **all** of these hold on day 1. Record each one in the period's evidence folder.

| # | Condition | Evidence to record |
|---|---|---|
| B1 | Separate, explicit approval to **deploy R4c** | Approval note, deployed commit hash (`git rev-parse --short HEAD` of what runs), deployment time |
| B2 | Separate, explicit approval to set `INSTRUMENT_SHADOW_ENABLED=true` | Approval note, the time the flag took effect |
| B3 | Settings recorded | `INSTRUMENT_SHADOW_INTERVAL_SECONDS`, `INSTRUMENT_SHADOW_WINDOW_HOURS`, `INSTRUMENT_SHADOW_MAX_ORDERS` actually in force (defaults 900 / 24 / 500; the job clamps them to at least 300 s, at most 168 h and at most 500) |
| B4 | Baseline report saved | `GET /api/v1/instruments/shadow-report?hours=24` JSON before any review, with hash (section C), showing each catalog's `loaded` state |
| B5 | Catalog state recorded per broker | For Alice Blue, Angel One and Groww: `loaded`, `snapshot_loaded_at`, and the reason if not loaded (for example Groww not enabled or no master URL) |
| B6 | Scope written down | **Equity / CASH only**, the Master-to-Child pairs under assessment, and the statement that FUT/OPT are out of scope |
| B7 | Deployment and engine restart times recorded | Every restart during the period is added to the log (restarts reload catalogs) |
| B8 | A reliable Master instrument identity source exists | See below |

### B8. The Master identity requirement

The job normalises each Master order from **exchange and symbol only**, because persisted orders carry no ISIN. A symbol is a
name, not an identity, so such a Master can never receive a definitive `MATCH`. For a period to count, the Master side needs
**ISIN-level identity from verified evidence**. Acceptable evidence:

- an ISIN taken from the Master broker's own instrument master row for the **same instrument** the Master order was placed
  with, where the owner has confirmed from a capture that the master actually carries an ISIN column and which row is meant; or
- an ISIN from an official exchange or depository security list, matched to the order by an exchange-assigned identifier, not by
  name.

In every case the source, the time it was loaded and how it was linked to the order must be recorded.

**Not acceptable:** a symbol, a symbol with `-EQ` removed, an ISIN looked up from the Groww or Angel One catalog by symbol, a
token that has not been verified, any entry from Alice's in-process `KNOWN_INSTRUMENTS` table (it mixes hand-written
placeholders and has no provenance), or any guess.

**The R4c job cannot use such a source today.** It always calls the normaliser with no ISIN (`normalize_equity(exchange,
symbol, None)`) and reads nothing but the order's exchange and symbol. So even after an identity source exists, the job must be
changed, in a separate approved change, to supply the verified ISIN before any result can be a definitive `MATCH`. Until both
exist, no run can count.

**Today no such source exists**, so any run before it does is a **coverage-only rehearsal**. A rehearsal is useful for finding
operational problems (catalog loading, report size, job behaviour). **Its days never count toward the readiness period**, and
the log must be headed `COVERAGE-ONLY REHEARSAL`.

---

## C. Seven-day protocol

**Duration.** Seven consecutive calendar days **and** at least five trading sessions in which the Master placed orders. If the
Master traded very few distinct instruments, extend the period: stability across few instruments is weak evidence. Note
exchange holidays in the log.

**Fixed daily review time.** After market close (for example 18:00 IST), the same time each day. The shadow job's dedupe day is
a **UTC** date, so each broker and instrument gets a new event shortly after 05:30 IST when nothing has changed.

### What the endpoints actually provide

`GET /api/v1/instruments/shadow-report?hours=&limit=&offset=` (internal token required, read-only):

| Behaviour | Fact |
|---|---|
| Window | `hours` default 24; values below 1 or above 168 are clamped to 1 and 168 (not rejected). A non-integer returns 422. |
| Paging | `limit` default 100, clamped to 1 to 500; `offset` clamped to 0 to 100000. |
| Event cap | At most **5000** newest events in the window are read. `truncated: true` means older events in the window were not read. |
| Aggregates | `groups`, `by_verdict`, `by_reason`, `by_broker` describe the **latest event per broker and instrument** among the events read. They ignore `limit` and `offset`. |
| Rows | `rows` is one page of the events read, newest first. `pagination.total` is the number of events read, not the number in the database. |
| Other fields | `enabled`, `generated_at`, `window_hours`, `min_evidence`, `event_count`, `instrument_count`, `catalogs` (per broker: `loaded`, `snapshot_loaded_at`, `note`), `notes`. |
| Row fields | `created_at`, `broker_id`, `exchange`, `symbol`, `master_key`, `confidence`, `verdict`, `reason`, `group`, `candidate_count`, `catalog_loaded`, `catalog_loaded_at`, `diffs`, `unverified_attributes`. |

`GET /api/v1/instruments/compare?exchange=&symbol=&isin=` compares one instrument against every catalog and **writes nothing**.
A caller-supplied ISIN can produce a real `MATCH`, so it is the tool for cross-checking a result against an independent source.
It returns, per broker, the verdict, reason, group, candidate count, catalog state and, when a row matched, that row's exchange,
trading symbol, ISIN, lot size and tick size (no token). Parameters: `exchange` (required, 1 to 20 characters), `symbol`
(required, 1 to 60) and `isin` (optional, at most 12); a missing or over-long value returns 422. Unusable input (an invalid ISIN,
a derivative, an index name) is **not** an error: it returns 200 with an `UNMAPPED` result and a reason.

**Authentication and errors (both endpoints).** GET only (POST, PUT, PATCH and DELETE return 405). The engine requires the
internal token: a missing or wrong token returns 401, and an unset server token returns 503 (fails closed). Through the
application's Next.js proxy, a request without a signed-in session returns 401 `UNAUTHENTICATED` before it reaches the engine.
An unexpected engine error returns 500 with `{"success": false, "error": {"code": "INTERNAL_ERROR", ...}}`. Successful responses
are `{"success": true, "data": ...}`.

**Groups** (existing verdicts, grouped by the report): `matched`, `attribute_mismatch`, `identity_conflict`,
`identity_unconfirmed` (symbol-only, or a candidate with no ISIN), `ambiguous`, `catalog_not_loaded`, `catalog_stale`,
`segment_not_supported`, `unmapped`. Note that Angel One's evidence level is below the fixed `VERIFIED_DOCS`, so it is reported as
`segment_not_supported` even though its catalog is also not loaded; read its `catalogs` entry for the real state.

**What the API does not provide.** There is **no daily breakdown**, **no list of result changes**, and **no job heartbeat or
last-run time**. Do not rely on any of these. The operator computes day-to-day changes from saved rows (below).

### Reaching the endpoints

The engine API is internal only. Use either the application's own authenticated proxy (sign in to the Next.js app, then request
`/api/v1/instruments/...` from the same browser session), or run an HTTP client **inside the engine container** that reads the
token from its own environment without printing it. Never echo the token or put it on a command line that is logged.

### Each day

1. **Retrieve.** Save, in the evidence folder, the raw JSON of:
   - `shadow-report?hours=24&limit=500&offset=0`, then `offset=500`, `1000`, ... until `pagination.returned` is below `limit`;
   - `shadow-report?hours=168&limit=500` (cumulative), paged the same way.
   Name files `YYYY-MM-DD_24h_p1.json` and so on. The aggregates are the same on every page; keep page 1's.
2. **Hash.** Compute a SHA-256 of every file (`sha256sum file` or `Get-FileHash file -Algorithm SHA256`) and record the hash,
   the date, the report window, `generated_at`, the deployed commit and the three settings.
3. **Check truncation.** If any report has `truncated: true`, its aggregates and rows cover only the newest 5000 events. Narrow
   `hours` and page again. If the **24-hour** report is truncated, record an incident: that day cannot count until resolved.
4. **Record catalog state.** Copy each broker's `loaded`, `snapshot_loaded_at` and note into the catalog template. Note any
   refresh (a changed `snapshot_loaded_at`) and the restart that caused it.
5. **Record counts** from `groups` and `by_broker`: matched, identity-unconfirmed, unmapped, ambiguous, identity-conflict,
   attribute-mismatch, stale catalog, unloaded catalog, plus `instrument_count` and each broker's `with_candidates`. If the
   Master identity requirement (B8) is not met, `matched` must be **zero** for symbol-only Masters; a nonzero count from the job
   in that case is a defect (see incidents).
6. **Compare with yesterday.** From the saved rows, take the latest row per (`broker_id`, `exchange`, `symbol`) for today and for
   yesterday. List every instrument whose `group`, `verdict`, `reason` or `candidate_count` differs, and every new or missing
   instrument. For each, classify the cause: *catalog refresh* (its `catalog_loaded_at` changed), *new Master instrument*, or
   *unexplained*. Explain every material change in writing. An unexplained change is an incident.
7. **Investigate** every `identity_conflict`, every `ambiguous` and every unexplained change. Use `compare` with an ISIN taken
   from an independent source, and record what it showed.
8. **Corroborate job activity** (see below). A non-empty report is not proof that the job is healthy.
9. **Write the day's entry** (template E1) and the instrument-level change list (E3).

### Job liveness: what can and cannot be shown

R4c has **no durable liveness signal**. A successful cycle writes **no log line**. Log lines that do exist:

| Log line (prefix `[InstrumentShadow]`) | Meaning |
|---|---|
| `enabled; first cycle after the start-up delay (observe-only).` | The job task started (once per engine start). |
| `cycle failed: ...` | A cycle raised an error. |
| `deduplication evidence unavailable; nothing written this cycle: ...` | Dedupe could not be read; nothing was written. |
| `cycle ended with ...` | A cycle returned an error. |
| `stopped after an error: ...` | The task ended with an error at shutdown. |

So an operator can show that the job **started** after each engine start and whether any **errors** were logged, but cannot show
that every cycle ran. The only indirect evidence is that each broker and instrument gets a new event on the first cycle after
each UTC midnight; an instrument with no event dated today suggests the job did not complete a cycle that day, **or** that the
Master did not trade it recently enough to be among the newest orders examined. Record the log search you used and its result
each day. Container log timestamps are UTC (IST is +5:30). Neither a non-empty nor an empty report proves anything about health:
an empty one can mean the job is down or that the Master placed no orders in the window, and a non-empty one can be left over
from earlier cycles. Also note that each cycle examines only the newest `INSTRUMENT_SHADOW_MAX_ORDERS` Master orders placed
within the last `INSTRUMENT_SHADOW_WINDOW_HOURS`; an instrument not traded within that window stops getting new events (its old
events stay). Until a heartbeat exists, criterion 9 below cannot be marked PASS on liveness.

### Missed reviews and gaps

Events persist and are not pruned, but the **report** is what an operator can reach, and it limits reconstruction:

- The window is always "the last `hours` hours from now", at most **168**. A specific past date cannot be requested.
- The report reads the **newest 5000** events in the window. Older events are the ones cut off when it is truncated, so the day
  you want to rebuild is the first to be lost. Reconstruction works only if **all events from that day up to now fit under 5000**
  (about the number of instruments traded times three brokers, per day, plus changes). If `truncated` is `true`, the missed day
  cannot be rebuilt from the report.
- The aggregates describe the **latest** state, not a past day's. To rebuild a day, request `hours` large enough to reach it, page
  every row, and take the latest row per (`broker_id`, `exchange`, `symbol`) whose `created_at` is no later than the end of that
  day. An identity with no row on or before that day (or whose rows were cut off) cannot be reconstructed.
- Querying the database directly would avoid these limits, but production database access is outside this runbook and needs its
  own approval.

Log the gap and how it was recovered. A day that cannot be reliably reconstructed (older than 168 hours, truncated, or with
missing identities) invalidates the period: restart it.

### Catalog refreshes and restarts

Catalogs change only when the engine restarts or a store is reloaded. A refresh changes `snapshot_loaded_at` and therefore
creates new events; that is expected and is recorded, not an incident, **provided** the cause is known and the candidate counts
change only as the new catalog explains. A restart does not restart the period by itself.

**Freshness consequence.** The Groww catalog is downloaded **once, at engine start**, and nothing refreshes it. The comparator
treats a catalog older than 24 hours as stale, so with continuous uptime Groww reports `catalog_stale`
(`CATALOG_OLDER_THAN_LIMIT`) from about 24 hours after the start. A seven-day period therefore cannot show a fresh Groww catalog
on days 2 to 7 unless the engine is restarted. **Do not restart the engine for this purpose:** a restart has real effects on
existing orders (restart recovery, orders ineligible for replication) and must be a separate, deliberate decision. Record the
staleness honestly; it makes criterion 5 FAIL for Groww until a safe refresh exists.

### Incidents and restarting the period

Log every incident (template E4) with its evidence and disposition. A **material defect** is any of: a `MATCH` produced for a
symbol-only Master identity; a match across NSE and BSE; an identity inferred from a symbol or from another broker's catalog; the
job writing anything other than shadow events; a report that exposes a token or credential; a comparison whose result changes with
no explanation; a dedupe failure that writes duplicates. After a material defect is **fixed in a separate approved change**, the
**entire seven-day period restarts** from day 1; days before the fix do not count. Non-material issues (a recovered missed review,
an explained restart) are logged and the period continues.

---

## D. Readiness criteria

Status values: **PASS** (measured and met, with saved evidence), **FAIL** (measured and not met), **NOT VERIFIED** (cannot be
measured or evidence is missing). A missing identity source, an unloaded catalog, an unmeasurable criterion, a truncated report or
an unreviewed material change is **never** PASS.

Expected status with today's code is shown only to make the gaps visible; it is not a result.

| # | Criterion | Measurement | PASS needs | Evidence required | Expected today / remediation |
|---|---|---|---|---|---|
| 1 | No unsafe guesses | Over all seven days, count `MATCH` rows whose `confidence` is not `ISIN` (report rows carry `confidence`). Report rows do **not** show which candidate matched, so a match across exchanges cannot be seen in the report: check it with `compare` spot checks, whose `matched.exchange` is shown, and treat the report-only evidence as incomplete | Zero `MATCH` with non-ISIN confidence on every daily report, and `compare` spot checks (recorded) showing the matched exchange equals the Master's | Saved daily JSON; the per-day count; the spot-check outputs | NOT VERIFIED: with no Master ISIN there are no definitive matches to test. Needs B8 and a job change. |
| 2 | Master identity evidence | Share of distinct Master instruments traded in the period whose identity has ISIN confidence from an accepted source (B8) | **100%** of the instruments traded. A symbol-only instrument is not covered. The threshold is 100% because enforcement acts on each order, so an instrument with no identity cannot be enforced either way | Identity source and provenance; per-instrument list | FAIL (0%). Needs a persisted or derivable Master identity source. |
| 3 | Alice-to-Alice parity | Every instrument the Master traded that the Child would previously have copied maps 1:1 | 100% `MATCH`; any skip means behaviour would change versus today, which the architecture forbids for Alice-to-Alice | Per-instrument `MATCH` list; zero unmapped | FAIL: Alice's catalog is not loaded. Needs a verified Alice catalog with ISIN and provenance. |
| 4 | Other broker pairs (for example Alice to Groww) | Per instrument traded: `MATCH`, or a recorded skip with a known reason | **No fixed percentage.** Every non-`MATCH` instrument has a written reason and an explicit owner decision to accept the skip. A percentage would hide which trades stop copying | Per-instrument table and owner sign-off | NOT VERIFIED until criteria 2 and 5 hold. |
| 5 | Catalog freshness and completeness | For each in-scope broker, every daily report shows `loaded: true`, a `snapshot_loaded_at` within the freshness limit (24 h) and no `catalog_stale` / `catalog_not_loaded` results | Zero stale or not-loaded results for in-scope brokers after day 1 | Daily catalog template | FAIL for Alice (not loaded) and Angel One (no production loader). Groww: loaded only when enabled with a master URL, loaded once at engine start and never refreshed, so it becomes stale about 24 hours after start; FAIL on days 2 to 7 without a safe refresh (do not restart the engine to get one). Its rows are equity only, so a missing instrument may have been filtered at load. Completeness can only be shown as "one full load"; a truncated download that still parses cannot be detected. |
| 6 | Seven-day stability | Same input and same catalog give the same result each day | Every change between days is explained (catalog refresh or new instrument); zero unexplained | Instrument-change lists (E3) | NOT VERIFIED until a period runs. |
| 7 | Unmapped and ambiguous review | Every `unmapped`, `ambiguous` and `identity_unconfirmed` instrument has a recorded cause | Zero unreviewed; the cause of every `ambiguous` found (for example duplicate catalog rows) | Review notes | NOT VERIFIED. |
| 8 | Attribute evidence | Lot and tick size compared only where both sides are verified | Either verified on both sides for the instruments in scope, or an explicit statement that the criterion is out of scope | `diffs` and `unverified_attributes` in saved rows | NOT VERIFIED: Masters carry no lot or tick size and Groww provides no tick size. |
| 9 | Job liveness and report completeness | Job started after each engine start; no error log lines unexplained; a report saved for every day; no truncated day | All of these, plus a liveness signal stronger than "events exist" | Log search results; the file set | NOT VERIFIED: no heartbeat exists, so continuous liveness cannot be shown. |
| 10 | No unresolved material investigations | Open incidents at the end | Zero unresolved; no material defect after the last restart of the period | Incident log | Evaluated at the end. |
| 11 | Scope | The proposal covers **equity / CASH only**; FUT/OPT remain explicitly unsupported and unaffected | The proposal says so and nothing in it changes derivative handling | The proposal text | PASS by design, provided the proposal keeps it. |

**Overall.** *READY FOR A PROPOSAL* only if criteria 1 to 11 that are in scope are all PASS and the period was not a rehearsal.
Any FAIL gives *NOT READY*. Any NOT VERIFIED gives *NOT READY* until it is measured.

**Why a coverage-only rehearsal cannot establish readiness.** Without ISIN-level identity on the Master side, every result is
symbol-only, which can never be a definitive match. The rehearsal can show that catalogs load, how big the reports are and
whether the job runs, but a high count of "candidate present" says nothing about whether the right instrument would be chosen.
Counting it would turn name similarity into evidence of identity.

---

## E. Evidence and incident templates

Copy these into the private evidence folder. Leave them blank in Git.

### E1. Daily report entry
```text
Period ID:                 Day (1-7):          Date (UTC):           Trading session? Y/N
Rehearsal (coverage-only)? Y/N   (Y = does not count)
Reviewer:                  Review time (IST):
Deployed commit:           Engine started at:  Restarts since last entry:
Settings in force:         interval=      window_hours=      max_orders=

Reports retrieved (file, window, generated_at, pages, SHA-256):
  24h:
  168h:
truncated in any report?   Y/N   If Y: action taken:
Missed review recovered?   Y/N   How:

Counts (latest state per broker and instrument):
  matched | identity_unconfirmed | unmapped | ambiguous | identity_conflict
  attribute_mismatch | catalog_stale | catalog_not_loaded | segment_not_supported
  instrument_count:        event_count:
MATCH with non-ISIN confidence (must be 0):

Changes vs previous day (see E3):     count:    unexplained:
Incidents opened today:               Incidents closed today:
Job log check (query used and result):
Notes:
```

### E2. Catalog state and coverage
```text
Date (UTC):
Broker        loaded  snapshot_loaded_at  fresh (<24h)?  note / reason not loaded  refresh since last entry (cause)
alicebluenew
angelone
groww
Scope reminder: equity/CASH only. Groww rows are EQ on NSE/BSE only.
Brokers whose criteria are NOT VERIFIED today because the catalog is not loaded:
```

### E3. Instrument-level result changes
```text
Date (UTC):
Broker | Exchange | Symbol | Previous (group/verdict/reason/candidates) | Now | Cause: catalog refresh / new instrument / unexplained | Explanation | Incident ref
```

### E4. Incident, investigation and resolution
```text
Incident ID:        Opened (UTC):       Opened by:
Day / period:       Material? Y/N (see "material defect" list)
Description:
Evidence (file names and hashes, log lines, compare output):
Independent source used for cross-check:
Investigation notes:
Root cause:
Disposition: no action / documented / fix proposed (separate approval) / period restarted
Closed (UTC):       Closed by:           Effect on the period:
```

### E5. Final seven-day readiness decision
```text
Period ID:   Start (UTC):   End (UTC):   Rehearsal? Y/N (if Y, stop: not eligible)
Trading sessions with Master activity:   Distinct instruments traded:
Master identity source (B8) and provenance:
Days reviewed / days missed or recovered:
Unresolved incidents:

Criterion  Status (PASS / FAIL / NOT VERIFIED)  Evidence (files)  Remediation if not PASS
 1 No unsafe guesses
 2 Master identity evidence
 3 Alice-to-Alice parity
 4 Other broker pairs
 5 Catalog freshness and completeness
 6 Seven-day stability
 7 Unmapped and ambiguous review
 8 Attribute evidence
 9 Job liveness and report completeness
10 No unresolved material investigations
11 Equity/CASH scope only

Overall: READY FOR A PROPOSAL / NOT READY
Known limitations carried forward (copy section F list):
Decided by:   Date:
This record is evidence only. It does not enable enforcement, change the copy pipeline, enable Live Trading or deploy anything.
```

---

## F. Final decision

A completed period provides **evidence for a separate enforcement proposal**, nothing more. It must never, by itself or
automatically:

- enable mapping enforcement or add a planner rule;
- change the copy pipeline, validator, risk logic or any order decision;
- enable Live Trading or alter any trading flag;
- trigger a deployment.

Any such change needs its own plan, review, tests and explicit approval.

**Limitations that must appear in every readiness decision**
- Persisted Master orders contain no ISIN, so the shadow job cannot establish definitive matches for symbol-only identities.
- Alice Blue's catalog is not loaded.
- Angel One has no production catalog loader and its data has no ISIN, lot or tick size.
- Groww covers equity rows on NSE and BSE only and provides no tick-size evidence; a missing instrument may have been filtered out.
- FUT/OPT mappings are unsupported.
- Events are not pruned, and a report window is capped at 168 hours and 5000 events.
- There is no durable job-liveness signal.
- The only evidence level used is `VERIFIED_DOCS`; no broker's instrument handling has been verified against live data here.

---

## G. Addendum for R4i, R5 and R6 (preflight)

Written before any observation. It changes nothing in the system and starts nothing. **No observation day has elapsed and none is
claimed.** Every table is a blank template; evidence stays outside Git (section A).

### G1. What changed since sections B to F were written

- **R4i** records a second, separate event type, `INSTRUMENT_SHADOW_CROSSWALK`, per Master `(exchange, symbol, instrument_token)` when
  `INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH` points at a local official NSE security-master file. The shadow report gains a `crosswalk`
  section. The comparison events, groups and verdicts of sections C and D are unchanged, and the job still passes **no ISIN** to the
  comparator, so `matched` must still be zero for the Master side.
- **A crosswalk candidate does not satisfy B8.** Every row is `MASTER_TOKEN_LINK_UNVERIFIED`: nothing yet shows that Alice's order-book
  `instrumentId` equals the contract-master `Token`. A candidate ISIN is a lead for an operator, never "ISIN from verified evidence".
  Criteria 1, 2 and 3 stay NOT VERIFIED / FAIL while the link is unverified, however many candidates the report shows.
- **R5** supports only Alice Blue -> Alice Blue for new Child orders; every other pair is skipped at order time as
  `BROKER_PAIR_NOT_SUPPORTED`. A skipped unsupported pair is expected behaviour, not an incident. It is also why criterion 4 cannot
  be measured on a live pair: record unsupported pairs, do not count them as parity.
- **R6** adds `GET /api/v1/strategies/{id}/readiness?hours=` (read-only; same authentication and proxy rules as section C). It reports
  per-Child pair support, the Master's recent-instrument coverage against the local snapshot, warning codes and snapshot provenance,
  **without** needing the shadow job. `verified_identities` is always 0.
- **R7 liveness.** `GET /api/v1/instruments/shadow-liveness` (and a `liveness` summary in `shadow-report`) reports `DISABLED`,
  `ENABLED_NOT_STARTED`, `STARTING`, `HEALTHY`, `FAILING`, `OVERDUE`, `STALLED_IN_CYCLE`, `STOPPED` or `UNKNOWN`, with the last success,
  the last failure and a stable error code. It comes from the job's own in-process state plus **one** `system_events` row of type
  `INSTRUMENT_SHADOW_LIVENESS` that is overwritten after each completed cycle (so it survives a restart and never grows). `HEALTHY`
  needs a successful cycle within three intervals plus 60 seconds. It is a **point-in-time** answer plus counters (`cycles_ok`,
  `cycles_failed`, `consecutive_failures`) since the last start: it does not record every cycle between two reviews. A cycle fails if
  either the comparison or the crosswalk step reported an error (crosswalk codes are prefixed `CROSSWALK_`). An empty report is never
  evidence of liveness. The older log lines still apply (prefix `[InstrumentShadow]`): `crosswalk deduplication unavailable; nothing
  written: ...`, `crosswalk step failed: ...` and `liveness evidence could not be persisted.`

### G2. Readiness status before any period (what exists, what does not)

| Prerequisite | Status | Evidence / reason |
|---|---|---|
| Shadow job, `shadow-report`, `compare` and the R6 readiness endpoint exist and are tested | READY | In the repository; flags default off. |
| Every safety flag off by default (`INSTRUMENT_SHADOW_ENABLED`, `CANONICAL_MAPPING_ENFORCED`, live trading) | READY | Pinned by tests. |
| Evidence stored outside Git; raw exports never committed | READY | Section A. |
| R4c+R4i+R5+R6 deployed where the Master trades (B1) | BLOCKED | Needs separate explicit approval; the deployed commit must be recorded. Not given. |
| Approval to set `INSTRUMENT_SHADOW_ENABLED=true` (B2) | BLOCKED | Needs separate explicit approval. Not given. |
| Master identity from verified evidence (B8) | BLOCKED | R4i candidates are unverified. Needs a fresh authorised Master order-book export showing `instrumentId` equals the contract-master `Token` per exchange, **and** a separate approved change so the job supplies a verified ISIN. |
| Alice Blue catalog loaded with ISINs and provenance | BLOCKED | Not loaded today (criterion 3). |
| Angel One catalog | BLOCKED | No production loader. Its pair is unsupported by R5, so it is out of scope unless that changes. |
| Groww catalog | COVERAGE-ONLY | Only with Groww enabled and a master URL; loaded once at engine start and stale after about 24 hours. Its pair is unsupported by R5. |
| NSE snapshot supplied by the operator | COVERAGE-ONLY | See G4. R7 lets the running engine pick up a newer file placed in a snapshot directory, without a restart; the daily download itself is still manual and subject to NSE's terms (unresolved). Not deployed. |
| BSE identity coverage | BLOCKED | No all-scrips source. BSE is reported `EXCHANGE_NOT_COVERED`; the scope must say so. |
| Cross-source ISIN disagreement policy | BLOCKED | Undecided. Record disagreements (G5); do not resolve them. |
| Job liveness evidence | COVERAGE-ONLY | R7 adds an explicit liveness status (G1). It shows the state at the time asked and the counters since start, not every cycle in between, and it is not deployed. Criterion 9 may be marked PASS only if the G7 liveness conditions hold on every day. |
| NSE data-use permission; real-row fixture in public Git history | BLOCKED | Governance decisions not made. Do not automate any download. |

**Conclusion today:** a period run now can only be a `COVERAGE-ONLY REHEARSAL`. It checks operations (catalog loading, report size,
job behaviour, snapshot handling) and **never counts** toward readiness (section B8). A counted period cannot start until every
BLOCKED row above that applies to the chosen scope is resolved by evidence or an explicit owner decision.

### G3. Daily preflight checklist (before retrieving anything)

Record each answer in the day's entry. Any "no" in items 1 to 6 means the day is excluded (G7) and the reason is logged.

1. The engine is the recorded deployed commit and has run without an unrecorded restart since the last entry.
2. `INSTRUMENT_SHADOW_ENABLED` and `INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH` are the approved values, and the three job settings are those recorded in B3.
3. A log search shows the job started after the last engine start and shows none of the error lines in section C or G1 (record the query and result; container logs are UTC).
4. The configured snapshot file is the one recorded for this period: file name, snapshot date and SHA-256 (G4) are unchanged, or the change is explained.
5. The snapshot is fresh by the crosswalk rule (G4), or the day is logged as `STALE_SNAPSHOT` and excluded.
6. No report you will save is truncated (section C step 3 and G6).
7. The Master traded today (a trading session), or the day is noted as a non-trading day.
8. The market-holiday calendar for the day is noted. The project has no holiday calendar, so a holiday can produce a false stale (it fails closed).

### G4. NSE snapshot: freshness, rotation and provenance

- The snapshot is an operator-supplied local file; nothing downloads it. Use of NSE data and any scripting of its download remain
  subject to NSE's terms, which are unresolved. Do not automate it.
- **Freshness rule:** stale when the snapshot date (read from the file name, India time) is in the future or **more than one weekday**
  old. Friday's file is fresh on Monday; Thursday's is not. There is no holiday calendar.
- **Rotation (R7).** Set `INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH` to a **directory** (a single file path still works as before, but
  then cannot rotate). Each day, place the official file in that directory under its official name
  (`NSE_CM_security_DDMMYYYY.csv[.gz]`). Copy it to a temporary name first (for example `.tmp`, which is ignored) and **rename it into
  place**, so it never appears half-written. The running engine lists the directory at most once a minute and adopts a file only if:
  its date is strictly newer than the loaded snapshot; it has been unmodified for 60 seconds; it passes the unchanged R4h validation;
  and it indexes at least 80% as many rows as the current snapshot (a truncated CSV still parses). Otherwise the **last good
  snapshot stays in place** and the reason is recorded: `NO_NEWER_SNAPSHOT`, `CANDIDATE_NOT_SETTLED`, `CANDIDATE_INVALID`,
  `ROW_COUNT_DROP`, `AMBIGUOUS_SAME_DATE`, `SAME_DATE_FILE_CHANGED`, `DIRECTORY_UNREADABLE`, `NO_CANDIDATE_FILES`; a successful
  change is `ROTATED` (`LOADED` for the first). Two files with the same date, or an edit to the loaded file, are never resolved
  automatically. An old snapshot is never made fresh: with no valid replacement it goes `STALE_SNAPSHOT` by the freshness rule above.
  Nothing restarts the engine and nothing downloads anything. Record the `refresh` object (outcome, candidate file name, rotations,
  `refused_outcome` if a newer file was refused) each day; the directory path is never shown. If the engine starts with an older
  file and a newer invalid one, the older file is loaded and the refusal is recorded in `refused_outcome`.
- **Provenance to record each day** (from `crosswalk.live_snapshot` in the shadow report or `mapping_coverage.snapshot` in the readiness
  response): `source_filename`, `snapshot_date`, `sha256`, `content_sha256`, `age_trading_days`, `stale`, `rows_indexed`,
  `conflicting_tokens`, `deleted_rows`, `invalid_isin_rows`. Independently hash the file on disk
  (`Get-FileHash <file> -Algorithm SHA256`) and confirm it equals `sha256`. A mismatch is an incident.
- Keep a copy of each snapshot actually used in the private evidence folder, not in Git (it is official NSE data).

### G5. Cross-source disagreement recording (record only)

The project has no policy for two sources disagreeing about an instrument's ISIN, so do not resolve any. Each day, for every Master
instrument that the crosswalk calls a `CANDIDATE`, call `compare` with that candidate ISIN and with any ISIN from an independent source
you hold, and record in the instrument-change list (E3): the sources, the ISINs, the exchange and the result. Record separately:

- a candidate ISIN that differs from another source's ISIN for the same instrument (a `CONFLICT` inside the snapshot, or a
  disagreement between sources) as an incident, material if it would change which instrument is chosen;
- NSE and BSE listings of one issuer (never equated);
- any instrument in a series outside the supported list (`UNSUPPORTED_SERIES`) or whose symbol disagrees with the snapshot.

### G6. Daily evidence set (JSON and hashes)

In addition to section C step 1, save and hash every day (names `YYYY-MM-DD_<what>.json`):

| File | Source | Notes |
|---|---|---|
| `shadow_24h_p*.json`, `shadow_168h_p*.json` | `shadow-report` | As section C. Includes the `crosswalk` section: keep page 1. |
| `readiness_<strategy>.json` | `GET /api/v1/strategies/{id}/readiness?hours=24` | One per strategy under observation. Records pair support, warnings and `mapping_coverage`. |
| `liveness.json` | `GET /api/v1/instruments/shadow-liveness` | Status, last success, last failure and code, counters, and the snapshot `refresh` outcome. |
| `snapshot_<name>.sha256.txt` | local hash of the snapshot file | G4. |

Record the SHA-256 of each file, `generated_at`, the deployed commit and the settings (E1). Counts to copy from the readiness and
crosswalk output into the day's entry: `by_state` (`CANDIDATE`, `NO_MASTER_TOKEN`, `UNMAPPED`, `UNSUPPORTED_SERIES`, `STALE_SNAPSHOT`,
`CONFLICT`, `DELETED`, `INVALID_ISIN`, `EXCHANGE_NOT_COVERED`, `CROSSWALK_UNAVAILABLE`, `SYMBOL_DISAGREES`, `MASTER_SYMBOL_UNUSABLE`),
`candidates`, `verified_identities` (must be 0), `total_instruments`, the pair summary (`children_pair_supported` and
`children_pair_unsupported`) and every warning code. **Report limits to respect:**

- **R7 truncation metadata.** `shadow-report` now carries `limits` (comparison events and crosswalk events read versus the 5000 cap,
  the window requested versus applied, the page requested versus applied with `has_more`, and the crosswalk `rows` cap), plus
  `complete` and `incomplete_reasons`. **A day whose 24-hour report has `complete: false` cannot count** (reasons:
  `COMPARISON_EVENT_CAP_REACHED`, `CROSSWALK_EVENT_CAP_REACHED`, `WINDOW_CLAMPED`). Paging and the 100-row crosswalk list are not
  incompleteness: aggregates cover every instrument. Never rely on `rows` for counts; use `by_state`.
- `mapping_coverage` assesses the newest 500 Master orders in the window and now has its own `limits`, `complete` and
  `incomplete_reasons` (`ORDER_CAP_REACHED`, `WINDOW_CLAMPED`, `NO_RECENT_ORDERS`); `hours` is capped at 168.
- A candidate ISIN is stored on the crosswalk events and appears in the saved report rows as `candidate_isin`; it is not in the readiness output.

### G7. Observation-day acceptance and exclusion

A day **counts** only if all of these hold, with the evidence saved:

1. It is a trading session on which the Master placed orders (and the period reaches 7 consecutive calendar days and at least 5 such sessions, section C).
2. Every G3 item 1 to 6 passed.
3. The full evidence set (G6) was saved and hashed on the day; none truncated.
4. Catalogs in scope were loaded and fresh as section D criterion 5 requires.
5. Every change from the previous day is explained (section C step 6) and every incident opened is logged.
6. `matched` is zero for symbol-only Masters, `verified_identities` is 0, and no candidate is described as verified anywhere in the notes.
7. The 24-hour `shadow-report` has `complete: true`, and the saved `liveness.json` shows `HEALTHY` at review time, a `cycles_ok` count
   consistent with the uptime and interval (roughly uptime divided by interval, allowing for the start-up delay), and any `cycles_failed`
   explained. `OVERDUE`, `STALLED_IN_CYCLE`, `ENABLED_NOT_STARTED`, `FAILING` or `UNKNOWN` at review time excludes the day.

A day is **excluded** (logged, not counted) if any of these is true: the job was not shown to have started; a restart occurred and is
unrecorded; a required snapshot or catalog was stale or unloaded; a report was truncated; the evidence set is incomplete or
unhashed; the Master did not trade; or a material defect (section C) is open. Excluded days do not restart the period unless they
fall under the material-defect rule or leave a gap that cannot be reconstructed (section C). A rehearsal day never counts.

### G8. Final review checklist (before section E5)

- [ ] Period was not a rehearsal and the B1, B2 and B8 approvals and evidence are filed.
- [ ] Seven consecutive days and at least five trading sessions with Master activity, each day passing G7 or logged as excluded with a reason.
- [ ] Snapshot provenance and independent hash recorded for every day; no unexplained change of file, date or hash.
- [ ] Cross-source disagreements (G5) all recorded; none resolved by guesswork.
- [ ] Unsupported broker pairs recorded from the readiness output; none counted as parity.
- [ ] BSE and derivatives stated as out of scope or shown unmapped; no claim of coverage.
- [ ] `verified_identities` was 0 on every day unless a separate approved change and evidence say otherwise.
- [ ] Open incidents: none material; every other disposition recorded.
- [ ] Section F limitations carried forward, plus: liveness is a point-in-time status with counters, not a record of every cycle; the snapshot directory is operator-fed by hand; NSE terms and the public-history fixture unresolved.
- [ ] The decision states it is evidence only and enables nothing.

Section F still applies in full: finishing a period enables no enforcement, changes no pipeline or flag, and deploys nothing.
