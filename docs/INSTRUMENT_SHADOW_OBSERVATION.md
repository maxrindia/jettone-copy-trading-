# Instrument shadow observation runbook (R4d)

How to run and judge a one-week observation of the R4c shadow comparison. It describes the **evidence** needed before anyone
writes a proposal to enforce instrument mapping. It does not start an observation, and it enables nothing.

Read with [`INSTRUMENT_SHADOW.md`](INSTRUMENT_SHADOW.md), which describes the job and the endpoints.

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
