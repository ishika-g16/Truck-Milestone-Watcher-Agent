from project.constants import (
    ARRIVAL_PROXIMITY_KM,
    GPS_STALE_AFTER,
    LIVE_GPS_SOURCES,
    OPERATIONAL_GRACE,
)
from project.data.types import DelayProjection, FlagSet, GPSData, LoadData, StateRow
from project.eta import haversine_km


def compute_flags(now, active_row: StateRow | None, load: LoadData, gps: GPSData | None, projection: DelayProjection) -> FlagSet:
    if active_row is None:
        return FlagSet.not_active()

    current_state = active_row.current_state
    planned = active_row.planned
    committed = active_row.committed
    forecasted = active_row.forecasted
    actual = active_row.actual

    def is_after(left, right) -> bool:
        if left is None or right is None:
            return False
        try:
            return left > right
        except TypeError:
            return False

    # 1. TRANSITION_OVERDUE
    overdue = False
    if planned is None:
        progress_state = "UNAVAILABLE"
    else:
        try:
            overdue = is_after(now, planned + OPERATIONAL_GRACE)
        except TypeError:
            overdue = False
        progress_state = "TRANSITION_OVERDUE" if overdue else "HEALTHY"

    # 2. COMMITMENT_RISK 
    commitment_risk = False
    commitment_state = "NOT_APPLICABLE"
    if current_state in {"ENROUTE_PICKUP", "ENROUTE_DROPOFF"}:
        if committed is None:
            commitment_state = "UNAVAILABLE"
        elif is_after(now, committed) and actual is None:
            commitment_risk = True
            commitment_state = "BREACHED"
        elif actual is None and forecasted is not None and is_after(forecasted, committed):
            commitment_risk = True
            commitment_state = "AT_RISK"

    # 3. STALE_GPS - live trackers only (not FASTAG / no GPS)
    source = (gps.data_source or "").upper() if gps else None
    live = source in LIVE_GPS_SOURCES
    if not live:
        stale_gps = False
    else:
        last = gps.last_activity if gps else None
        if last is None:
            stale_gps = True
        else:
            try:
                stale_gps = now - last > GPS_STALE_AFTER
            except TypeError:
                stale_gps = True

    # 4. STATUS_GPS_CONFLICT - live GPS vs claimed stop; skip ASSIGNED
    try:
        conflict = live and _gps_conflicts(current_state, load, gps)
    except (TypeError, ValueError) as exc:
        print(f"GPS conflict check failed for load {load.load_id}: {exc}")
        conflict = False

    if conflict:
        evidence_state = "CONFLICTING"
    elif stale_gps:
        evidence_state = "STALE_GPS"
    elif (load.canonical_status or "").upper() == "UNMAPPED":
        evidence_state = "UNMAPPED_STATUS"
    elif not live:
        evidence_state = "GPS_UNAVAILABLE"
    else:
        evidence_state = "RELIABLE"

    # 5. DROPOFF_RISK
    dropoff_risk = (projection is not None and projection.dropoff_risk in {"AT_RISK", "BREACH_LIKELY"})

    return FlagSet(
        transition_overdue=overdue,
        commitment_risk=commitment_risk,
        stale_gps=stale_gps,
        status_gps_conflict=conflict,
        dropoff_risk=dropoff_risk,
        progress_state=progress_state,
        commitment_state=commitment_state,
        evidence_state=evidence_state,
    )

def _gps_conflicts(current_state: str, load: LoadData, gps: GPSData | None) -> bool:
    if current_state == "ASSIGNED" or gps is None:
        return False
    if gps.latitude is None or gps.longitude is None:
        return False

    pickup = load.pickup_lat, load.pickup_lon
    dropoff = load.dropoff_lat, load.dropoff_lon

    def is_near(coords: tuple[float | None, float | None]) -> bool | None:
        lat, lon = coords
        if lat is None or lon is None:
            return None
        try:
            distance = haversine_km(gps.latitude, gps.longitude, lat, lon)
        except (TypeError, ValueError):
            return None
        return distance <= ARRIVAL_PROXIMITY_KM # is the ping 1km away?

    at_pickup = is_near(pickup)
    at_dropoff = is_near(dropoff)

    # conflict has occured if...
    if current_state == "ENROUTE_PICKUP":
        return at_pickup is True
    if current_state == "AT_PICKUP":
        return at_pickup is False
    if current_state == "LOADED":
        return at_dropoff is True
    if current_state == "ENROUTE_DROPOFF":
        return at_dropoff is True
    return False
