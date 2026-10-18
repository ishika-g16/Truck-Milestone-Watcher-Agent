"""Map ops and internal DB rows onto dataclasses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from project.data.types import (
    GPSData,
    Incident,
    JOURNEY_STATES,
    LoadData,
    StateRow,
    TimeData,
    Watch,
)

UNMAPPED_STATUS = "UNMAPPED"

# tender_status, sub_status (main.load_tender) and status_event_type (main.status_event,
# pre-filtered to the one event we track - see main.py::get_load's LEFT JOIN) mapped to
# canonical_status set
STATUS_MAP: dict[str, str] = {
    "ENR_PICK": "ENROUTE_PICKUP",
    "ARR_PICK": "AT_PICKUP",
    "DEP_PICK": "ENROUTE_DROPOFF",
    "ENR_DROP": "ENROUTE_DROPOFF",
    "ARR_DROP": "AT_DROPOFF",
    "DEP_DROP": "AT_DROPOFF",  # past watcher terminal
    "POD_SUBMITTED": "AT_DROPOFF",
    "POD_APPROVED": "AT_DROPOFF",
    "ACCEPTED": "ASSIGNED",
    "INTRANSIT": "ENROUTE_PICKUP",  # without sub; GPS lag → STATUS_GPS_CONFLICT
    "DELIVERED": "AT_DROPOFF",
    "LOADED": "LOADED",  # from status_event - no load_tender status ever maps here
}

def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _norm_key(value: str | None) -> str | None:
    raw = _str(value)
    if raw is None:
        return None
    return raw.upper().replace(" ", "_").replace("-", "_")


def normalize_status(
    tender_status: str | None,
    sub_status: str | None = None,
    status_event_type: str | None = None,
) -> str:
    """Reconciles up to three status sources - sub_status, tender_status, and a
    pre-filtered status_event type (currently only ever 'LOADED', see get_load's LEFT
    JOIN) - and returns whichever mapped value is furthest along in JOURNEY_STATES.
    This never regresses canonical_status backward (a stale/lagging source just loses
    to a more-advanced one) and still never lets GPS rewrite status - only these three
    authoritative sources ever feed into it. ACCEPTED→ASSIGNED, INTRANSIT→ENROUTE_PICKUP,
    POD_*/DELIVERED/DEP_DROP collapse to AT_DROPOFF."""
    mapped = []
    for raw in (sub_status, tender_status, status_event_type):
        key = _norm_key(raw)
        if key is not None and key in STATUS_MAP:
            mapped.append(STATUS_MAP[key])

    if not mapped:
        return UNMAPPED_STATUS

    return max(mapped, key=JOURNEY_STATES.index)


def row_to_load_data(row: dict[str, Any]) -> LoadData:
    tender_status = (
        _str(row.get("source_status"))
        or _str(row.get("tender_status"))
    )
    sub_status = (
        _str(row.get("source_sub_status"))
        or _str(row.get("sub_status"))
    )
    status_event_type = _str(row.get("status_event_type"))

    return LoadData(
        load_id=_str(row.get("load_id")),
        load_num=_str(row.get("load_num")),
        vh_num=_str(row.get("vh_num")),
        vh_id=_str(row.get("vh_id")),
        carrier_name=_str(row.get("carrier_name")),
        shipper_name=_str(row.get("shipper_name")),
        origin=_str(row.get("origin")),
        destination=_str(row.get("destination")),
        pickup_lat=_float(row.get("pickup_lat")),
        pickup_lon=_float(row.get("pickup_lon")),
        dropoff_lat=_float(row.get("dropoff_lat")),
        dropoff_lon=_float(row.get("dropoff_lon")),
        source_status=tender_status,
        source_sub_status=sub_status,
        canonical_status=normalize_status(tender_status, sub_status, status_event_type),
    )


def row_to_gps_data(row: dict[str, Any]) -> GPSData:
    return GPSData(
        vh_id=_str(row.get("vh_id")),
        vh_num=_str(row.get("vh_num")),
        load_id=_str(row.get("load_id")),
        load_num=_str(row.get("load_num")),
        latitude=_float(row.get("latitude")),
        longitude=_float(row.get("longitude")),
        city_name=_str(row.get("city_name")),
        last_activity=_dt(row.get("last_activity")),
        received_at=_dt(row.get("received_at")),
        data_source=_str(row.get("data_source")),
        speed_kph=_float(row.get("speed_kph")) or _float(row.get("speed")),
    )


def row_to_time_data(row: dict[str, Any]) -> TimeData:
    return TimeData(
        load_id=_str(row.get("load_id")),
        load_num=_str(row.get("load_num")),
        assigned_at=_dt(row.get("vh_assigned_time")),
        committed_pickup_at=_dt(row.get("committed_pickup_at")),
        committed_dropoff_at=_dt(row.get("committed_dropoff_at")),
        planned_pickup_at=_dt(row.get("planned_pickup_at")),
        planned_dropoff_at=_dt(row.get("planned_dropoff_at")),
        actual_pickup_time=_dt(row.get("actual_pickup_time"))
        or _dt(row.get("actual_pickup_at")),
        actual_dropoff_time=_dt(row.get("actual_dropoff_time"))
        or _dt(row.get("actual_dropoff_at")),
        actual_dep_pickup=_dt(row.get("actual_dep_pickup")),
        actual_loaded_at=_dt(row.get("actual_loaded_at")),
    )


def row_to_watch(row: Any) -> Watch:
    r = dict(row)
    return Watch(
        watch_id=r.get("watch_id"),
        load_id=r.get("load_id"),
        load_num=r.get("load_num"),
        vh_num=r.get("vh_num"),
        watch_state=r.get("watch_state"),
        progress_state=r.get("progress_state"),
        commitment_state=r.get("commitment_state"),
        evidence_state=r.get("evidence_state"),
        created_at=_dt(r.get("created_at")),
        last_evaluated_at=_dt(r.get("last_evaluated_at")),
    )


def row_to_incident(row: Any) -> Incident:
    r = dict(row)
    return Incident(
        incident_id=r.get("incident_id"),
        correlation_key=r.get("correlation_key"),
        watch_id=r.get("watch_id"),
        load_id=r.get("load_id"),
        incident_type=r.get("incident_type"),
        status=r.get("status"),
        opened_at=_dt(r.get("opened_at")),
        resolved_at=_dt(r.get("resolved_at")),
        last_updated_at=_dt(r.get("last_updated_at")),
        recommended_action=r.get("recommended_action"),
        severity=r.get("severity"),
        narrative=r.get("narrative"),
        suppressed=bool(r.get("suppressed")),
        times_seen=int(r.get("times_seen") or 1),
    )


def row_to_state_row(row: Any) -> StateRow:
    r = dict(row)
    return StateRow(
        load_id=r.get("load_id"),
        seq=int(r["seq"]),
        current_state=r.get("current_state"),
        next_state=r.get("next_state"),
        planned=_dt(r.get("planned")),
        committed=_dt(r.get("committed")),
        forecasted=_dt(r.get("forecasted")),
        actual=_dt(r.get("actual")),
        completed=bool(r.get("completed")),
    )
