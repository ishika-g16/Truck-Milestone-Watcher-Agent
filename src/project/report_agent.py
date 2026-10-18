"""Report agent as the last step of a watcher wake: aggregate LoadResults into a HTML ops report."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime

from project.constants import DEMO_ACTIONS, now_ist_naive
from project.data.types import LoadResult, Domino
from project.report import write_html

DEFAULT_MODEL = "claude-sonnet-4-6"

INCIDENT_REASON = {
    "TRANSITION_OVERDUE": (
        "The planned milestone plus the 30-minute operational grace has passed "
        "and status has not advanced."
    ),
    "COMMITMENT_RISK": (
        "The customer appointment is at risk on the current ETA, or is already "
        "past with no arrival evidence."
    ),
    "STALE_GPS": (
        "The live tracker has not pinged in over an hour, so the truck is untrackable."
    ),
    "STATUS_GPS_CONFLICT": (
        "Live GPS does not match the stop implied by the current claimed status."
    ),
    "DROPOFF_RISK": (
        "A delay earlier in the journey has propagated to the AT_DROPOFF state and "
        "now endangers the dropoff commitment time."
    )
}

ACTION_PLAIN = {
    "CHECK_STATUS_WITH_CARRIER": "Call the carrier and confirm the current milestone.",
    "ESCALATE_CUSTOMER_COMMITMENT": "Escalate the customer appointment (at risk or missed).",
    "VERIFY_TRACKER": "Check why the live tracker has gone silent.",
    "VERIFY_LOCATION_VS_STATUS": "Reconcile claimed status with the live GPS location.",
    "NOTIFY_CUSTOMER_OF_RISK": "Call the customer and inform about the possible delay in dropoff."
}

SYSTEM_PROMPT = """
You are the report agent for a truck-milestone watcher used by a logistics ops team.

The watcher is in shadow-mode only: recommend actions, never claim that anything was
updated in the ops system. Use only facts in the briefing JSON. Do not invent loads,
times, vehicles, or incidents.

Write a report that can be read in about 2 minutes. Prefer grouping similar issues over a
per-load dump. Use load_num when present (ops reads load numbers, not internal ids).
Mention vehicle numbers when relevant. Do not write paragraphs of grouped loads together -
present them as organized, bulleted points.

When an investigation is present, prefer its overall_severity, finding narratives, and
recommended_action over the raw flag names. Rank CRITICAL then ELEVATED then WATCH.
Do not highlight suppressed findings.

Priority order (when investigation is missing, fall back to flags):
1. investigator CRITICAL / customer commitment BREACHED
2. investigator ELEVATED / STATUS_GPS_CONFLICT
3. TRANSITION_OVERDUE
4. commitment AT_RISK
5. STALE_GPS

Plain language:
- TRANSITION_OVERDUE = expected milestone not confirmed (planned + 30 minutes)
- COMMITMENT_RISK / BREACHED = customer appointment at risk or missed
- STALE_GPS = live tracker silent more than 1 hour (FASTAG is not live)
- STATUS_GPS_CONFLICT = claimed stop vs live location disagree

