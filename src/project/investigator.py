"""Per-load investigator: LLM triage of fired flags against clocks and memory."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from project.constants import FLAG_TO_INCIDENT, LIVE_GPS_SOURCES
from project.data.types import (
    Domino,
    Finding,
    FlagSet,
    GPSData,
    Incident,
    Investigation,
    LoadData,
    LoadMemory,
    StateRow,
    Watch,
)
from project.internal.store import InternalStore

DEFAULT_MODEL = "claude-sonnet-4-6"

SEVERITIES = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
OVERALL = frozenset({"CLEAR", "WATCH", "ELEVATED", "CRITICAL"})

TOOLS = {
    "name": "submit_investigation",
    "description": "Submit the triage decision for this load.",
    "input_schema": {
        "type": "object",
        "properties": {
            "overall_severity": {
                "type": "string",
                "enum": ["CLEAR", "WATCH", "ELEVATED", "CRITICAL"],
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "flag_type": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                        },
                        "suppressed": {"type": "boolean"},
                        "suppression_reason": {"type": ["string", "null"]},
                        "narrative": {"type": ["string", "null"]},
                    },
                    "required": ["flag_type", "severity", "suppressed"],
                },
            },
            "domino": {
                "type": ["object", "null"],
                "properties": {
                    "dropoff_risk": {"enum": ["SAFE", "AT_RISK", "BREACH_LIKELY", "UNKNOWN"]},
                    "dropoff_buffer": {"type": ["integer", "null"]},
                    "projected_delay_min": {"type": ["integer", "null"]},
                    "affected_states": {"type": "array", "items": {"type": "string"}},
                    "preventive_actions": {"type": "array", "items": {"type": "string"}},
                    "customer_alert": {"type": ["string", "null"]},
                },
                "required": ["dropoff_risk"]
            },
            "recommended_action": {"type": "string"},
            "confidence": {"type": "number"},
            "reasoning": {"type": "string"},
        },
        "required": [
            "overall_severity",
            "findings",
            "recommended_action",
            "confidence",
            "reasoning",
        ],
    },
}

SYSTEM_PROMPT = """
You are an investigator for a freight operations watcher. You assess whether
fired flags on a load represent genuine operational concerns, calibrated against
historical patterns (when available) and this load's clocks and GPS.

The watcher is shadow-mode only: recommend actions, never claim anything was
updated in the ops system. Use only facts in the load context JSON.

You receive:
- Flags computed deterministically this wake
- Active-row clocks (planned, committed, forecasted, actual)
- Remaining incomplete journey rows
- GPS source/age (FASTAG and missing live GPS are GPS_UNAVAILABLE, not STALE_GPS)
- Historical carrier/lane/shipper patterns only when sample_count >= 3
- Similar closed loads for this carrier, if any
- Your prior TRIAGE decisions for this same load, if any

Flag meanings (match the code, not folklore):
- TRANSITION_OVERDUE: now > planned + 30 minutes on the active row. There is no
  extra per-state due buffer beyond what is already in planned. If planned is
  missing, progress_state is UNAVAILABLE and this flag does not fire (unknown,
  not healthy).
- COMMITMENT_RISK: ENROUTE only. AT_RISK if forecasted > committed; BREACHED if
  now > committed and actual is missing. Treat BREACHED as unrecoverable for
  this appointment.
- STALE_GPS: live tracker (TELEMATICS, MOBILE_APP, CELLULAR_TRACKING) last ping
  older than 1 hour. FASTAG is not live and does not fire this flag.
- STATUS_GPS_CONFLICT: live GPS vs claimed stop, 1 km radius. ENROUTE_PICKUP
  near pickup is a conflict; AT_PICKUP not near pickup is a conflict;
  ENROUTE_DROPOFF near dropoff is a conflict. GPS never advances status.
- DROPOFF_RISK: a deterministic delay rolled the active row's delay onto all 
  subsequent milestones. Fires when projected dropoff arrival is later than
  committed dropoff appointment (AT_RISK within DROPOFF_RISK_MARGIN, BREACH_LIKELY
  beyond it).

Your responsibilities:
1. For each fired flag, decide if it is genuinely concerning or within normal
   variance given the patterns. Suppress it if it is noise.
