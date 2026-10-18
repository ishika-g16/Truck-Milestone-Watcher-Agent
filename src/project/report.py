"""HTML renderer for the watcher report agent."""

from __future__ import annotations

import re
from datetime import datetime
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from project.report_agent import LoadNote, LoadSnapshot, ReportNarrative, WakeBriefing


def write_html(briefing: WakeBriefing, narrative: ReportNarrative, output_path: str = "report.html") -> None:
    html = build_html(briefing, narrative)
    try:
        with open(output_path, "w", encoding="utf-8") as file:
            file.write(html)
    except OSError as exc:
        print(f"Failed to write report to {output_path}: {exc}")
        raise
    print(f"Wrote report to {output_path}")


def _txt(value) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, datetime):
        return escape(value.strftime("%Y-%m-%d %H:%M"))
    if isinstance(value, bool):
        return "yes" if value else "no"
    return escape(str(value))


def _bullets(text: str) -> str:
    """Split narrative prose into bullet points rather than one dense paragraph - the
    LLM still authors a single blob of text, so this is a crude sentence split, not a
    real restructuring of the content."""
    blob = " ".join((text or "").split())
    if not blob:
        return "<p>-</p>"
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", blob) if s.strip()]
    if len(sentences) <= 1:
        return f"<p>{escape(blob)}</p>"
    return "<ul class='finding-bullets'>" + "".join(
        f"<li>{escape(s)}</li>" for s in sentences
    ) + "</ul>"


def _pill(text: str | None, kind: str) -> str:
    if not text:
        return ""
    return f'<span class="pill {kind}">{escape(text)}</span>'


def _health_kind(dimension: str, value: str | None) -> str:
    if not value:
        return "muted"
    v = value.upper()
    if dimension == "progress":
        if v == "TRANSITION_OVERDUE":
            return "bad"
        if v == "UNAVAILABLE":
            return "muted"
        if v in {"HEALTHY", "COMPLETED"}:
            return "ok"
    if dimension == "commitment":
        if v == "BREACHED":
            return "bad"
        if v == "AT_RISK":
            return "warn"
        if v in {"NOT_APPLICABLE", "UNAVAILABLE"}:
            return "muted"
    if dimension == "evidence":
        if v in {"CONFLICTING", "UNMAPPED_STATUS"}:
            return "bad"
        if v in {"STALE_GPS", "GPS_UNAVAILABLE"}:
            return "warn"
        if v == "RELIABLE":
            return "ok"
    return "muted"


def _severity_kind(value: str | None) -> str:
    if not value:
        return "muted"
    v = value.upper()
    if v == "CRITICAL":
        return "bad"
    if v == "ELEVATED":
        return "warn"
    if v == "WATCH":
        return "muted"
    if v == "CLEAR":
        return "ok"
    return "muted"


def _lane(snap: LoadSnapshot) -> str:
    origin = snap.origin
    dest = snap.destination
    if origin and dest:
        return f"{_txt(origin)} → {_txt(dest)}"
    return _txt(origin or dest)


def _note_map(notes: list[LoadNote]) -> dict[str, LoadNote]:
    return {n.load_id: n for n in notes}


def build_html(briefing: WakeBriefing, narrative: ReportNarrative) -> str:
    totals = briefing.totals
    notes = _note_map(narrative.load_notes)
    source = "Claude" if narrative.source == "llm" else "structured summary (no LLM)"

    page = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8'>",
        "<title>Watcher Ops Report</title>",
        "<style>",
        _CSS,
        "</style>",
        "</head>",
        "<body>",
        "<header>",
        "<p class='kicker'>Truck milestone watcher</p>",
        "<h1>Ops report</h1>",
        "<div class='kpis'>",
        f"<div class='kpi'><strong>{_txt(totals.get('evaluated', 0))}</strong><span>Evaluated</span></div>",
        f"<div class='kpi attention'><strong>{_txt(totals.get('attention', 0))}</strong><span>Need attention</span></div>",
        f"<div class='kpi new-count'><strong>{_txt(totals.get('new_this_wake', 0))}</strong><span>New this wake</span></div>",
        f"<div class='kpi'><strong>{_txt(totals.get('open_incidents', 0))}</strong><span>Open incidents</span></div>",
        f"<div class='kpi'><strong>{_txt(totals.get('completed', 0))}</strong><span>Completed this wake</span></div>",
        f"<div class='kpi'><strong>{_txt(totals.get('healthy', 0))}</strong><span>Healthy</span></div>",
        "</div>",
        "</header>",
        "<main>",
    ]

    page.extend(_attention_section(briefing.attention, notes))
    page.extend(_simple_table_section("Completed this wake", briefing.completed, empty="No watches completed this wake."))
    page.extend(_simple_table_section("Healthy watches", briefing.healthy, empty="No healthy active watches.", use_load_id=True))

    page.extend(
        [
            "</main>",
            "<footer>",
            "<p>Recommendations only. The watcher does not write to the ops database.</p>",
            "</footer>",
            _JOURNEY_SCRIPT,
            _FILTER_SCRIPT,
            "</body></html>",
        ]
    )
    return "\n".join(page)


def _eval_id(load_id: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in load_id)
    return f"eval-{safe}"


def _issue_kind(incident_type: str) -> str:
    v = (incident_type or "").upper()
    if v in {"COMMITMENT_RISK", "STATUS_GPS_CONFLICT", "DROPOFF_RISK"}:
        return "bad"
    if v in {"TRANSITION_OVERDUE", "STALE_GPS"}:
        return "warn"
    return "muted"


def _issue_chips(snap: LoadSnapshot) -> str:
    if not snap.incident_types:
        return _pill("degraded health", "muted")
    return "".join(
        _pill(t.replace("_", " ").title(), _issue_kind(t)) for t in snap.incident_types
    )


def _wake_badge(snap: LoadSnapshot) -> str:
    """'New' when every currently-open incident on this load first fired this wake -
    nothing shown for a load carried over from a prior wake."""
    if snap.incident_is_new or not snap.incident_wakes:
        return _pill("New", "new")
    return ""


def _finding_sev_kind(value: str | None) -> str:
    v = (value or "").upper()
    if v in {"CRITICAL", "HIGH"}:
        return "bad"
    if v == "MEDIUM":
        return "warn"
    return "muted"


def _dropoff_risk_kind(value: str | None) -> str:
    v = (value or "").upper()
    if v == "BREACH_LIKELY":
        return "bad"
    if v == "AT_RISK":
        return "warn"
    if v == "SAFE":
        return "ok"
    return "muted"


