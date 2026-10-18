from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

def now_ist_naive() -> datetime:
    """Current time in Asia/Kolkata, matches CONVERT_TZ(..., 'Asia/Kolkata') in SQL."""
    return datetime.now(IST).replace(tzinfo=None)

# --- transition overdue ---
DISPATCH_THRESH = timedelta(hours=2)
OPERATIONAL_GRACE = timedelta(minutes=30)
PICKUP_DWELL = timedelta(hours=18)

# --- delay projection ---
DOMINO_MIN_SLIP = timedelta(minutes=30)
DROPOFF_RISK_MARGIN = timedelta(minutes=60)

# --- evidence ---
GPS_STALE_AFTER = timedelta(hours=1)
ARRIVAL_PROXIMITY_KM = 1.0
LIVE_GPS_SOURCES = frozenset({"TELEMATICS", "MOBILE_APP", "CELLULAR_TRACKING"})

# --- investigator memory ---
PATTERN_MIN_SAMPLES = 3

# --- ETA (also used by eta.py) ---
DEFAULT_SPEED_KPH = 40.0
MIN_ETA_SPEED_KPH = 25.0
ROAD_DISTANCE_FACTOR = 1.3

# Persist flags
FLAG_TO_INCIDENT = (
    ("transition_overdue", "TRANSITION_OVERDUE"),
    ("commitment_risk", "COMMITMENT_RISK"),
    ("stale_gps", "STALE_GPS"),
    ("status_gps_conflict", "STATUS_GPS_CONFLICT"),
    ("dropoff_risk", "DROPOFF_RISK"),
)

DEMO_ACTIONS = {
    "TRANSITION_OVERDUE": "CHECK_STATUS_WITH_CARRIER",
    "COMMITMENT_RISK": "ESCALATE_CUSTOMER_COMMITMENT",
    "STALE_GPS": "VERIFY_TRACKER",
    "STATUS_GPS_CONFLICT": "VERIFY_LOCATION_VS_STATUS",
    "DROPOFF_RISK": "NOTIFY_CUSTOMER_OF_RISK",
}

