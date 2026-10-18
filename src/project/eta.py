"""Deterministic ETA from ops GPS + stop coordinates (haversine)."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt

from project.data.types import ETAData, GPSData, LoadData
from project.constants import DEFAULT_SPEED_KPH, MIN_ETA_SPEED_KPH, ROAD_DISTANCE_FACTOR


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between two points in kilometers."""
    r = 6371.0 # radius of earth
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    a = min(1.0, max(0.0, a))
    return 2 * r * asin(sqrt(a))


def eta_target(load: LoadData, current_state: str | None) -> tuple[str, float | None, float | None]:
    """Returns (eta_of, target_lat, target_lon) from the active row's current_state."""
    status = (current_state or "").upper()
    if status in {"LOADED", "ENROUTE_DROPOFF", "AT_DROPOFF"}:
        return "dropoff", load.dropoff_lat, load.dropoff_lon
    return "pickup", load.pickup_lat, load.pickup_lon


def speed_kph_for_eta(gps: GPSData | None) -> float:
    if gps is None or gps.speed_kph is None:
        return DEFAULT_SPEED_KPH
    try:
        live = float(gps.speed_kph)
    except (TypeError, ValueError):
        return DEFAULT_SPEED_KPH
    if live >= MIN_ETA_SPEED_KPH:
        return live
    return DEFAULT_SPEED_KPH


def compute_eta(load: LoadData, gps: GPSData | None, now: datetime, current_state: str | None) -> ETAData:
    """Haversine × ROAD_DISTANCE_FACTOR; cruise speed if ≥ MIN_ETA_SPEED_KPH else default."""
    eta_of, target_lat, target_lon = eta_target(load, current_state)

    base = ETAData(
        load_id=load.load_id,
        vh_id=load.vh_id or (gps.vh_id if gps else None),
        ETA_value=None,
        ETA_of=eta_of,
        calculated_at=now,
    )

    if gps is None or gps.latitude is None or gps.longitude is None:
        return base
    if target_lat is None or target_lon is None:
        return base

    try:
        distance_km = haversine_km(gps.latitude, gps.longitude, target_lat, target_lon)
        road_km = distance_km * ROAD_DISTANCE_FACTOR
        speed = speed_kph_for_eta(gps)
        if speed <= 0:
            return base
        hours = road_km / speed
        base.ETA_value = now + timedelta(hours=hours)
    except (TypeError, ValueError, ZeroDivisionError):
        return base
    return base
