import os

import pymysql
from dotenv import load_dotenv

from project.report_agent import run_report_agent
from project.constants import LIVE_GPS_SOURCES, FLAG_TO_INCIDENT, DEMO_ACTIONS, now_ist_naive
from project.data.mappers import row_to_load_data, row_to_gps_data, row_to_time_data
from project.data.types import (
    TERMINAL_STATE,
    FlagSet,
    Incident,
    Investigation,
    LoadData,
    LoadResult,
    StateRow,
    TimeData,
    Watch,
)
from project.eta import compute_eta
from project.flags import compute_flags
from project.investigator import investigate, should_investigate
from project.internal import (InternalStore, connect as internal_connect, correlation_key, db_path as internal_db_path)
from project.llm_metrics import print_batch_metrics, reset_batch_metrics
from project.domino import propagate_delay

load_dotenv()

REQUIRED_OPS_ENV = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PWD", "DB_NAME")


def db_connection():
    """Establishing a connection with the ops DB."""
    missing = [key for key in REQUIRED_OPS_ENV if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"Missing ops DB env vars: {', '.join(missing)}")
    try:
        port = int(os.environ["DB_PORT"])
    except ValueError as exc:
        raise RuntimeError(f"DB_PORT must be an integer, got {os.environ['DB_PORT']!r}") from exc
    try:
        return pymysql.connect(
            host=os.environ["DB_HOST"],
            port=port,
            user=os.environ["DB_USER"],
            password=os.environ["DB_PWD"],
            database=os.environ["DB_NAME"],
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.Error as exc:
        raise RuntimeError(f"Could not connect to ops DB: {exc}") from exc


def _close(conn, name: str) -> None:
    """Closing the DB connection."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception as exc:
        print(f"Failed to close {name}: {exc}")


def list_active_loads(conn) -> list[str]:
    """Fectch the list of active load ids from the ops DB. Returns list of load ids."""
    sql = """
        SELECT lt.id
        FROM main.load_tender AS lt
        WHERE tender_status IN ('ACCEPTED', 'INTRANSIT')
            AND tractor_number IS NOT NULL
            AND lt.latest_vehicle_assign_time >= UTC_TIMESTAMP() - INTERVAL 1 MONTH
        LIMIT 25
    """
    with conn.cursor() as cur:
        try:
            cur.execute(sql)
            rows = cur.fetchall()
        except pymysql.Error as exc:
            raise RuntimeError(f"Failed to list active loads: {exc}") from exc
    return [str(r["id"]) for r in rows if r.get("id") is not None]


def get_load(conn, load_id: str):
    """Fetch all load details of one load, given a specific load id."""
    sql = """
        SELECT lt.id AS load_id,
            lt.load_num,
            lt.sub_status AS source_sub_status,
            lt.tender_status AS source_status,
            se_loaded.event_type AS status_event_type,
            shipper.name AS shipper_name,
            carrier.name AS carrier_name,
            lt.tractor_id AS vh_id,
            lt.tractor_number AS vh_num,

            lt.orig_city_name AS origin,
            lt.dest_city_name AS destination,
            lt.orig_latitude AS pickup_lat,
            lt.orig_longitude AS pickup_lon,
            lt.dest_latitude AS dropoff_lat,
            lt.dest_longitude AS dropoff_lon
        FROM main.load_tender AS lt
            INNER JOIN main.organization AS shipper
                ON lt.customer_org_id = shipper.id
            INNER JOIN main.organization AS carrier
                ON lt.carrier_org_id = carrier.id
            LEFT JOIN main.status_event AS se_loaded
                ON se_loaded.load_id = lt.id
                AND se_loaded.event_type = 'LOADED'
        WHERE lt.id = %s
          AND lt.tractor_number IS NOT NULL
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (load_id,))
            row = cur.fetchone()
    except pymysql.Error as exc:
        print(f"Ops query failed for load {load_id}: {exc}")
        return None
    if not row:
        return None
    try:
        return row_to_load_data(row)
    except Exception as exc:
        print(f"Could not map load {load_id}: {exc}")
        return None


