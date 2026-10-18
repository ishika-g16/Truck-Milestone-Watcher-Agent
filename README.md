# Truck Milestone Watcher

A pilot watcher that monitors in-transit loads for SemiCab's operations team. Every time
it wakes, it reads live load and GPS data from the ops database, rebuilds a per-load
journey ledger, deterministically flags anything off-track, optionally has Claude triage
and explain what fired, and writes a static HTML report ops can read in about two minutes.

**Shadow mode:** the watcher only ever *recommends*. It reads ops MySQL, writes only to
its own internal SQLite store, and never writes back to ops. 

---

## Running it

```bash
cd src/project
uv run main.py
# or, from anywhere in the repo:
uv run watcher
```

Required env vars:
- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PWD`
- `DB_NAME`

Output: a `report.html` file written to the current working directory.

---

## What one wake does

1. List assigned/in-transit loads from ops (`load_tender.tender_status` in `ACCEPTED` /
   `INTRANSIT`, truck assigned, capped at 60).
2. For each load: pull `LoadData` / `TimeData` / `GPSData` from ops, reconcile canonical
   status from three sources (`sub_status`, `tender_status`, and a `status_event` LOADED
   signal), and rebuild the five-row journey ledger.
3. Catch up completed rows from canonical status, then hydrate each row's
   planned/committed/actual clocks and a GPS-based ETA where applicable.
4. Compute 5 deterministic flags on the active row (no LLM involved in any of this).
5. Roll the active row's delay forward onto the rest of the journey (the "domino" delay
   cascade) and check whether the dropoff appointment is still reachable.
6. If any flag fired, open/refresh an incident and hand the load to the investigator
   (Claude) for triage - suppress-or-not, severity, narrative, recommended action. Skipped
   entirely for a healthy load.
7. Once every load in the batch is processed, the report agent aggregates all results,
   ranks them by a deterministic severity score, asks Claude (or falls back
   deterministically) to write the wake-level summary and priority actions, and renders
   everything to `report.html`.

See [docs/system_overview.md](docs/system_overview.md) for the detailed version of this,
including an embedded flowchart of every branch.

---

## Canonical journey

```text
ASSIGNED → ENROUTE_PICKUP → AT_PICKUP → LOADED → ENROUTE_DROPOFF → AT_DROPOFF
```

`AT_DROPOFF` is terminal (physical arrival at dropoff) and closes the watch; it has no
ledger row of its own - the ledger tracks the five *transitions*, not the sixth state.
Canonical status is resolved from whichever of `sub_status` / `tender_status` /
`status_event.event_type = 'LOADED'` maps furthest along this list - never backward, and
GPS never advances or overrides it (a disagreement is flagged as `STATUS_GPS_CONFLICT`
instead). Anything unrecognized becomes `UNMAPPED` rather than a guess.

---

## Internal SQLite

Six tables, all in `src/project/internal/db.py`:

| Table | Role |
|---|---|
| `watches` | Load-level header. At most one `ACTIVE` watch per load. |
| `state_rows` | The five-row ledger (`seq` 0–4): planned/committed/forecasted/actual + `completed` per milestone. |
| `incidents` | One row per `load_id \| state \| incident_type`. `OPEN`/`RESOLVED`, a `times_seen` wake counter, plus the investigator's `severity`/`narrative`/`suppressed` overlay when it ran. Never deleted. |
| `investigator_runs` | Successful investigator calls (prompt JSON + Claude's JSON response). |
| `load_memory` | One closed-load episode, written when a watch closes. |
| `pattern_store` | Rolling delay stats by carrier/lane/shipper + state, written on close - feeds investigator calibration once a key has `PATTERN_MIN_SAMPLES` (3) closed samples. |

---

## The 5 flags

Computed deterministically in `flags.py`, no LLM involved. Exact conditions in
[docs/intervention.md](docs/intervention.md).

| Flag | Fires when |
|---|---|
| `TRANSITION_OVERDUE` | `now > planned + 30 min` on the active row |
| `COMMITMENT_RISK` | En route only - `forecasted > committed` (`AT_RISK`) or `now > committed` with no actual yet (`BREACHED`) |
| `STALE_GPS` | Live-tracker ping older than 1 hour (FASTAG doesn't count as live) |
| `STATUS_GPS_CONFLICT` | Live GPS within 1 km of a stop the claimed status says the truck shouldn't be near yet |
| `DROPOFF_RISK` | The delay-cascade projection says the dropoff appointment is `AT_RISK` or `BREACH_LIKELY` |

---

## Project layout

```text
src/project/
  main.py              # wake entry point: ops queries, run_batch / run_one_load, persist_flags
  flags.py             # compute_flags → FlagSet (the 5 flags above)
  domino.py            # propagate_delay → rolls active-row delay onto downstream milestones
  eta.py               # haversine-based ETA
  investigator.py      # per-load Claude triage, only when a flag fired
  report_agent.py      # aggregates LoadResults into a WakeBriefing + narrative
  report.py            # renders the briefing + narrative to report.html
  llm_metrics.py       # per-batch LLM call/cost tracking
  constants.py         # buffers, GPS/ETA thresholds, incident/action maps
  data/
    types.py           # dataclasses + JOURNEY_STATES
    mappers.py         # ops row → dataclass, normalize_status
  internal/
    db.py              # SQLite schema
    store.py           # ledger read/write + investigator memory
docs/
  system_overview.md    # full architecture walkthrough + flowchart
  design_decisions.md   # what's implemented and why
  intervention.md       # exact flag conditions
  watcher_demo.html      # presentation deck
README.md
```

---

## Key constants

(`src/project/constants.py`)

| Name | Value | Used for |
|---|---|---|
| Dispatch threshold | 2 hours | `ASSIGNED.planned` (`assigned_at + 2h`); also added to `LOADED.planned` |
| Pickup dwell | 18 hours | `AT_PICKUP` / `LOADED` planned and committed |
| Operational grace | 30 minutes | `TRANSITION_OVERDUE`: `planned + 30m` |
| Domino min slip | 30 minutes | Delay cascade ignores slip smaller than this |
| Dropoff risk margin | 60 minutes | Delay cascade: `AT_RISK` vs `BREACH_LIKELY` cutoff |
| GPS stale after | 1 hour | Live sources only |
| Arrival proximity | 1 km | Status–GPS conflict radius |
| Default / min ETA speed | 40 / 25 km/h | ETA falls back to 40 km/h below the 25 km/h floor |
| Road factor | 1.3 | Haversine distance × factor, approximating real road distance |
| Pattern min samples | 3 | Investigator ignores a carrier/lane/shipper pattern until this many closed loads back it |

---

## Not in this pilot

- Writing anything back to ops MySQL - recommendations only
- Multi-stop trips (single pickup, single drop)
- A real routing engine for ETA (straight-line × fixed factor, not a route projection)
- Skipping loads between wakes - every assigned load is evaluated every wake
- Historical replay / a logical clock - each wake runs against wall-clock time
- An automated feedback loop on what ops actually did with a recommendation

See [docs/watcher_demo.html](docs/watcher_demo.html) ("Where this could go next") for the
fuller list of enhancements discussed but out of scope for this pilot.