def _minutes_delta(later: str | None, earlier: str | None) -> int | None:
    """Signed minute gap between two 'YYYY-MM-DD HH:MM' clock strings, or None if
    either is missing/unparseable."""
    if not later or not earlier:
        return None
    try:
        gap = datetime.strptime(later, "%Y-%m-%d %H:%M") - datetime.strptime(
            earlier, "%Y-%m-%d %H:%M"
        )
    except (TypeError, ValueError):
        return None
    return int(gap.total_seconds() / 60)


def _human_minutes(mins: int) -> str:
    """Signed minute count as a compact, scannable duration - '+2h10m', '-45m',
    '+17d4h'. Long-open incidents can run into tens of thousands of minutes, which
    isn't scannable as a raw number."""
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    days, rem = divmod(mins, 1440)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:  # once we're into days, minutes stop being useful precision
        parts.append(f"{minutes}m")
    if not parts:
        parts.append("0m")
    return sign + "".join(parts)


def _delta_note(later: str | None, earlier: str | None) -> str:
    """_minutes_delta, rendered as a chip."""
    mins = _minutes_delta(later, earlier)
    if mins is None:
        return ""
    if mins == 0:
        return "<span class='delta ok'>on time</span>"
    kind = "bad" if mins > 0 else "ok"
    return f"<span class='delta {kind}'>{_human_minutes(mins)}</span>"


def _clocks_strip(snap: LoadSnapshot) -> str:
    """The active row's four clocks - plain values, no delta chips (they read as
    more confusing than useful next to the journey diagram's own color-coded deltas)."""
    rows = [
        ("Planned", snap.planned),
        ("Appointment", snap.committed),
        ("Forecast", snap.forecasted),
        ("Actual", snap.actual),
    ]
    cells = "".join(
        f"<div class='clock'><span class='clock-k'>{escape(label)}</span>"
        f"<span class='clock-v'>{_txt(value)}</span></div>"
        for label, value in rows
    )
    return f"<div class='clocks'>{cells}</div>"


def _headline(snap: LoadSnapshot) -> str:
    """One deterministic line stating what's wrong and the key number, so a reader
    doesn't have to open a load's findings to know why it's here. Priority mirrors the
    documented flag-priority order (BREACHED -> CONFLICT -> OVERDUE -> AT_RISK -> STALE),
    with a dropoff-risk cascade bumped to the top since it implies a commitment problem."""
    domino = getattr(snap, "domino", None)
    if domino and domino.dropoff_risk in {"AT_RISK", "BREACH_LIKELY"}:
        delay_min = domino.projected_delay_min
        if delay_min is None and domino.dropoff_buffer is not None:
            delay_min = -domino.dropoff_buffer
        delay_txt = _human_minutes(delay_min)[1:] if delay_min is not None else "an unknown amount"
        risk_word = "breach likely" if domino.dropoff_risk == "BREACH_LIKELY" else "at risk"
        return f"Dropoff {risk_word} - projected {delay_txt} late at dropoff"
    if snap.commitment_state == "BREACHED":
        mins = _minutes_delta(snap.forecasted, snap.committed)
        suffix = f" - {_human_minutes(mins)[1:]} past appointment" if mins else ""
        return f"Commitment breached{suffix}"
    incident_types = set(snap.incident_types or [])
    if "STATUS_GPS_CONFLICT" in incident_types:
        return "Status–GPS conflict - live position disagrees with the claimed stop"
    if "TRANSITION_OVERDUE" in incident_types:
        return "Transition overdue - past planned time beyond the grace window"
    if snap.commitment_state == "AT_RISK":
        return "Commitment at risk - forecast is beyond the appointment window"
    if "STALE_GPS" in incident_types:
        return "Stale GPS - no live ping recently"
    if snap.overall_severity:
        return f"{snap.overall_severity.title()} - see findings below"
    return "Needs attention"


def _action_icon(text: str | None) -> str:
    t = (text or "").lower()
    # "call"/"escalate" checked first - "escalate the customer appointment" is an
    # escalation, not a customer-notify message, even though it mentions "customer".
    if "call" in t or "escalate" in t:
        return "📞"
    if "notify" in t:
        return "⚠️"
    if "verify" in t or "investigat" in t or "confirm" in t:
        return "🔍"
    if "monitor" in t:
        return "👀"
    return "▸"


def _action_chip(snap: LoadSnapshot) -> str:
    text = snap.investigation_action or (
        snap.recommended_actions[0] if snap.recommended_actions else None
    )
    if not text:
        return ""
    return (
        f"<div class='action-chip'><span class='ic'>{_action_icon(text)}</span>"
        f"{_txt(_one_sentence(text))}</div>"
    )


def _forecast_table_html(snap: LoadSnapshot, domino) -> str:
    """'What to expect' - one row per downstream milestone still ahead, planned vs
    projected (from DelayProjection.projected, read directly - see
    docs/report_v2_implementation_plan.md, Feature 3), the last row carrying the
    dropoff verdict rather than a plain delta."""
    milestones = snap.projected_milestones
    if not milestones:
        return ""
    rows_by_state = {r.state: r for r in (snap.journey_rows or [])}
    order = [st for st in _JOURNEY_ORDER if st in milestones]
    if not order:
        return ""
    trs = []
    for i, st in enumerate(order):
        row = rows_by_state.get(st)
        planned = row.planned if row else None
        projected = milestones[st]
        is_last = i == len(order) - 1
        if is_last and domino and domino.dropoff_risk:
            verdict_cell = _pill(domino.dropoff_risk, _dropoff_risk_kind(domino.dropoff_risk))
        else:
            verdict_cell = _delta_note(projected, planned) or "-"
        label = "Dropoff (appt)" if is_last else st.replace("_", " ").title()
        trs.append(
            "<tr>"
            f"<td>{escape(label)}</td>"
            f"<td>{_txt(planned)}</td>"
            f"<td>{_txt(projected)}</td>"
            f"<td>{verdict_cell}</td>"
            "</tr>"
        )
    return (
        "<div class='forecast'><h5>What to expect &mdash; projected from current pace</h5>"
        "<table class='forecast-table'><thead><tr>"
        "<th>Milestone</th><th>Planned</th><th>Projected</th><th>Verdict</th>"
        "</tr></thead><tbody>" + "".join(trs) + "</tbody></table></div>"
    )