def get_latest_gps(conn, load_id: str):
    """Fetch the latest gps ping for a load, if available, from the ops DB."""
    sql = """
    SELECT
       load_id,
       load_num,
       vehicle_id AS vh_id,
       vehicle_num AS vh_num,
       city_name,
       latitude,
       longitude,
       CONVERT_TZ(last_activity_date, 'UTC', 'Asia/Kolkata') AS last_activity,
       data_source,
       speed,
       CONVERT_TZ(create_time, 'UTC', 'Asia/Kolkata') AS received_at
    FROM main.location_log
    WHERE data_source IN ('TELEMATICS', 'MOBILE_APP', 'FASTAG', 'CELLULAR_TRACKING')
        AND load_id = %s
    ORDER BY last_activity_date DESC 
    LIMIT 1
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (load_id,))
            row = cur.fetchone()
    except pymysql.Error as exc:
        print(f"Ops GPS query failed for {load_id}: {exc}")
        return None
    if not row:
        return None
    try:
        return row_to_gps_data(row)
    except Exception as exc:
        print(f"Could not map GPS for {load_id}: {exc}")
        return None


def get_times(conn, load_id) -> TimeData | None:
    """Fetch planned, committed, and actual time data for a load from ops DB"""
    sql = """
    SELECT 
        lt.id as load_id,
        lt.load_num,

        CONVERT_TZ(lt.latest_vehicle_assign_time, 'UTC', 'Asia/Kolkata') AS vh_assigned_time,
        CONVERT_TZ(pickup.appt_start_time, 'UTC', 'Asia/Kolkata') AS committed_pickup_at,
        CONVERT_TZ(dropoff.appt_start_time, 'UTC', 'Asia/Kolkata') AS committed_dropoff_at,
        CONVERT_TZ(pickup.arrive_time, 'UTC', 'Asia/Kolkata') AS planned_pickup_at,
        CONVERT_TZ(dropoff.arrive_time, 'UTC', 'Asia/Kolkata') AS planned_dropoff_at,
        CONVERT_TZ(pickup.actual_arrive_time, 'UTC', 'Asia/Kolkata') AS actual_pickup_at,
        CONVERT_TZ(dropoff.actual_arrive_time, 'UTC', 'Asia/Kolkata') AS actual_dropoff_at,
        CONVERT_TZ(pickup.actual_depart_time, 'UTC', 'Asia/Kolkata') AS actual_dep_pickup,

        se_loaded.event_time AS actual_loaded_at

    FROM main.load_tender AS lt

        LEFT JOIN main.load_stop AS pickup
            ON pickup.parentid = lt.id
            AND CAST(pickup.is_pickup AS UNSIGNED) = 1
        
        LEFT JOIN main.load_stop AS dropoff 
            ON dropoff.parentid = lt.id
            AND CAST(dropoff.is_drop AS UNSIGNED) = 1

        LEFT JOIN main.status_event AS se_loaded
            ON se_loaded.load_id = lt.id
            AND se_loaded.event_type = "LOADED"

    WHERE lt.id = %s
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (load_id,))
            row = cur.fetchone()
    except pymysql.Error as exc:
        print(f"Ops times query failed for {load_id}: {exc}")
        return None
    if not row:
        return None
    try:
        return row_to_time_data(row)
    except Exception as exc:
        print(f"Could not map times for {load_id}: {exc}")
        return None