2. Assess overall severity: CLEAR | WATCH | ELEVATED | CRITICAL
3. Write a plain-language narrative per unsuppressed finding
4. Provide a specific recommended_action if severity >= ELEVATED (who calls whom,
   what to check). Otherwise a short monitoring note is enough.
   The watcher evaluates every assigned load every wake; do not tell ops that
   the system will skip this load.
5. When domino_delay is present, reflect on the WHOLE remaining journey:
    - name the affected downstream states (narrow delay_cascade.affected_states
    if history suggests some absorb the delay)
    - restate the dropoff verdict and by how many minutes late is the revised ETA
    - give 2-3 concrete preventive_actions to protect the appointment

Calibration:
- A delay near this carrier/lane/shipper avg_delta_min is LOW unless commitment
  is BREACHED.
- If commitment_state is BREACHED, minimum overall severity is CRITICAL.
- If prior wakes show escalating severity on this load, weight toward higher.
- STALE_GPS alone without TRANSITION_OVERDUE is not ELEVATED or above.
- Empty patterns/episodes means no history yet - do not invent it.
- `wakes_open` on each open_incident is the exact, ground-truth count of consecutive
  wakes that incident has been open - use it verbatim if you state a wake count.
  Never estimate, extrapolate, or increment a wake count from your own prior
  narrative; if `wakes_open` isn't present for something, don't state a specific
  number for it.

Call submit_investigation with the structured result.
"""


def flags_firing(flags: FlagSet) -> list[str]:
    return [
        incident_type
        for attr, incident_type in FLAG_TO_INCIDENT
        if getattr(flags, attr)
    ]


def should_investigate(flags: FlagSet) -> bool:
    return bool(flags_firing(flags))


def _fmt(value) -> str | None:
    """Make datetimes readable and JSON-safe for LLM"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)

def to_int_or_none(v):
    try: return int(v)
    except (TypeError, ValueError): return None 


def _minutes_between(later: datetime | None, earlier: datetime | None) -> int | None:
    if later is None or earlier is None:
        return None
    try:
        return int((later - earlier).total_seconds() / 60)
    except TypeError:
        return None


def _fallback(load_id: str, flags: FlagSet, projection=None) -> Investigation:
    """In case the LLM is unavailable, set defaults to return a valid Investigation object"""
    findings = [
        Finding(
            flag_type=flag_type,
            severity="MEDIUM",
            suppressed=False,
            suppression_reason=None,
            narrative=None,
        )
        for flag_type in flags_firing(flags)
    ]

    domino = None
    if flags.dropoff_risk and projection is not None:
        domino = Domino(
            dropoff_risk        = projection.dropoff_risk,
            dropoff_buffer      = projection.dropoff_buffer,
            projected_delay_min = -projection.dropoff_buffer if projection.dropoff_buffer is not None else None,
            preventive_actions  = ["Call the carrier and confirm current position and ETA."],
            affected_states     = projection.affected_states or [],
        )
        if domino.dropoff_risk in {"AT_RISK", "BREACH_LIKELY"}:
            domino.customer_alert = (
                f"Load {load_id} may be delayed to the dropoff appointment; "
                "investigator unavailable, recommend manual review."
            )

    return Investigation(
        load_id=load_id,
        overall_severity="WATCH",
        findings=findings,
        recommended_action="Review this load with the carrier.",
        confidence=0.0,
        reasoning="Investigator unavailable; flags left unsuppressed.",
        domino = domino
    )