Call submit_ops_report with:
- headline: one line
- summary: what happened this wake
- reasoning: why the flagged loads matter, in operational terms
- priority_actions: grouped work for the team (title, action, why, load_ids)
- load_notes: one-sentence assessment per attention-queue load_id only
"""

TOOLS = {
    "name": "submit_ops_report",
    "description": "Submit the organized ops report narrative for this wake.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "summary": {"type": "string"},
            "reasoning": {"type": "string"},
            "priority_actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "action": {"type": "string"},
                        "why": {"type": "string"},
                        "load_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["title", "action", "why", "load_ids"],
                },
            },
            "load_notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "load_id": {"type": "string"},
                        "assessment": {"type": "string"},
                        "action": {"type": "string"},
                    },
                    "required": ["load_id", "assessment"],
                },
            },
        },
        "required": ["headline", "summary", "reasoning", "priority_actions"],
    },
}


def _clock(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


INV_RANK = {"CRITICAL": 400, "ELEVATED": 300, "WATCH": 200, "CLEAR": 0}


def _severity(result: LoadResult) -> int:
    if result.active_state is None:
        return 0
    flags = result.flags
    watch = result.watch
    score = 0
    investigation = result.investigation
    if investigation:
        score += INV_RANK.get(investigation.overall_severity, 0)
    if watch and watch.commitment_state == "BREACHED":
        score += 100
    elif watch and watch.commitment_state == "AT_RISK":
        score += 80
    if flags.dropoff_risk:
        score += 70 if (result.projection and result.projection.dropoff_risk == "BREACH_LIKELY") else 60
    if flags.status_gps_conflict:
        score += 50
    if flags.transition_overdue:
        score += 60
    if flags.stale_gps:
        score += 30
    if watch and watch.evidence_state == "UNMAPPED_STATUS":
        score += 20
    if result.incidents:
        score += 5 * len(result.incidents)
    return score


def needs_attention(result: LoadResult) -> bool:
    if result.active_state is None:
        return False
    investigation = result.investigation
    if investigation is not None:
        unsuppressed = any(not f.suppressed for f in investigation.findings)
        if investigation.overall_severity == "CLEAR" and not unsuppressed:
            return False
        return unsuppressed or investigation.overall_severity in {
            "WATCH",
            "ELEVATED",
            "CRITICAL",
        }
    flags = result.flags
    return bool(result.incidents) or any(
        (
            flags.transition_overdue,
            flags.commitment_risk,
            flags.stale_gps,
            flags.status_gps_conflict,
            flags.dropoff_risk
        )
    )


@dataclass
class SnapshotFinding:
    flag_type: str
    severity: str | None
    narrative: str | None


@dataclass
class JourneyRow:
    """One of the load's 5 ledger rows, clocks stringified for the report."""
    state: str
    next_state: str | None
    planned: str | None
    committed: str | None
    forecasted: str | None
    actual: str | None
    completed: bool


@dataclass
class JourneyFlag:
    """One incident (open or resolved) placed at the canonical row it fired on."""
    state: str | None
    incident_type: str | None
    opened_at: str | None
    resolved_at: str | None
    status: str | None
    severity: str | None
    narrative: str | None
    suppressed: bool
    times_seen: int | None = None


def _incident_state(correlation_key: str | None) -> str | None:
    """correlation_key is 'load_id | state | incident_type'."""
    parts = [p.strip() for p in (correlation_key or "").split("|")]
    return parts[1] if len(parts) == 3 and parts[1] else None


@dataclass
class LoadSnapshot:
    load_id: str
    load_num: str | None
    vh_num: str | None
    carrier_name: str | None
    shipper_name: str | None
    origin: str | None
    destination: str | None
    active_state: str | None
    watch_state: str | None
    progress_state: str | None
    commitment_state: str | None
    evidence_state: str | None 
    severity: int
    band: str
    incident_types: list[str]
    recommended_actions: list[str]
    planned: str | None
    committed: str | None
    forecasted: str | None
    actual: str | None
    overall_severity: str | None = None
    investigation_action: str | None = None
    finding_narratives: list[str] = field(default_factory=list)
    findings: list[SnapshotFinding] = field(default_factory=list)
    journey_rows: list[JourneyRow] = field(default_factory=list)
    journey_flags: list[JourneyFlag] = field(default_factory=list)
    domino: Domino | None = None
    incident_wakes: int | None = None  # max times_seen across currently-open incidents
    incident_is_new: bool = False      # True when every open incident started this wake
    # per-milestone projected times (state -> clock string), straight from DelayProjection -
    # the report's "what to expect" forecast table. Domino itself only carries the single
    # dropoff number, not one per state, so this reads result.projection directly.
    projected_milestones: dict[str, str] | None = None

    def label(self) -> str:
        return self.load_num or self.load_id


@dataclass
class WakeBriefing:
    generated_at: str
    totals: dict
    incident_counts: dict[str, int]
    attention: list[LoadSnapshot]
    completed: list[LoadSnapshot]
    healthy: list[LoadSnapshot]


@dataclass
class PriorityAction:
    title: str
    action: str
    why: str
    load_ids: list[str]


@dataclass
class LoadNote:
    load_id: str
    assessment: str
    action: str | None = None


@dataclass
class ReportNarrative:
    headline: str
    summary: str
    reasoning: str
    priority_actions: list[PriorityAction] = field(default_factory=list)
    load_notes: list[LoadNote] = field(default_factory=list)
    source: str = "deterministic"