def persist_flags(
    store: InternalStore,
    watch: Watch,
    load: LoadData,
    active_row: StateRow,
    flags: FlagSet,
    now,
    investigation: Investigation | None = None,
) -> Watch:
    """After compute_flags, copy FlagSet onto Watch and open/resolve incidents."""
    watch.progress_state = flags.progress_state
    watch.commitment_state = flags.commitment_state
    watch.evidence_state = flags.evidence_state
    watch.last_evaluated_at = now
    watch = store.update_watch(watch) # write updated watch into memory

    load_id = load.load_id
    if not load_id:
        print("persist_flags: missing load_id, skip incidents")
        return watch

    firing = { # get all flags that are true for this load
        incident_type
        for attr, incident_type in FLAG_TO_INCIDENT
        if getattr(flags, attr)
    }
    state = active_row.current_state 
    findings = {
        f.flag_type: f for f in (investigation.findings if investigation else [])
    }
    use_inv_action = bool(
        investigation
        and investigation.overall_severity in {"ELEVATED", "CRITICAL"}
        and investigation.recommended_action
    )

    # resolve incidents if the state has moved on or if the flags were not fired
    for incident in store.get_open_incidents(load_id):
        if not incident.correlation_key:
            continue
        parts = [p.strip() for p in (incident.correlation_key or "").split("|")]
        inc_state = parts[1] if len(parts) == 3 else None
        inc_type = parts[2] if len(parts) == 3 else incident.incident_type
        expected_state = "ENROUTE_DROPOFF" if inc_type == "DROPOFF_RISK" else state 
        if inc_state != expected_state or inc_type not in firing:
            store.resolve_incident(incident.correlation_key, at=now)

    # Open or re-open incidents that have just fired
    for incident_type in firing:
        finding = findings.get(incident_type)
        incident_state = "ENROUTE_DROPOFF" if incident_type=="DROPOFF_RISK" else state
        action = DEMO_ACTIONS.get(incident_type, "REVIEW_LOAD")
        if use_inv_action and investigation is not None:
            action = investigation.recommended_action
        store.upsert_incident(
            Incident(
                incident_id=None,
                correlation_key=correlation_key(
                    load_id=load_id,
                    state=incident_state,
                    incident_type=incident_type,
                ),
                watch_id=watch.watch_id,
                load_id=load_id,
                incident_type=incident_type,
                status="OPEN",
                opened_at=now,
                resolved_at=None,
                last_updated_at=now,
                recommended_action=action,
                severity=finding.severity if finding else None,
                narrative=finding.narrative if finding else None,
                suppressed=finding.suppressed if finding else False,
            )
        )
    return watch

def _result_if_watch_closed(
    store: InternalStore,
    load_id: str,
    watch: Watch | None,
    load: LoadData | None = None,
) -> LoadResult | None:
    """Close an ACTIVE watch. Return LoadResult only if this wake closed one."""
    if watch is None or not watch.watch_id: # skip loads that are completed before being seen by watcher 
        print(f"\nSKIP {load_id}: journey complete, no ACTIVE watch.")
        return None
    store.resolve_open_incidents_for_watch(watch.watch_id)
    store.complete_watch(watch.watch_id)
    if load is not None:
        completed_rows = store.get_state_rows(load_id)
        try:
            store.write_load_memory(load, watch, completed_rows)
            store.update_patterns(load, completed_rows, watch)
        except Exception as exc:
            print(f"Memory write failed for {load_id}: {exc}")
    print(f"\nCOMPLETED WATCH for load {load_id}.")
    return LoadResult(
        load_id=load_id,
        active_state=None,
        watch=watch,
        flags=FlagSet.not_active(),
        incidents=store.get_open_incidents(load_id),
        load=load,
        active_row=None,
        journey_rows=store.get_state_rows(load_id),
        incident_history=store.get_incidents(load_id),
    )