def build_context(
    load: LoadData,
    active_row: StateRow,
    flags: FlagSet,
    memory: LoadMemory,
    now: datetime,
    gps: GPSData | None,
    watch: Watch | None,
    state_rows: list[StateRow] | None,
    incidents: list[Incident] | None,
    projection,
) -> dict:
    """Assemble the JSON context handed to the investigator LLM: load/active-row facts,
    remaining incomplete rows, this wake's flags, GPS, open incidents, calibrated memory,
    and the domino delay-cascade projection (if any)."""
    source = (gps.data_source or "").upper() if gps else None
    live = bool(source and source in LIVE_GPS_SOURCES)
    remaining = []
    for row in state_rows or []:
        if row.completed:
            continue
        remaining.append(
            {
                "state": row.current_state,
                "planned": _fmt(row.planned),
                "committed": _fmt(row.committed),
                "forecasted": _fmt(row.forecasted),
                "actual": _fmt(row.actual),
            }
        )
    time_in_state = _minutes_between(now, active_row.planned)
    if time_in_state is None and watch is not None:
        time_in_state = _minutes_between(now, watch.created_at)

    domino_delay = None
    if projection is not None:
        domino_delay = {
            "delay_min": projection.delay,
            "affected_states": projection.affected_states,
            "projected": {state: _fmt(dt) for state, dt in projection.projected.items()},
            "dropoff_risk": projection.dropoff_risk,
            "dropoff_buffer": projection.dropoff_buffer,
        }

    return {
        "load": {
            "load_id": load.load_id,
            "load_num": load.load_num,
            "carrier": load.carrier_name,
            "shipper": load.shipper_name,
            "origin": load.origin,
            "destination": load.destination,
            "vehicle": load.vh_num,
            "current_state": active_row.current_state,
            "source_status": load.source_status,
            "source_sub_status": load.source_sub_status,
            "canonical_status": load.canonical_status,
        },
        "active_row": {
            "planned": _fmt(active_row.planned),
            "committed": _fmt(active_row.committed),
            "forecasted": _fmt(active_row.forecasted),
            "actual": _fmt(active_row.actual),
            "time_in_state_minutes": time_in_state,
        },
        "remaining_rows": remaining,
        "flags": {
            "transition_overdue": flags.transition_overdue,
            "commitment_risk": flags.commitment_risk,
            "stale_gps": flags.stale_gps,
            "status_gps_conflict": flags.status_gps_conflict,
            "progress_state": flags.progress_state,
            "commitment_state": flags.commitment_state,
            "evidence_state": flags.evidence_state,
            "firing": flags_firing(flags),
        },
        "gps": {
            "data_source": source,
            "live": live,
            "city": gps.city_name if gps else None,
            "last_activity": _fmt(gps.last_activity) if gps else None,
            "age_minutes": _minutes_between(now, gps.last_activity) if gps else None,
        },
        "open_incidents": [
            {
                "type": i.incident_type,
                "severity": i.severity,
                "narrative": i.narrative,
                "opened_at": _fmt(i.opened_at),
                "wakes_open": i.times_seen,
            }
            for i in (incidents or [])
        ],
        "memory": {
            "patterns": {
                k: {
                    "avg_delta_min": v.avg_delta_min,
                    "sample_count": v.sample_count,
                    "incident_rate": v.incident_rate,
                    "commitment_breach_rate": v.commitment_breach_rate,
                }
                for k, v in memory.patterns.items()
            },
            "similar_episodes": [
                {
                    "load_id": e.load_id,
                    "commitment_met": e.commitment_met,
                    "total_delay_min": e.total_delay_min,
                    "highest_severity": e.highest_severity,
                    "state_actuals": e.state_actuals,
                }
                for e in memory.similar_episodes
            ],
            "prior_wake_decisions": memory.prior_wake_decisions,
        },
        "domino_delay": domino_delay
    }