def _snapshot(result: LoadResult) -> LoadSnapshot:
    load = result.load
    watch = result.watch
    row = result.active_row
    incidents = result.incidents or []
    investigation = result.investigation
    unsuppressed = [
        f for f in (investigation.findings if investigation else []) if not f.suppressed
    ]
    if result.active_state is None:
        band = "completed"
    elif needs_attention(result):
        band = "attention"
    else:
        band = "healthy"
    incident_types = (
        [f.flag_type for f in unsuppressed]
        if investigation
        else [i.incident_type for i in incidents if i.incident_type]
    )

    incident_wakes = max((i.times_seen or 1) for i in incidents) if incidents else None
    incident_is_new = incident_wakes in (None, 1)

    domino = None
    if investigation and investigation.domino:
        domino = investigation.domino
    elif result.projection and result.projection.dropoff_risk in {"AT_RISK", "BREACH_LIKELY"}:
        p = result.projection
        domino = Domino(
            dropoff_risk        = p.dropoff_risk,
            dropoff_buffer      = p.dropoff_buffer,
            projected_delay_min = -p.dropoff_buffer if p.dropoff_buffer is not None else None,
            preventive_actions  = [],
            affected_states     = p.affected_states or [],
        )

    projected_milestones = (
        {state: _clock(dt) for state, dt in result.projection.projected.items()}
        if result.projection and result.projection.projected
        else None
    )

    return LoadSnapshot(
        load_id=result.load_id,
        load_num=(load.load_num if load else None) or (watch.load_num if watch else None),
        vh_num=(load.vh_num if load else None) or (watch.vh_num if watch else None),
        carrier_name=load.carrier_name if load else None,
        shipper_name=load.shipper_name if load else None,
        origin=load.origin if load else None,
        destination=load.destination if load else None,
        active_state=result.active_state,
        watch_state=watch.watch_state if watch else None,
        progress_state=watch.progress_state if watch else None,
        commitment_state=watch.commitment_state if watch else None,
        evidence_state=watch.evidence_state if watch else None,
        domino=domino,
        incident_wakes=incident_wakes,
        incident_is_new=incident_is_new,
        projected_milestones=projected_milestones,
        severity=_severity(result),
        band=band,
        incident_types=incident_types,
        recommended_actions=[
            i.recommended_action for i in incidents if i.recommended_action
        ],
        planned=_clock(row.planned) if row else None,
        committed=_clock(row.committed) if row else None,
        forecasted=_clock(row.forecasted) if row else None,
        actual=_clock(row.actual) if row else None,
        overall_severity=investigation.overall_severity if investigation else None,
        investigation_action=(
            investigation.recommended_action if investigation and investigation.recommended_action else None
        ),
        finding_narratives=[f.narrative for f in unsuppressed if f.narrative],
        findings=[
            SnapshotFinding(f.flag_type, f.severity, f.narrative) for f in unsuppressed
        ],
        journey_rows=[
            JourneyRow(
                state=r.current_state,
                next_state=r.next_state,
                planned=_clock(r.planned),
                committed=_clock(r.committed),
                forecasted=_clock(r.forecasted),
                actual=_clock(r.actual),
                completed=bool(r.completed),
            )
            for r in (result.journey_rows or [])
        ],
        journey_flags=[
            JourneyFlag(
                state=_incident_state(inc.correlation_key),
                incident_type=inc.incident_type,
                opened_at=_clock(inc.opened_at),
                resolved_at=_clock(inc.resolved_at),
                status=inc.status,
                severity=inc.severity,
                narrative=inc.narrative,
                suppressed=bool(inc.suppressed),
                times_seen=inc.times_seen,
            )
            for inc in (result.incident_history or [])
        ],
    )


def aggregate(
    results: list[LoadResult],
    now: datetime | None = None,
) -> WakeBriefing:
    """Turn this wake's LoadResults into a structured briefing."""
    snaps = [_snapshot(r) for r in results]
    attention = sorted(
        [s for s in snaps if s.band == "attention"],
        key=lambda s: s.severity,
        reverse=True,
    )
    completed = [s for s in snaps if s.band == "completed"]
    healthy = [s for s in snaps if s.band == "healthy"]
    incident_counts = Counter()
    for snap in attention:
        incident_counts.update(snap.incident_types)
    generated = now or now_ist_naive()
    return WakeBriefing(
        generated_at=generated.strftime("%Y-%m-%d %H:%M IST"),
        totals={
            "evaluated": len(snaps),
            "attention": len(attention),
            "completed": len(completed),
            "healthy": len(healthy),
            "open_incidents": sum(incident_counts.values()),
            "new_this_wake": sum(
                1 for s in attention if s.incident_is_new or not s.incident_wakes
            ),
        },
        incident_counts=dict(incident_counts),
        attention=attention,
        completed=completed,
        healthy=healthy,
    )