def run_one_load(ops_conn, store: InternalStore, now, load_id) -> LoadResult | None:
    """Processing each active load"""
    load = get_load(ops_conn, load_id)
    if load is None:
        print(f"No load data for {load_id}")
        return None
    if not load.load_id:
        print(f"Load {load_id} mapped without load_id")
        return None

    existing_rows = store.get_state_rows(load_id)
    active_watch = store.get_active_watch(load_id)
    is_terminal = (load.canonical_status or "").upper() == TERMINAL_STATE

    # First-seen and already done: no SQLite writes.
    if is_terminal and not existing_rows and active_watch is None:
        print(f"\nSKIP {load_id}: untracked terminal status.")
        return None

    if is_terminal:
        if existing_rows:
            store.catch_up_state_rows(load, existing_rows)
        return _result_if_watch_closed(store, load_id, active_watch, load)

    state_rows = store.ensure_state_rows(load, times=None)
    state_rows = store.catch_up_state_rows(load, state_rows)
    if all(row.completed for row in state_rows):
        return _result_if_watch_closed(store, load_id, active_watch, load)

    watch = store.ensure_active_watch(load, at=now)

    times = get_times(ops_conn, load_id)
    state_rows = store.refresh_actuals(state_rows, times)
    try:
        active_row = next(row for row in state_rows if not row.completed)
    except StopIteration:
        return _result_if_watch_closed(store, load_id, watch, load)

    gps = get_latest_gps(ops_conn, load_id)

    eta = None
    source = (gps.data_source or "").upper() if gps else None
    if ( # only forecast times for enroute states
        active_row.current_state in {"ENROUTE_PICKUP", "ENROUTE_DROPOFF"}
        and source in LIVE_GPS_SOURCES
    ):
        try:
            eta = compute_eta(load, gps, now, active_row.current_state)
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            print(f"ETA failed for {load_id}: {exc}")
            eta = None

    forecasted = eta.ETA_value if eta is not None else None
    store.set_forecasted(load_id, active_row.seq, forecasted) # update forecasted time in state table
    if forecasted is not None:
        active_row.forecasted = forecasted
    elif active_row.forecasted is None:
        active_row.forecasted = active_row.planned

    projection = propagate_delay(state_rows, times, active_row, eta, now)
    state_rows = store.apply_projected_forecasts(state_rows, projection)

    flags = compute_flags(now, active_row, load, gps, projection)
    investigation = None
    if should_investigate(flags):
        prior_incidents = store.get_open_incidents(load_id)
        investigation = investigate(
            store,
            load,
            active_row,
            flags,
            now,
            gps=gps,
            watch=watch,
            state_rows=state_rows,
            incidents=prior_incidents,
            projection=projection
        )
    watch = persist_flags(store, watch, load, active_row, flags, now, investigation)
    open_incidents = store.get_open_incidents(load_id)

    return LoadResult(
        load_id=load_id,
        active_state=active_row.current_state,
        watch=watch,
        flags=flags,
        incidents=open_incidents,
        load=load,
        active_row=active_row,
        investigation=investigation,
        projection=projection,
        journey_rows=state_rows,
        incident_history=store.get_incidents(load_id),
    )

def run_batch(now):
    print("\nWelcome! Batch has started running...\n")
    ops_conn = None
    internal_conn = None
    try:
        ops_conn = db_connection()
        internal_conn = internal_connect()
    except Exception as exc:
        print(f"WATCHER STARTUP FAILED: {exc}")
        _close(ops_conn, "ops DB")
        _close(internal_conn, "internal DB")
        return False

    store = InternalStore(internal_conn)
    reset_batch_metrics()
    try:
        try:
            active_loads = list_active_loads(ops_conn)
        except Exception as exc:
            print(f"Failed to list active loads: {exc}")
            return False
        print(f"LIST OF ACTIVE LOAD IDs: {active_loads}\n")

        results = []
        for load_id in active_loads:
            try:
                result = run_one_load(ops_conn, store, now, load_id)
                if result is not None:
                    results.append(result)
            except Exception as exc:
                print(f"\nERROR evaluating {load_id}: {exc}")
                continue
        try:
            run_report_agent(results, now=now)
        except Exception as exc:
            print(f"Report agent failed: {exc}")

        print_batch_metrics()
        return True
    finally:
        _close(ops_conn, "ops DB")
        _close(internal_conn, "internal DB")

def main():
    now = now_ist_naive()
    ok = run_batch(now)
    print("END MAIN")
    if not ok:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