def _parse_investigation(raw: dict, load_id: str, flags: FlagSet) -> Investigation:
    firing = set(flags_firing(flags))
    findings: list[Finding] = []
    seen: set[str] = set()
    for item in raw.get("findings") or []:
        if not isinstance(item, dict):
            continue
        flag_type = str(item.get("flag_type") or "")
        if flag_type not in firing or flag_type in seen:
            continue
        seen.add(flag_type)
        severity = str(item.get("severity") or "MEDIUM").upper()
        if severity not in SEVERITIES:
            severity = "MEDIUM"
        findings.append(
            Finding(
                flag_type=flag_type,
                severity=severity,
                suppressed=bool(item.get("suppressed")),
                suppression_reason=item.get("suppression_reason") or None,
                narrative=item.get("narrative") or None,
            )
        )
    for flag_type in firing:
        if flag_type not in seen:
            findings.append(
                Finding(
                    flag_type=flag_type,
                    severity="MEDIUM",
                    suppressed=False,
                    suppression_reason=None,
                    narrative=None,
                )
            )
    overall = str(raw.get("overall_severity") or "WATCH").upper()
    if overall not in OVERALL:
        overall = "WATCH"
    try:
        confidence = float(raw.get("confidence") if raw.get("confidence") is not None else 0)
    except (TypeError, ValueError):
        confidence = 0.0

    domino = None
    raw_domino = raw.get("domino")
    if isinstance(raw_domino, dict) and "DROPOFF_RISK" in firing:
        risk = str(raw_domino.get("dropoff_risk") or "UNKNOWN").upper()
        if risk not in {"SAFE", "AT_RISK", "BREACH_LIKELY", "UNKNOWN"}:
            risk = "UNKNOWN"
        buffer_min = to_int_or_none(raw_domino.get("dropoff_buffer"))
        delay_min = to_int_or_none(raw_domino.get("projected_delay_min"))
        if delay_min is None and buffer_min is not None:
            # the model sometimes omits projected_delay_min even when it gives a
            # confident dropoff_risk - dropoff_buffer is the more reliable field,
            # so backfill from it rather than losing the number.
            delay_min = -buffer_min
        domino = Domino(
            dropoff_risk=risk,
            dropoff_buffer=buffer_min,
            projected_delay_min=delay_min,
            preventive_actions=[str(a) for a in raw_domino.get("preventive_actions") or []],
            affected_states=[str(s) for s in raw_domino.get("affected_states") or []],
            customer_alert=str(raw_domino["customer_alert"]) if raw_domino.get("customer_alert") else None,
        )
    
    return Investigation(
        load_id=load_id,
        overall_severity=overall,
        findings=findings,
        recommended_action=str(raw.get("recommended_action") or ""),
        confidence=max(0.0, min(1.0, confidence)),
        reasoning=str(raw.get("reasoning") or ""),
        domino=domino
    )


def _usage_tokens(message) -> tuple[int, int]:
    usage = getattr(message, "usage", None)
    if usage is None:
        return 0, 0
    return int(getattr(usage, "input_tokens", 0) or 0), int(
        getattr(usage, "output_tokens", 0) or 0
    )


def _llm_investigate(context: dict) -> tuple[dict, str, int]:
    from anthropic import Anthropic

    from project.llm_metrics import record_llm_call

    client = Anthropic()
    model = os.environ.get("REPORT_MODEL", DEFAULT_MODEL)
    user = "Triage this load.\n\n" + json.dumps(context, indent=2, default=str)
    llm_call = dict(
        model=model,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        tools=[TOOLS],
        tool_choice={"type": "tool", "name": "submit_investigation"},
        messages=[{"role": "user", "content": user}],
    )
    t0 = time.monotonic()
    try:
        message = client.messages.create(**llm_call, thinking={"type": "disabled"})
    except TypeError:
        message = client.messages.create(**llm_call)
    latency_ms = int((time.monotonic() - t0) * 1000)
    input_tokens, output_tokens = _usage_tokens(message)
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_investigation":
            raw = block.input if isinstance(getattr(block, "input", None), dict) else {}
            record_llm_call(
                agent="investigator",
                model=model,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            return raw, json.dumps(raw), latency_ms
    raise RuntimeError("investigator: model did not call submit_investigation")


def investigate(
    store: InternalStore,
    load: LoadData,
    active_row: StateRow,
    flags: FlagSet,
    now: datetime,
    *,
    gps: GPSData | None = None,
    watch: Watch | None = None,
    state_rows: list[StateRow] | None = None,
    incidents: list[Incident] | None = None,
    projection,
) -> Investigation:
    """Run TRIAGE for a load with firing flags. Always returns an Investigation."""
    load_id = load.load_id or ""
    memory = store.build_memory_context(load, active_row.current_state)
    context = build_context(
        load, active_row, flags, memory, now, gps, watch, state_rows, incidents, projection
    )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"Investigator: no ANTHROPIC_API_KEY, fallback for {load_id}")
        return _fallback(load_id, flags, projection)

    try:
        parsed, raw_out, latency_ms = _llm_investigate(context)
        investigation = _parse_investigation(parsed, load_id, flags)
        store.write_investigator_run(
            load_id=load_id,
            input_context=json.dumps(context, default=str),
            output=raw_out,
            timestamp=now,
        )
        print(
            f"Investigator: {load_id} severity={investigation.overall_severity} "
            f"latency_ms={latency_ms}"
        )
        return investigation
    except Exception as exc:
        print(f"Investigator failed for {load_id}: {exc}")
        return _fallback(load_id, flags, projection)