def _llm_payload(briefing: WakeBriefing) -> dict:
    """Compact JSON for the model: full attention queue, counts elsewhere."""

    def _slim(snap: LoadSnapshot) -> dict:
        d = asdict(snap)
        # journey_* is render-only detail for the report diagram; keep it out of the prompt
        d.pop("journey_rows", None)
        d.pop("journey_flags", None)
        return d

    return {
        "generated_at": briefing.generated_at,
        "totals": briefing.totals,
        "incident_counts": briefing.incident_counts,
        "attention": [_slim(s) for s in briefing.attention],
        "completed_labels": [s.label() for s in briefing.completed],
        "healthy_count": len(briefing.healthy),
        "action_catalog": DEMO_ACTIONS,
        "INCIDENT_REASON": INCIDENT_REASON,
    }


def _deterministic_narrative(briefing: WakeBriefing) -> ReportNarrative:
    """Used if the LLM is unavailable or cannot generate a report."""
    totals = briefing.totals
    n_att = totals["attention"]
    if totals["evaluated"] == 0:
        headline = "No loads evaluated this wake"
        summary = "The watcher did not return any LoadResults."
        reasoning = "Nothing to recommend until the next wake sees assigned loads."
        return ReportNarrative(headline, summary, reasoning, source="deterministic")

    if n_att == 0:
        headline = "No loads need attention"
        summary = (
            f"{totals['evaluated']} loads evaluated. "
            f"{totals['completed']} completed this wake. "
            f"{totals['healthy']} watches are healthy with no open incidents."
        )
        reasoning = (
            "No unsuppressed findings remain after investigation, or no flags "
            "fired on any active watch evaluated this wake."
        )
        return ReportNarrative(headline, summary, reasoning, source="deterministic")

    parts = []
    for kind, count in briefing.incident_counts.items():
        parts.append(f"{count} {kind.replace('_', ' ').lower()}")
    mix = ", ".join(parts) if parts else "open incidents"
    headline = f"{n_att} load{'s' if n_att != 1 else ''} need attention"
    summary = (
        f"{totals['evaluated']} loads evaluated this wake. "
        f"{n_att} need ops follow-up ({mix}). "
        f"{totals['completed']} completed. "
        f"{totals['healthy']} remain healthy."
    )
    reasoning = (
        "Attention is ranked by investigator severity (CRITICAL, then ELEVATED, then WATCH), "
        "with customer commitment and flag weights as tie-breakers. "
        "Each recommended action prefers the investigator when severity is elevated."
    )

    grouped: dict[str, list[str]] = defaultdict(list)
    for snap in briefing.attention:
        types = snap.incident_types or ["UNCLASSIFIED"]
        for incident_type in types:
            grouped[incident_type].append(snap.load_id)

    order = (
        "COMMITMENT_RISK",
        "STATUS_GPS_CONFLICT",
        "TRANSITION_OVERDUE",
        "STALE_GPS",
    )
    actions = []
    for incident_type in order:
        load_ids = grouped.pop(incident_type, [])
        if not load_ids:
            continue
        demo = DEMO_ACTIONS.get(incident_type, "REVIEW_LOAD")
        actions.append(
            PriorityAction(
                title=incident_type.replace("_", " ").title(),
                action=ACTION_PLAIN.get(demo, demo),
                why=INCIDENT_REASON.get(incident_type, ""),
                load_ids=load_ids,
            )
        )
    for incident_type, load_ids in grouped.items():
        demo = DEMO_ACTIONS.get(incident_type, "REVIEW_LOAD")
        actions.append(
            PriorityAction(
                title=incident_type.replace("_", " ").title(),
                action=ACTION_PLAIN.get(demo, demo),
                why=INCIDENT_REASON.get(incident_type, "Review this load with the carrier."),
                load_ids=load_ids,
            )
        )

    notes = []
    for snap in briefing.attention:
        bits = [t.replace("_", " ").lower() for t in snap.incident_types]
        issue = ", ".join(bits) if bits else "watch health is degraded"
        clocks = []
        if snap.planned:
            clocks.append(f"planned {snap.planned}")
        if snap.committed:
            clocks.append(f"appointment {snap.committed}")
        clock_txt = f" ({'; '.join(clocks)})" if clocks else ""
        demo_action = snap.recommended_actions[0] if snap.recommended_actions else None
        action = snap.investigation_action or (
            ACTION_PLAIN.get(demo_action, demo_action) if demo_action else None
        )
        if snap.finding_narratives:
            assessment = " ".join(snap.finding_narratives)
        else:
            assessment = (
                f"{snap.label()} is {snap.active_state or 'unknown'} with {issue}{clock_txt}."
            )
        notes.append(
            LoadNote(
                load_id=snap.load_id,
                assessment=assessment,
                action=action,
            )
        )
    return ReportNarrative(
        headline=headline,
        summary=summary,
        reasoning=reasoning,
        priority_actions=actions,
        load_notes=notes,
        source="deterministic",
    )


