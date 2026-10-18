from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


# Canonical-status journey for the 5-row state table.
# AT_DROPOFF is the last state - completing it completes the Watch.
JOURNEY_STATES: tuple[str, ...] = (
    "ASSIGNED",
    "ENROUTE_PICKUP",
    "AT_PICKUP",
    "LOADED",
    "ENROUTE_DROPOFF",
    "AT_DROPOFF",
)
TERMINAL_STATE = JOURNEY_STATES[-1]
WATCH_STATES = JOURNEY_STATES[:-1] # state table ends with the row for ENROUTE_DROPOFF -> AT_DROPOFF


# ------------------ OPERATIONAL DATA ------------------

@dataclass
class LoadData:
    load_id: str | None  # load_tender.id
    load_num: str | None
    vh_id: str | None
    vh_num: str | None
    carrier_name: str | None
    shipper_name: str | None

    origin: str | None
    destination: str | None
    pickup_lat: float | None
    pickup_lon: float | None
    dropoff_lat: float | None
    dropoff_lon: float | None

    source_status: str | None  # load_tender.tender_status
    source_sub_status: str | None  # load_tender.sub_status
    canonical_status: str | None  # after normalize_status


@dataclass
class GPSData:
    vh_id: str | None
    vh_num: str | None
    load_id: str | None
    load_num: str | None
    latitude: float | None
    longitude: float | None
    city_name: str | None
    last_activity: datetime | None
    received_at: datetime | None
    data_source: str | None
    speed_kph: float | None = None


@dataclass
class ETAData:
    load_id: str | None
    vh_id: str | None
    ETA_value: datetime | None  # this wake's forecast
    ETA_of: str | None  # "pickup" or "dropoff"
    calculated_at: datetime | None


@dataclass
class TimeData:
    load_id: str | None
    load_num: str | None

    assigned_at: datetime | None  # load_tender.latest_vehicle_assign_time

    committed_pickup_at: datetime | None  # load_stop.appt_start_time AND is_pickup
    committed_dropoff_at: datetime | None  # load_stop.appt_start_time AND is_drop

    planned_pickup_at: datetime | None  # load_stop.arrive_time AND is_pickup
    planned_dropoff_at: datetime | None  # load_stop.arrive_time AND is_drop

    actual_pickup_time: datetime | None  # load_stop.actual_arrive_time AND is_pickup
    actual_dropoff_time: datetime | None  # load_stop.actual_arrive_time AND is_drop
    actual_dep_pickup: datetime | None # load_stop.actual_depart_time AND is_pickup

    actual_loaded_at: datetime | None  # load_stop.arrive_time AND is_loaded


# ------------------ INTERNAL WATCH DATA ------------------

@dataclass
class Watch:
    """Load-level header"""

    watch_id: str | None
    load_id: str | None
    load_num: str | None
    vh_num: str | None

    watch_state: str | None  # ACTIVE | COMPLETED
    progress_state: str | None  # HEALTHY | TRANSITION_OVERDUE | UNAVAILABLE | COMPLETED
    commitment_state: str | None  # NOT_APPLICABLE (state is not ENROUTE)| AT_RISK | BREACHED | UNAVAILABLE (no commitment data for computation)
    evidence_state: str | None  # RELIABLE | STALE_GPS | CONFLICTING | UNMAPPED_STATUS | GPS_UNAVAILABLE

    created_at: datetime | None
    last_evaluated_at: datetime | None


@dataclass
class StateRow:
    """One row in the state table."""

    load_id: str | None
    seq: int  # 0..4, order in WATCH_STATES
    current_state: str
    next_state: str  # last row is ENROUTE_DROPOFF → AT_DROPOFF

    planned: datetime | None
    committed: datetime | None
    forecasted: datetime | None
    actual: datetime | None
    completed: bool = False


@dataclass
class Incident:
    incident_id: str | None
    correlation_key: str | None  # load_id | state | incident_type
    watch_id: str | None
    load_id: str | None

    incident_type: str | None  # TRANSITION_OVERDUE, STALE_GPS, COMMITMENT_RISK, STATUS_GPS_CONFLICT
    status: str | None  # OPEN | RESOLVED

    opened_at: datetime | None
    resolved_at: datetime | None
    last_updated_at: datetime | None

    recommended_action: str | None

    severity: str | None = None
    narrative: str | None = None
    suppressed: bool = False
    times_seen: int = 1  # wakes this incident has been open/reconfirmed; 1 = new this wake


@dataclass
class FlagSet:
    transition_overdue: bool
    commitment_risk: bool
    stale_gps: bool
    status_gps_conflict: bool
    dropoff_risk: bool

    progress_state: str
    commitment_state: str
    evidence_state: str

    @classmethod
    def not_active(cls) -> FlagSet:
        """Watch missing, not ACTIVE, or journey already COMPLETED - no interventions."""
        return cls(
            transition_overdue=False,
            commitment_risk=False,
            stale_gps=False,
            status_gps_conflict=False,
            dropoff_risk=False,
            progress_state="HEALTHY",
            commitment_state="NOT_APPLICABLE",
            evidence_state="RELIABLE",
        )


@dataclass
class Finding:
    flag_type: str
    severity: str  # LOW | MEDIUM | HIGH | CRITICAL
    suppressed: bool
    suppression_reason: str | None = None
    narrative: str | None = None


@dataclass
class Investigation:
    load_id: str
    overall_severity: str  # CLEAR | WATCH | ELEVATED | CRITICAL
    findings: list[Finding]
    recommended_action: str
    confidence: float
    reasoning: str
    domino: Domino | None = None

    


@dataclass
class PatternSummary:
    pattern_key: str
    sample_count: int
    avg_delta_min: float | None
    incident_rate: float | None
    commitment_breach_rate: float | None


@dataclass
class LoadEpisode:
    load_id: str
    closed_at: datetime | None
    commitment_met: bool
    total_delay_min: int | None
    highest_severity: str
    state_actuals: dict
    carrier_name: str | None = None
    origin: str | None = None
    destination: str | None = None


@dataclass
class LoadMemory:
    """Assembled at investigation time. Not persisted as one row."""
    patterns: dict[str, PatternSummary]
    similar_episodes: list[LoadEpisode]
    prior_wake_decisions: list[dict]


@dataclass
class LoadResult:
    """Per-load output of one wake. Feed for the report agent."""
    load_id: str
    active_state: str | None
    watch: Watch
    flags: FlagSet
    incidents: list[Incident]
    projection: DelayProjection | None = None
    load: LoadData | None = None
    active_row: StateRow | None = None
    investigation: Investigation | None = None
    journey_rows: list[StateRow] | None = None  # all 5 ledger rows, for the report journey diagram
    incident_history: list[Incident] | None = None  # open + resolved incidents for this load


@dataclass
class DelayProjection:
    delay: int # active row is behind the planned time by how many minutes
    projected: dict[str, datetime] # [key: milestone, value: projected datetime]
    dropoff_risk: str # SAFE | AT_RISK | BREACH_LIKELY | UNKNOWN 
    dropoff_buffer: int | None 
    affected_states: list[str] | None = None


@dataclass
class Domino:
    dropoff_risk: str # SAFE | AT_RISK | BREACH_LIKELY | UNKNOWN 
    dropoff_buffer: int | None 
    projected_delay_min: int | None = None
    preventive_actions: list[str] | None = None
    affected_states: list[str] | None = None
    customer_alert: str | None = None