def _full_assessment(snap: LoadSnapshot, note: LoadNote | None) -> str:
    if snap.finding_narratives:
        return "\n".join(snap.finding_narratives)
    if note and note.assessment:
        return note.assessment
    if snap.incident_types:
        return ", ".join(t.replace("_", " ") for t in snap.incident_types)
    return ""


def _finding_card(flag_type: str, severity: str | None, narrative: str | None) -> str:
    kind = _finding_sev_kind(severity)
    sev = _pill(severity, kind) if severity else ""
    body = _bullets(narrative) if narrative else "<p>-</p>"
    title = _txt((flag_type or "").replace("_", " ").title())
    return (
        f"<div class='finding-card sev-{kind}'>"
        f"<div class='finding-head'><h4>{title}</h4>{sev}</div>"
        f"<div class='finding-body'>{body}</div>"
        "</div>"
    )


_JOURNEY_ORDER = ["ASSIGNED", "ENROUTE_PICKUP", "AT_PICKUP", "LOADED", "ENROUTE_DROPOFF"]


def _ts(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M") if value else None
    except (TypeError, ValueError):
        return None


def _short_ts(value: str | None) -> str:
    """'2026-09-09 18:00' -> '09-09 18:00'."""
    if not value:
        return ""
    head, _, tail = value.partition(" ")
    return f"{head[5:]} {tail}".strip() if len(head) >= 10 and tail else value

_TRUCK_SVG = (
    "<svg viewBox='0 0 60 30' width='40' height='20' aria-hidden='true'>"
    "<rect x='2' y='8' width='32' height='14' rx='1.5' fill='currentColor'/>"
    "<path d='M34 10 h12 a2 2 0 0 1 2 2 v8 a2 2 0 0 1 -2 2 h-12 z' fill='currentColor' opacity='.55'/>"
    "<circle cx='13' cy='24' r='3.6' fill='#152033'/><circle cx='44' cy='24' r='3.6' fill='#152033'/>"
    "</svg>"
)
_PIN_SVG = (
    "<svg viewBox='0 0 20 22' width='100%' height='100%' aria-hidden='true'>"
    "<line x1='3' y1='2' x2='3' y2='20' stroke='#152033' stroke-width='1.6'/>"
    "<path d='M3 2 L17 5 L3 9 Z' fill='currentColor'/></svg>"
)


# Six evenly-spaced marker slots, regardless of which milestone the load is actually at -
# no time-scaling, no crowding to guard against by construction.
_JOURNEY_POS = {
    "ASSIGNED": 4.0,
    "ENROUTE_PICKUP": 22.8,
    "AT_PICKUP": 41.6,
    "LOADED": 60.4,
    "ENROUTE_DROPOFF": 79.2,
    "AT_DROPOFF": 98.0,
}
_JOURNEY_MARKERS = list(_JOURNEY_POS)

# Each ledger row's own clock describes completing the transition INTO the next state,
# not reaching its own name - confirmed in internal/store.py: _planned_for/_committed_for/
# _actual_for("ENROUTE_DROPOFF", ...) resolve straight to planned_dropoff_at/
# committed_dropoff_at/actual_dropoff_time, i.e. arrival AT dropoff, not departure into
# the dropoff leg. So a marker displays the PRECEDING row's data, not its own row.
# ASSIGNED has no preceding row - it's handled separately (its own row, actual only).
_MARKER_SOURCE_ROW = {
    "ENROUTE_PICKUP": "ASSIGNED",
    "AT_PICKUP": "ENROUTE_PICKUP",
    "LOADED": "AT_PICKUP",
    "ENROUTE_DROPOFF": "LOADED",
    "AT_DROPOFF": "ENROUTE_DROPOFF",
}
# Which incident types actually speak to *timing* at a marker - STALE_GPS/
# STATUS_GPS_CONFLICT are data-quality problems, not lateness, so they don't recolor a clock.
_BREACH_TYPES = {"TRANSITION_OVERDUE", "COMMITMENT_RISK", "DROPOFF_RISK"}


def _journey_html(snap: LoadSnapshot) -> str:
    """Static annotated journey line for one load: 6 fixed, evenly-spaced milestone
    markers, each showing its OWN clock (shifted from the preceding row - see
    _MARKER_SOURCE_ROW), a truck at the current row, and clickable flag pins below the
    line at exactly the marker for the state each incident fired on.

    Color is binary and flag-driven, not a numeric comparison: red if ANY
    TRANSITION_OVERDUE/COMMITMENT_RISK/DROPOFF_RISK ever fired for that marker's state
    (open OR resolved - resolving usually just means the active row moved on, not that
    the delay was fixed), green if nothing ever did. A missing value never gets colored -
    it shows a plain grey label instead of a real number."""
    rows = snap.journey_rows or []
    if not rows:
        return ""

    rows_by_state = {r.state: r for r in rows}

    # Truck and flags sit at the marker literally named after the active row / the state
    # a flag fired on - independent of the label-content shift above (which row's clock a
    # marker DISPLAYS is a separate question from where the truck/flags visually sit).
    assigned_row = rows_by_state.get("ASSIGNED")
    done_markers = {r.state for r in rows if r.completed}
    if "ENROUTE_DROPOFF" in done_markers:
        done_markers.add("AT_DROPOFF")  # arrival mirrors ENROUTE_DROPOFF's own completion

    active_row_state = next((r.state for r in rows if not r.completed), "AT_DROPOFF")
    truck_marker = active_row_state
    truck_x = _JOURNEY_POS.get(truck_marker, 98.0)
    fill_x = max([truck_x] + [_JOURNEY_POS.get(m, 0.0) for m in done_markers])

    # getattr, not snap.domino, purely as a defensive habit - LoadSnapshot.domino is a
    # real field now, but this keeps the diagram from ever crashing on it regardless.
    domino = getattr(snap, "domino", None)

    # every incident this state has EVER had, open or resolved - resolving doesn't mean
    # the delay went away, it usually just means the active row moved on.
    flags_by_state: dict[str, set[str]] = {}
    for fl in snap.journey_flags or []:
        flags_by_state.setdefault(fl.state or "", set()).add((fl.incident_type or "").upper())

    labels = []
    for st in _JOURNEY_MARKERS:
        cls = "jy-label"
        if st == truck_marker:
            cls += " jy-on"
        elif st in done_markers:
            cls += " jy-done"

        if st == "ASSIGNED":
            # a pure fact (when was this load assigned), not a deadline - nothing to
            # compare it against, so it's never colored. Dispatch-deadline lateness
            # (was TRANSITION_OVERDUE fired while active_row was ASSIGNED) belongs to
            # the ENROUTE_PICKUP marker below, since that's where ASSIGNED's own
            # planned/forecast now display under the shift.
            actual = assigned_row.actual if assigned_row else None
            if actual:
                ts_html = f"<span class='jy-name'>{escape(st.replace('_', ' '))}</span><span class='jy-ts jy-ts-actual'>{escape(_short_ts(actual))}</span>"
            else:
                ts_html = f"<span class='jy-name'>{escape(st.replace('_', ' '))}</span><span class='jy-ts jy-ts-dash'>-</span>"
            labels.append(f"<span class='{cls}' style='left:{_JOURNEY_POS[st]}%'>{ts_html}</span>")
            continue

        source_state = _MARKER_SOURCE_ROW[st]
        row = rows_by_state.get(source_state)
        flagged = bool(flags_by_state.get(source_state, set()) & _BREACH_TYPES)

        top = None
        top_is_actual = False
        if row:
            if row.completed:
                top = row.actual  # a completed milestone never shows a forecast, stale or not
                top_is_actual = True
            elif row.forecasted and row.forecasted != row.planned:
                top = row.forecasted  # only a real, diverged forecast counts as a signal

        planned = row.planned if row else None
        color = "mk-red" if flagged else ("mk-green" if (top or planned) else "")

        ts_html = f"<span class='jy-name'>{escape(st.replace('_', ' '))}</span>"
        if top:
            top_cls = "jy-ts-actual" if top_is_actual else "jy-ts-fcst"
            prefix = "" if top_is_actual else "~"
            extra = f" {color}" if color else ""
            ts_html += f"<span class='jy-ts {top_cls}{extra}'>{prefix}{escape(_short_ts(top))}</span>"
        else:
            word = "actual" if (row and row.completed) else "forecast"
            ts_html += f"<span class='jy-ts jy-ts-word'>{word}</span>"
        if planned:
            extra = f" {color}" if color else ""
            ts_html += f"<span class='jy-ts jy-ts-planned{extra}'>{escape(_short_ts(planned))}</span>"
        else:
            ts_html += "<span class='jy-ts jy-ts-word jy-ts-planned'>planned</span>"

        labels.append(
            f"<span class='{cls}' style='left:{_JOURNEY_POS[st]}%'>{ts_html}</span>"
        )

    points = "".join(
        f"<span class='jy-point' style='left:{_JOURNEY_POS[st]}%'></span>" for st in _JOURNEY_MARKERS
    )

    # each flag sits at the marker literally named after the state it fired on; flags
    # landing on the same marker merge into one pin
    placed: dict[float, list] = {}
    for fl in snap.journey_flags or []:
        x = _JOURNEY_POS.get(fl.state or "", 50.0)
        placed.setdefault(round(x, 1), []).append(fl)

    pins = []
    for idx, x in enumerate(sorted(placed)):
        flags = sorted(
            placed[x], key=lambda f: _ts(f.opened_at) or datetime.min
        )
        open_now = any((f.status or "").upper() == "OPEN" for f in flags)
        kind = "jy-flag-open" if open_now else "jy-flag-resolved"
        states = []
        for f in flags:
            s = (f.state or "").replace("_", " ")
            if s and s not in states:
                states.append(s)
        fl_html = []
        for f in flags:
            sev = _pill(f.severity, _finding_sev_kind(f.severity)) if f.severity else ""
            status_txt = "still open" if (f.status or "").upper() == "OPEN" else "resolved"
            fl_html.append(
                "<span class='jy-fl'>"
                f"<span class='jy-fl-h'><strong>{_txt((f.incident_type or '').replace('_', ' ').title())}</strong>{sev}</span>"
                f"<span class='jy-fl-at'>{escape((f.state or '').replace('_', ' '))} &middot; {status_txt}</span>"
                "</span>"
            )
        head = (
            f"{escape(' / '.join(states) or 'flag')} &middot; "
            f"{len(flags)} flag{'s' if len(flags) != 1 else ''}"
        )
        pins.append(
            f"<span class='jy-flag {kind}' style='left:{x}%; "
            f"animation-delay:{0.55 + idx * 0.12:.2f}s' tabindex='0' role='button' "
            f"aria-label='Flags at {escape(states[0] if states else 'journey')}'>"
            f"<span class='jy-pin'>{_PIN_SVG}</span>"
            f"<span class='jy-popup'><span class='jy-popup-h'>{head}</span>{''.join(fl_html)}</span>"
            "</span>"
        )

    all_flags = snap.journey_flags or []
    n_open = sum(1 for f in all_flags if (f.status or "").upper() == "OPEN")
    summary = f"Journey &amp; flags &mdash; {len(all_flags)} incident{'s' if len(all_flags) != 1 else ''}"
    if n_open:
        summary += f", {n_open} open"

    cascade_html = ""
    if domino and domino.dropoff_risk in {"AT_RISK", "BREACH_LIKELY"}:
        kind = "warn" if domino.dropoff_risk == "AT_RISK" else "bad"
        start_x, end_x = truck_x, _JOURNEY_POS["AT_DROPOFF"]
        cascade_html = (
            f"<span class='jy-cascade jy-cascade-{kind}' "
            f"style='left:{start_x}%; width:{max(0.0, end_x - start_x)}%'></span>"
            f"<span class='jy-dropoff-warn jy-dropoff-warn-{kind}' "
            f"style='left:{end_x}%'>&#9888;</span>"
        )

    return (
        "<details class='journey'>"
        f"<summary>{summary}</summary>"
        "<div class='jy-wrap'>"
        "<div class='jy-labels'>" + "".join(labels) + "</div>"
        "<div class='jy-track'>"
        + cascade_html
        + "<span class='jy-line'></span>"
        f"<span class='jy-fill' style='width:{fill_x}%'></span>"
        + points
        + f"<span class='jy-truck' style='left:{truck_x}%'>{_TRUCK_SVG}</span>"
        + "".join(pins)
        + "</div>"
        "<p class='jy-hint'>Each marker shows its own forecast/actual above planned - "
        "red means a flag fired here at some point (resolving doesn't mean it was fixed), "
        "green means it never did. Click a flag below the line for the incident.</p>"
        "</div></details>"
    )


def _evaluation_html(snap: LoadSnapshot, note: LoadNote | None) -> str:
    """The card's detail body: clocks, the delay-cascade block (if any), findings
    collapsed behind 'Show full reasoning', and the (already-collapsed) journey diagram.
    State badge / severity / wake-age live in the card header, not here - see _load_card."""
    blocks: list[str] = []

    blocks.append(_clocks_strip(snap))

    domino = getattr(snap, "domino", None)
    if domino:
        cascade_bits: list[str] = []
        forecast_html = _forecast_table_html(snap, domino)
        if forecast_html:
            cascade_bits.append(forecast_html)
        else:
            # per-milestone projection unavailable - fall back to a single cause -> effect line
            cause_type = next((t for t in snap.incident_types if t != "DROPOFF_RISK"), None)
            cause_label = _txt((cause_type or "This delay").replace("_", " ").title())
            # last-resort backfill in case a Domino ever reaches here with projected_delay_min
            # unset but dropoff_buffer known (see docs/report_v2_implementation_plan.md, Feature 2).
            delay_min = domino.projected_delay_min
            if delay_min is None and domino.dropoff_buffer is not None:
                delay_min = -domino.dropoff_buffer
            delay_txt = _human_minutes(delay_min) if delay_min is not None else "?"
            verdict_pill = _pill(domino.dropoff_risk, _dropoff_risk_kind(domino.dropoff_risk))
            cascade_bits.append(
                f"<p class='cascade-line'>{cause_label} &rarr; {delay_txt} at dropoff {verdict_pill}</p>"
            )
        if domino.preventive_actions:
            items = "".join(f"<li>{escape(a)}</li>" for a in domino.preventive_actions)
            cascade_bits.append(
                f"<div class='cascade-recover'><h5>Recover</h5><ul>{items}</ul></div>"
            )
        if domino.customer_alert:
            cascade_bits.append(
                "<div class='cascade-notify'><h5>If not recoverable &rarr; notify</h5>"
                f"<p>{escape(domino.customer_alert)}</p></div>"
            )
        blocks.append(f"<div class='cascade-block'>{''.join(cascade_bits)}</div>")

    cards: list[str] = []
    if snap.findings:
        for finding in snap.findings:
            cards.append(
                _finding_card(finding.flag_type, finding.severity, finding.narrative)
            )
    else:
        fallback = _full_assessment(snap, note)
        for text in (p.strip() for p in fallback.split("\n") if p.strip()):
            cards.append(
                f"<div class='finding-card'><div class='finding-body'>{_bullets(text)}</div></div>"
            )
    if cards:
        n = len(cards)
        blocks.append(
            "<details class='full-reasoning'>"
            f"<summary>Show full reasoning &middot; {n} finding{'s' if n != 1 else ''}</summary>"
            f"<div class='findings'>{''.join(cards)}</div>"
            "</details>"
        )

    journey = _journey_html(snap)
    if journey:
        blocks.append(journey)

    return "".join(blocks) if blocks else "<p>-</p>"


def _one_sentence(text: str | None) -> str:
    text = " ".join((text or "").split())
    if not text:
        return "-"
    cut = text.find(". ")
    return text[: cut + 1] if cut != -1 else text


def _recommended_action_text(snap: LoadSnapshot) -> str | None:
    return snap.investigation_action or (
        snap.recommended_actions[0] if snap.recommended_actions else None
    )


def _load_card(snap: LoadSnapshot, note: LoadNote | None) -> str:
    """One self-contained attention-queue row. Collapsed by default (a <details> element,
    no extra JS needed) to a single-line header - load, lane, state, severity, recommended
    action - so ~10 loads fit on screen at once; expanding reveals the full evaluation body
    (clocks, cascade/forecast, findings collapsed behind 'Show full reasoning', the journey
    diagram collapsed behind its own summary)."""
    kind = _severity_kind(snap.overall_severity) if snap.overall_severity else "warn"
    sev_bucket = escape((snap.overall_severity or "unranked").lower())
    is_new = 1 if (snap.incident_is_new or not snap.incident_wakes) else 0
    state = escape(snap.active_state or "")
    shipper = escape(snap.shipper_name or "")

    badges = f"<span class='state-badge'>{_txt(snap.active_state)}</span>"
    if snap.overall_severity:
        badges += _pill(snap.overall_severity, kind)
    badges += _wake_badge(snap)

    action_text = _recommended_action_text(snap)
    action_html = (
        f"<span class='card-action'><span class='ic'>{_action_icon(action_text)}</span>"
        f"{_txt(_one_sentence(action_text))}</span>"
        if action_text else "<span class='card-action muted'>No action needed</span>"
    )

    return (
        f"<details class='load-card sev-{kind}' id='{_eval_id(snap.load_id)}' "
        f"data-severity='{sev_bucket}' data-new='{is_new}' "
        f"data-state='{state}' data-shipper='{shipper}'>"
        "<summary class='card-head'>"
        "<div class='card-title'>"
        f"<strong>{_txt(snap.load_id)}</strong>"
        f"<span class='lane'>{_lane(snap)} &middot; <span class='id'>{_txt(snap.load_num)}</span></span>"
        "</div>"
        f"{action_html}"
        f"<div class='card-badges'>{badges}</div>"
        "</summary>"
        "<div class='card-body'>"
        f"<p class='headline'>{_txt(_headline(snap))}</p>"
        f"{_action_chip(snap)}"
        f"{_evaluation_html(snap, note)}"
        "</div>"
        "</details>"
    )


def _attention_section(rows: list[LoadSnapshot], notes: dict[str, LoadNote]) -> list[str]:
    page = ["<section>", "<h2>Attention queue</h2>"]
    if not rows:
        page.append("<p>Nothing in the attention queue.</p>")
        page.append("</section>")
        return page

    # dynamic filter counts, computed from this batch - never hand-picked, so the
    # chips can't drift out of sync with what's actually on the page.
    sev_counts: dict[str, int] = {}
    new_count = 0
    for snap in rows:
        bucket = (snap.overall_severity or "unranked").lower()
        sev_counts[bucket] = sev_counts.get(bucket, 0) + 1
        if snap.incident_is_new or not snap.incident_wakes:
            new_count += 1
    carried_count = len(rows) - new_count

    sev_order = ["critical", "elevated", "watch", "unranked"]
    sev_labels = {"critical": "Critical", "elevated": "Elevated", "watch": "Watch", "unranked": "Unranked"}
    chips = [f"<button class='chip active' data-filter='all' type='button'>All <span class='n'>{len(rows)}</span></button>"]
    for bucket in sev_order:
        n = sev_counts.get(bucket, 0)
        if n:
            chips.append(
                f"<button class='chip' data-filter='sev:{bucket}' type='button'>"
                f"{sev_labels[bucket]} <span class='n'>{n}</span></button>"
            )
    if new_count:
        chips.append(
            f"<button class='chip' data-filter='new' type='button'>New this wake <span class='n'>{new_count}</span></button>"
        )
    if carried_count:
        chips.append(
            f"<button class='chip' data-filter='carried' type='button'>Carried over <span class='n'>{carried_count}</span></button>"
        )
    page.append(f"<div class='filters'>{''.join(chips)}</div>")

    # state / shipper dropdowns - only worth showing when there's more than one
    # distinct value in this batch to actually split on.
    states = sorted({snap.active_state for snap in rows if snap.active_state})
    shippers = sorted({snap.shipper_name for snap in rows if snap.shipper_name})
    selects = []
    if len(states) > 1:
        opts = "".join(f"<option value='{escape(s)}'>{escape(s)}</option>" for s in states)
        selects.append(
            "<label class='filter-select-label'>State "
            f"<select class='filter-select' id='stateFilter'><option value='all'>All</option>{opts}</select>"
            "</label>"
        )
    if len(shippers) > 1:
        opts = "".join(f"<option value='{escape(s)}'>{escape(s)}</option>" for s in shippers)
        selects.append(
            "<label class='filter-select-label'>Shipper "
            f"<select class='filter-select' id='shipperFilter'><option value='all'>All</option>{opts}</select>"
            "</label>"
        )
    if selects:
        page.append(f"<div class='filters filters-select'>{''.join(selects)}</div>")

    page.append("<div class='card-grid'>")
    for snap in rows:
        note = notes.get(snap.load_id) or notes.get(snap.load_num or "")
        page.append(_load_card(snap, note))
    page.append("</div>")
    page.append("</section>")
    return page




def _simple_table_section(
    title: str,
    rows: list[LoadSnapshot],
    empty: str,
    *,
    use_load_id: bool = False,
) -> list[str]:
    page = ["<section class='quiet'>", f"<h2>{escape(title)}</h2>"]
    if not rows:
        page.append(f"<p>{escape(empty)}</p>")
        page.append("</section>")
        return page
    page.append("<table>")
    page.append(
        "<thead><tr><th>Load</th><th>Lane</th><th>Vehicle</th><th>State</th></tr></thead><tbody>"
    )
    for snap in rows:
        load_cell = snap.load_id if use_load_id else snap.label()
        page.append(
            "<tr>"
            f"<td>{_txt(load_cell)}</td>"
            f"<td>{_lane(snap)}</td>"
            f"<td>{_txt(snap.vh_num)}</td>"
            f"<td>{_txt(snap.active_state or snap.watch_state)}</td>"
            "</tr>"
        )
    page.append("</tbody></table></section>")
    return page


_CSS = """
:root {
  --ink: #152033;
  --muted: #5c6573;
  --line: #d9dee8;
  --paper: #f5f6f8;
  --card: #fff;
  --navy: #10233f;
  --bad: #9b1c1c;
  --warn: #9a6700;
  --ok: #166534;
  --new: #1d5fa8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 15px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif;
  color: var(--ink);
  background: var(--paper);
}
header, main, footer { max-width: 1080px; margin: 0 auto; padding: 24px 28px; }
header {
  background: var(--navy);
  color: #fff;
  max-width: none;
  padding: 32px 28px 28px;
}
header .kicker { margin: 0; letter-spacing: .08em; text-transform: uppercase; font-size: 11px; opacity: .75; }
header h1 { margin: 6px 0 8px; font-size: 28px; font-weight: 650; }
header .headline { margin: 0 0 8px; font-size: 18px; opacity: .95; }
header .meta { margin: 0; opacity: .7; font-size: 13px; }
.kpis { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 20px; }
.kpi {
  background: rgba(255,255,255,.1);
  border: 1px solid rgba(255,255,255,.14);
  border-radius: 10px;
  padding: 10px 14px;
  min-width: 120px;
}
.kpi strong { display: block; font-size: 22px; }
.kpi span { font-size: 12px; opacity: .8; }
.kpi.attention { background: #c45c26; border-color: transparent; }
.kpi.new-count { background: var(--new); border-color: transparent; }
h2 { font-size: 18px; margin: 0 0 10px; }
section { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 18px 20px; margin: 16px 0; }
section.quiet { background: transparent; }
section p { margin: 0 0 8px; }
.id, .lede { color: var(--muted); font-size: 13px; }
.id { font-family: ui-monospace, SFMono-Regular, monospace; }

/* ---- filter chip bar ---- */
.filters { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.chip {
  font: inherit; font-size: 12.5px; padding: 5px 12px; border-radius: 999px;
  border: 1px solid var(--line); background: #fff; color: var(--muted); cursor: pointer;
}
.chip:hover { border-color: var(--navy); color: var(--ink); }
.chip.active { background: var(--navy); border-color: var(--navy); color: #fff; }
.chip .n { opacity: .7; margin-left: 3px; }

.filters-select { align-items: center; }
.filter-select-label { font-size: 12.5px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.filter-select {
  font: inherit; font-size: 12.5px; padding: 4px 8px; border-radius: 6px;
  border: 1px solid var(--line); background: #fff; color: var(--ink); cursor: pointer;
}

/* ---- attention-queue card grid - one row per load, collapsed to a header by
   default (<details>/<summary>, no extra JS) so ~10 loads fit on screen at once ---- */
.card-grid { display: grid; grid-template-columns: 1fr; gap: 10px; }
.load-card {
  border: 1px solid var(--line); border-left: 4px solid var(--line); border-radius: 10px;
  background: var(--card); overflow: hidden;
}
.load-card.sev-bad { border-left-color: var(--bad); }
.load-card.sev-warn { border-left-color: var(--warn); }
.load-card.sev-ok { border-left-color: var(--ok); }
.load-card.sev-muted { border-left-color: var(--line); }
.load-card > summary { list-style: none; cursor: pointer; }
.load-card > summary::-webkit-details-marker { display: none; }
.load-card > summary::after {
  content: "▸"; color: var(--muted); font-size: 12px; margin-left: auto; align-self: center;
  transition: transform .12s ease;
}
.load-card[open] > summary::after { transform: rotate(90deg); }
.load-card > summary:hover { background: var(--paper); }
.load-card > .card-body { padding: 0 18px 18px; }
.card-head {
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  padding: 12px 18px; margin-bottom: 0;
}
.card-action {
  display: flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600;
  color: #7a4a14; flex: 1 1 220px; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.card-action.muted { color: var(--muted); font-weight: 400; }
.card-action .ic { font-size: 14px; flex-shrink: 0; }
.card-title { display: flex; flex-direction: column; gap: 1px; }
.card-title strong { font-size: 14.5px; }
.card-title .lane { font-size: 12px; color: var(--muted); }
.card-badges { display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; align-content: flex-start; }
.headline { font-size: 14px; font-weight: 600; margin: 0 0 10px; }
.action-chip {
  display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600;
  background: #fbf3ea; border: 1px solid #eddcc4; color: #7a4a14; border-radius: 8px;
  padding: 8px 10px; margin-bottom: 12px;
}
.action-chip .ic { font-size: 15px; }

/* ---- findings, collapsed behind a toggle so a card isn't a wall of text ---- */
details.full-reasoning { margin-top: 12px; }
details.full-reasoning > summary {
  cursor: pointer; font-size: 12px; font-weight: 600; color: var(--navy); list-style: none;
}
details.full-reasoning > summary::-webkit-details-marker { display: none; }
details.full-reasoning > summary::before { content: '\\25B8'; margin-right: 6px; color: var(--muted); }
details.full-reasoning[open] > summary::before { content: '\\25BE'; }
details.full-reasoning > .findings { margin-top: 10px; }

/* ---- 'what to expect' forecast table ---- */
.forecast h5 {
  margin: 0 0 6px; font-size: 10.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
}
table.forecast-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
table.forecast-table th {
  text-align: left; font-size: 10px; text-transform: uppercase; letter-spacing: .03em;
  color: var(--muted); padding: 4px 6px 4px 0; border-bottom: 1px solid var(--line);
}
table.forecast-table td { padding: 5px 6px 5px 0; border-bottom: 1px solid var(--line); vertical-align: middle; }
table.forecast-table tr:last-child td { border-bottom: none; }

.eval-btn {
  display: inline-block;
  padding: 4px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  font-size: 12px;
  color: var(--navy);
  white-space: nowrap;
  background: #fff;
}
.eval-btn:hover { border-color: var(--navy); }
.state-badge {
  display: inline-block;
  margin: 0 6px 4px 0;
  padding: 2px 8px;
  border-radius: 6px;
  background: #eef1f6;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .03em;
}
.eval-head { margin-bottom: 12px; }
.clocks {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin: 0 0 16px;
  padding: 12px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.clock { display: flex; flex-direction: column; gap: 2px; }
.clock-k { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.clock-v { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 13px; }
.delta {
  align-self: flex-start;
  margin-top: 2px;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 11px;
  background: #eef1f6;
  color: var(--muted);
}
.delta.bad { background: #fde8e8; color: var(--bad); }
.delta.ok { background: #e3f6e8; color: var(--ok); }
.findings { display: flex; flex-direction: column; gap: 10px; }
.finding-card {
  border: 1px solid var(--line);
  border-left: 3px solid var(--line);
  border-radius: 8px;
  padding: 10px 12px;
}
.finding-card.sev-bad { border-left-color: var(--bad); }
.finding-card.sev-warn { border-left-color: var(--warn); }
.finding-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 4px; }
.finding-head h4 { margin: 0; font-size: 13px; font-weight: 650; }
.finding-body p { margin: 0 0 6px; line-height: 1.5; font-size: 13px; max-width: 52em; }
.finding-body p:last-child { margin-bottom: 0; }
.finding-bullets { margin: 0; padding-left: 18px; max-width: 52em; }
.finding-bullets li { margin: 0 0 5px; line-height: 1.5; font-size: 13px; }
.finding-bullets li:last-child { margin-bottom: 0; }
table.evals { table-layout: fixed; }
table.evals th:nth-child(1), table.evals td.eval-load { width: 18%; }
table.evals td { padding: 16px 12px; }
table.evals td.eval-load { vertical-align: top; }
table.evals td.eval-copy { vertical-align: top; font-size: 15px; }
.eval-sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
#evaluations tr { scroll-margin-top: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; vertical-align: top; padding: 8px 8px; border-bottom: 1px solid var(--line); }
th { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.pill {
  display: inline-block;
  margin: 0 4px 4px 0;
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 11px;
  background: #eef1f6;
}
.pill.bad { background: #fde8e8; color: var(--bad); }
.pill.warn { background: #fff4d6; color: var(--warn); }
.pill.ok { background: #e3f6e8; color: var(--ok); }
.pill.muted { background: #eef1f6; color: var(--muted); }
.pill.new { background: #e3edfb; color: var(--new); }
footer { color: var(--muted); font-size: 12px; }

/* ---- per-load journey diagram ---- */
details.journey { margin-top: 16px; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); }
details.journey > summary {
  cursor: pointer; padding: 8px 12px; font-size: 12px; font-weight: 600;
  color: var(--navy); list-style: none;
}
details.journey > summary::-webkit-details-marker { display: none; }
details.journey > summary::before { content: '\\25B8'; margin-right: 6px; color: var(--muted); }
details.journey[open] > summary::before { content: '\\25BE'; }
.jy-wrap { padding: 92px 18px 14px; }
.jy-labels { position: relative; height: 40px; }
.jy-label {
  position: absolute; transform: translateX(-50%); bottom: 0;
  display: flex; flex-direction: column; align-items: center; gap: 1px;
  white-space: nowrap; padding-bottom: 3px; border-bottom: 2px solid transparent;
}
.jy-name {
  font-size: 9px; letter-spacing: .03em; text-transform: uppercase; color: var(--muted);
}
.jy-ts { font-size: 9px; font-variant-numeric: tabular-nums; color: var(--muted); }
.jy-ts-actual { color: var(--ink); font-weight: 600; }
.jy-ts-fcst { color: var(--muted); font-style: italic; }
.jy-ts-planned { color: var(--muted); }
.jy-ts-dash { opacity: .5; }
.jy-ts-word { color: var(--muted); font-style: italic; opacity: .7; }
.mk-green { color: var(--ok); font-weight: 600; }
.mk-red { color: var(--bad); font-weight: 600; }
.jy-label.jy-done .jy-name { color: var(--ink); }
.jy-label.jy-on { border-bottom-color: var(--navy); }
.jy-label.jy-on .jy-name { color: var(--navy); font-weight: 700; }
.jy-track { position: relative; height: 52px; }
.jy-line { position: absolute; top: 26px; left: 0; right: 0; height: 2px; background: var(--line); }
.jy-fill { position: absolute; top: 26px; left: 0; height: 2px; background: var(--navy); animation: jyFill .9s ease-out both; }
@keyframes jyFill { from { width: 0; } }
.jy-point {
  position: absolute; top: 20px; width: 12px; height: 12px; border-radius: 50%;
  background: var(--card); border: 2px solid var(--ink); transform: translateX(-50%); z-index: 2;
}
.jy-truck { position: absolute; top: 6px; transform: translateX(-50%); color: var(--navy); z-index: 3; animation: jyTruck .9s ease-out both; }
@keyframes jyTruck { from { left: 0; opacity: 0; } }
/* flags sit below the line, exactly at the marker for the state they fired on -
   above the line is already spoken for by the forecast/planned label stack */
.jy-flag {
  position: absolute; top: 32px; width: 15px; height: 20px; transform: translateX(-50%);
  cursor: pointer; z-index: 5; color: var(--bad); animation: jyPin .3s ease-out both;
}
.jy-flag.jy-flag-resolved { color: var(--muted); }
/* thin stem from the route line down to the pin */
.jy-flag::after {
  content: ''; position: absolute; left: 50%; top: -6px; width: 1px; height: 6px;
  background: currentColor; opacity: .45;
}
@keyframes jyPin { from { transform: translateX(-50%) translateY(6px); opacity: 0; } }
.jy-pin { display: block; width: 100%; height: 100%; }
.jy-popup {
  display: none; position: absolute; bottom: calc(100% + 8px); left: 50%;
  transform: translateX(-50%); width: 300px; max-width: min(360px, 80vw);
  max-height: 240px; overflow: hidden auto; overflow-wrap: anywhere;
  padding: 10px 12px; background: var(--navy); color: #fff; border-radius: 8px;
  font-size: 12px; line-height: 1.45; text-align: left; z-index: 20;
  box-shadow: 0 8px 24px rgba(0,0,0,.18);
}
.jy-flag.jy-open .jy-popup { display: block; }
.jy-popup::after {
  content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
  border: 6px solid transparent; border-top-color: var(--navy);
}
.jy-popup-h { display: block; font-weight: 700; margin-bottom: 6px; font-size: 10px; letter-spacing: .04em; text-transform: uppercase; opacity: .85; }
.jy-fl { display: block; padding: 6px 0; border-top: 1px solid rgba(255,255,255,.16); }
.jy-fl:first-of-type { border-top: 0; padding-top: 0; }
.jy-fl-h { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 6px; margin-bottom: 3px; }
.jy-fl-h > strong { min-width: 0; }
.jy-fl-at {
  display: block; font-size: 9px; letter-spacing: .04em; text-transform: uppercase;
  opacity: .7;
}
.jy-popup .pill { background: rgba(255,255,255,.16); color: #fff; }
.jy-hint { margin: 12px 0 0; font-size: 10.5px; color: var(--muted); font-style: italic; }
.jy-cascade {
  position: absolute; top: 25px; height: 4px; z-index: 1; border-radius: 2px;
}
.jy-cascade-warn { background: var(--warn); }
.jy-cascade-bad { background: var(--bad); }
.jy-dropoff-warn {
  position: absolute; top: 12px; transform: translateX(-50%);
  font-size: 14px; line-height: 1; z-index: 4;
}
.jy-dropoff-warn-warn { color: var(--warn); }
.jy-dropoff-warn-bad { color: var(--bad); }

/* ---- delay cascade block (evaluation cell) ---- */
.cascade-block { margin: 14px 0 0; padding-top: 12px; border-top: 1px solid var(--line); }
.cascade-line { margin: 0 0 8px; font-weight: 600; font-size: 13px; }
.cascade-recover, .cascade-notify { margin-top: 8px; font-size: 13px; }
.cascade-recover h5, .cascade-notify h5 {
  margin: 0 0 4px; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: .04em; color: var(--muted);
}
.cascade-recover ul { margin: 0; padding-left: 18px; }
.cascade-recover li { margin: 0 0 3px; }

@media (prefers-reduced-motion: reduce) {
  .jy-fill, .jy-truck, .jy-flag { animation: none !important; }
}
@media print {
  header { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  section { break-inside: avoid; }
  details.journey { break-inside: avoid; }
  details.journey > .jy-wrap { display: block !important; }
}
"""


_JOURNEY_SCRIPT = """<script>
(function () {
  var flags = Array.prototype.slice.call(document.querySelectorAll('.jy-flag'));
  function closeAll(except) {
    flags.forEach(function (f) { if (f !== except) f.classList.remove('jy-open'); });
  }
  flags.forEach(function (f) {
    f.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = f.classList.contains('jy-open');
      closeAll(f);
      f.classList.toggle('jy-open', !open);
    });
    f.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); f.click(); }
      else if (e.key === 'Escape') { f.classList.remove('jy-open'); }
    });
  });
  document.addEventListener('click', function () { closeAll(null); });
})();
</script>"""


_FILTER_SCRIPT = """<script>
(function () {
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
  var cards = Array.prototype.slice.call(document.querySelectorAll('.load-card'));
  if (!cards.length) return;
  var stateSel = document.getElementById('stateFilter');
  var shipperSel = document.getElementById('shipperFilter');
  var activeChip = 'all';

  function apply() {
    var st = stateSel ? stateSel.value : 'all';
    var sh = shipperSel ? shipperSel.value : 'all';
    cards.forEach(function (card) {
      var show = (activeChip === 'all'
          || (activeChip.indexOf('sev:') === 0 && card.dataset.severity === activeChip.slice(4))
          || (activeChip === 'new' && card.dataset.new === '1')
          || (activeChip === 'carried' && card.dataset.new === '0'))
        && (st === 'all' || card.dataset.state === st)
        && (sh === 'all' || card.dataset.shipper === sh);
      card.hidden = !show;
    });
  }

  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      chips.forEach(function (c) { c.classList.remove('active'); });
      chip.classList.add('active');
      activeChip = chip.dataset.filter;
      apply();
    });
  });
  if (stateSel) stateSel.addEventListener('change', apply);
  if (shipperSel) shipperSel.addEventListener('change', apply);
})();
</script>"""