def _narrative_from_tool(raw: dict, fallback: ReportNarrative) -> ReportNarrative:
    actions = []
    for item in raw.get("priority_actions") or []:
        if not isinstance(item, dict):
            continue
        load_ids = item.get("load_ids") or []
        if not isinstance(load_ids, list):
            load_ids = []
        actions.append(
            PriorityAction(
                title=str(item.get("title") or "Action"),
                action=str(item.get("action") or ""),
                why=str(item.get("why") or ""),
                load_ids=[str(x) for x in load_ids],
            )
        )
    notes = []
    for item in raw.get("load_notes") or []:
        if not isinstance(item, dict) or not item.get("load_id"):
            continue
        notes.append(
            LoadNote(
                load_id=str(item["load_id"]),
                assessment=str(item.get("assessment") or ""),
                action=str(item["action"]) if item.get("action") else None,
            )
        )
    return ReportNarrative(
        headline=str(raw.get("headline") or fallback.headline),
        summary=str(raw.get("summary") or fallback.summary),
        reasoning=str(raw.get("reasoning") or fallback.reasoning),
        priority_actions=actions or fallback.priority_actions,
        load_notes=notes or fallback.load_notes,
        source="llm",
    )


def _llm_narrative(briefing: WakeBriefing) -> ReportNarrative:
    import time

    from anthropic import Anthropic

    from project.llm_metrics import record_llm_call

    client = Anthropic()
    model = os.environ.get("REPORT_MODEL", DEFAULT_MODEL)
    user = (
        "Produce the ops report for this watcher wake.\n\n"
        + json.dumps(_llm_payload(briefing), indent=2)
    )
    kwargs = dict(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[TOOLS],
        tool_choice={"type": "tool", "name": "submit_ops_report"},
        messages=[{"role": "user", "content": user}],
    )
    t0 = time.monotonic()
    try:
        message = client.messages.create(**kwargs, thinking={"type": "disabled"})
    except TypeError:
        message = client.messages.create(**kwargs)
    latency_ms = int((time.monotonic() - t0) * 1000)
    usage = getattr(message, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0

    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_ops_report":
            raw = block.input if isinstance(getattr(block, "input", None), dict) else {}
            record_llm_call(
                agent="report",
                model=model,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            return _narrative_from_tool(raw, _deterministic_narrative(briefing))
    raise RuntimeError("report agent: model did not call submit_ops_report")


def generate_narrative(briefing: WakeBriefing) -> ReportNarrative:
    fallback = _deterministic_narrative(briefing)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Report agent: no ANTHROPIC_API_KEY, writing structured summary without LLM.")
        return fallback
    try:
        narrative = _llm_narrative(briefing)
        print("Report agent: LLM narrative attached.")
        return narrative
    except Exception as exc:
        print(f"Report agent: LLM failed ({exc}); writing structured summary without LLM.")
        return fallback


def run_report_agent(
    results: list[LoadResult],
    now: datetime | None = None,
    output_path: str = "report.html",
) -> None:
    """Aggregate this wake and write an organized ops report to report.html."""
    print("\nReport agent is running...\n")
    try:
        briefing = aggregate(results, now=now)
        narrative = generate_narrative(briefing)
        write_html(briefing, narrative, output_path=output_path)
    except Exception as exc:
        print(f"Report agent failed: {exc}")
        return
    print(
        f"Report agent: {briefing.totals['evaluated']} loads, "
        f"{briefing.totals['attention']} need attention, "
        f"narrative={narrative.source}." # llm or deterministic
    )