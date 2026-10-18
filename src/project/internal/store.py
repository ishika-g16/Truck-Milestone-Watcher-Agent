"""Internal SQLite for watches, incidents, and state table"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime

from project.data.types import (
    JOURNEY_STATES,
    TERMINAL_STATE,
    WATCH_STATES,
    Incident,
    LoadData,
    LoadEpisode,
    LoadMemory,
    PatternSummary,
    StateRow,
    TimeData,
    Watch,
)
from project.data.mappers import _dt, row_to_incident, row_to_state_row, row_to_watch
from project.constants import DISPATCH_THRESH, PATTERN_MIN_SAMPLES, PICKUP_DWELL, now_ist_naive


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _dt_to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def correlation_key(*, load_id: str, state: str | None, incident_type: str) -> str:
    """Key for each incident: load_id | state | incident_type"""
    return " | ".join(
        [
            load_id,
            state or "",
            incident_type,
        ]
    )


def _add(base: datetime | None, delta) -> datetime | None:
    if base is None:
        return None
    return base + delta


def _planned_for(state: str, times: TimeData | None) -> datetime | None:
    if times is None:
        return None
    if state == "ASSIGNED":
        return _add(times.assigned_at, DISPATCH_THRESH)
    if state == "ENROUTE_PICKUP":
        return times.planned_pickup_at
    if state == "AT_PICKUP":
        return _add(times.planned_pickup_at, PICKUP_DWELL)
    if state == "LOADED":
        # same dwell-end as AT_PICKUP, plus a dispatch grace period before we expect the
        # truck moving again - mirrors ASSIGNED's own DISPATCH_THRESH for the first leg.
        return _add(times.planned_pickup_at, PICKUP_DWELL + DISPATCH_THRESH)
    return times.planned_dropoff_at


def _committed_for(state: str, times: TimeData | None) -> datetime | None:
    if times is None:
        return None
    if state == "ASSIGNED":
        return None
    if state == "ENROUTE_PICKUP":
        return times.committed_pickup_at
    if state == "AT_PICKUP":
        return _add(times.committed_pickup_at, PICKUP_DWELL)
    if state == "LOADED":
        return _add(times.committed_pickup_at, PICKUP_DWELL + DISPATCH_THRESH)
    return times.committed_dropoff_at


def _actual_for(state: str, times: TimeData | None) -> datetime | None:
    if times is None:
        return None
    if state == "ASSIGNED":
        return times.assigned_at  # the one genuinely real, always-known "actual" for this row
    if state == "ENROUTE_PICKUP":
        return times.actual_pickup_time
    if state == "LOADED":
        return times.actual_loaded_at
    if state == "ENROUTE_DROPOFF":
        return times.actual_dropoff_time
    return None


def row_completed(seq: int, canonical_status: str | None) -> bool:
    """Catch-up: rows before canonical are done. UNMAPPED does not advance.
    AT_DROPOFF completes all five rows."""
    status = (canonical_status or "").upper()
    if not status or status == "UNMAPPED" or status not in JOURNEY_STATES:
        return False
    if status == TERMINAL_STATE:
        return True
    return seq < JOURNEY_STATES.index(status)


def build_state_rows(load: LoadData, times: TimeData | None) -> list[StateRow]:
    """Five rows for wake 0. Last is ENROUTE_DROPOFF → AT_DROPOFF.
    completed comes from canonical_status, not flags."""
    rows: list[StateRow] = []
    for seq, state in enumerate(WATCH_STATES):
        rows.append(
            StateRow(
                load_id=load.load_id,
                seq=seq,
                current_state=state,
                next_state=JOURNEY_STATES[seq + 1],
                planned=_planned_for(state, times),
                committed=_committed_for(state, times),
                forecasted=None,
                actual=_actual_for(state, times),
                completed=row_completed(seq, load.canonical_status),
            )
        )
    return rows


class InternalStore:
    """Repository over the internal SQLite connection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ----- watches -----

    def get_active_watch(self, load_id: str) -> Watch | None:
        """Return the ACTIVE watch for this load, or None."""
        cur = self.conn.execute(
            """
            SELECT * FROM watches
            WHERE load_id = ? AND watch_state = 'ACTIVE'
            """,
            (load_id,),
        )
        row = cur.fetchone()
        return row_to_watch(row) if row else None

    def get_watch(self, watch_id: str) -> Watch | None:
        cur = self.conn.execute(
            "SELECT * FROM watches WHERE watch_id = ?",
            (watch_id,),
        )
        row = cur.fetchone()
        return row_to_watch(row) if row else None

    def insert_watch(self, watch: Watch) -> Watch:
        """
        Insert a watch. ACTIVE inserts require no other ACTIVE watch
        for the same load_id (enforced by unique index + pre-check).
        """
        state = watch.watch_state or "ACTIVE"
        if state == "ACTIVE" and watch.load_id:
            existing = self.get_active_watch(watch.load_id)
            if existing is not None:
                raise ValueError(
                    f"load_id={watch.load_id!r} already has ACTIVE watch "
                    f"{existing.watch_id!r}; complete it first"
                )

        watch_id = watch.watch_id or _new_id("w")
        created = watch.created_at or now_ist_naive()
        self.conn.execute(
            """
            INSERT INTO watches (
                watch_id, load_id, load_num, vh_num,
                watch_state, progress_state, commitment_state, evidence_state,
                created_at, last_evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                watch_id,
                watch.load_id,
                watch.load_num,
                watch.vh_num,
                state,
                watch.progress_state,
                watch.commitment_state,
                watch.evidence_state,
                _dt_to_db(created),
                _dt_to_db(watch.last_evaluated_at),
            ),
        )
        self.conn.commit()
        out = self.get_watch(watch_id)
        assert out is not None
        return out

    def create_active_watch(self, load: LoadData, *, at: datetime | None = None) -> Watch:
        """Build and insert a thin Watch from ops LoadData. Call when none exists."""
        if not load.load_id:
            raise ValueError("create_active_watch requires load.load_id")
        return self.insert_watch(
            Watch(
                watch_id=None,
                load_id=load.load_id,
                load_num=load.load_num,
                vh_num=load.vh_num,
                watch_state="ACTIVE",
                progress_state="HEALTHY",
                commitment_state="NOT_APPLICABLE",
                evidence_state="RELIABLE",
                created_at=at,
                last_evaluated_at=None,
            )
        )

    def ensure_active_watch(self, load: LoadData, *, at: datetime | None = None) -> Watch:
        """Return the ACTIVE watch for this load, creating it from LoadData if missing."""
        if not load.load_id:
            raise ValueError("ensure_active_watch requires load.load_id")
        existing = self.get_active_watch(load.load_id)
        if existing is not None:
            return existing
        return self.create_active_watch(load, at=at)

    def update_watch(self, watch: Watch) -> Watch:
        if not watch.watch_id:
            raise ValueError("update_watch requires watch_id")
        self.conn.execute(
            """
            UPDATE watches SET
                load_num = ?,
                vh_num = ?,
                watch_state = ?,
                progress_state = ?,
                commitment_state = ?,
                evidence_state = ?,
                last_evaluated_at = ?
            WHERE watch_id = ?
            """,
            (
                watch.load_num,
                watch.vh_num,
                watch.watch_state,
                watch.progress_state,
                watch.commitment_state,
                watch.evidence_state,
                _dt_to_db(watch.last_evaluated_at),
                watch.watch_id,
            ),
        )
        self.conn.commit()
        out = self.get_watch(watch.watch_id)
        assert out is not None
        return out

    def complete_watch(
        self,
        watch_id: str,
        *,
        progress_state: str = "COMPLETED",
        at: datetime | None = None,
    ) -> None:
        when = at or now_ist_naive()
        self.conn.execute(
            """
            UPDATE watches
            SET watch_state = 'COMPLETED',
                progress_state = ?,
                last_evaluated_at = ?
            WHERE watch_id = ?
            """,
            (progress_state, _dt_to_db(when), watch_id),
        )
        self.conn.commit()

    # ----- state rows (5 per load_id) -----

    def get_state_rows(self, load_id: str) -> list[StateRow]:
        cur = self.conn.execute(
            """
            SELECT * FROM state_rows
            WHERE load_id = ?
            ORDER BY seq ASC
            """,
            (load_id,),
        )
        return [row_to_state_row(r) for r in cur.fetchall()]

    def ensure_state_rows(self, load: LoadData, times: TimeData | None = None) -> list[StateRow]:
        """Return the 5-row ledger, inserting a status-only skeleton if missing.
        Clocks (planned/committed/actual) stay None until times are hydrated."""
        if not load.load_id:
            raise ValueError("ensure_state_rows requires load.load_id")
        existing = self.get_state_rows(load.load_id)
        if existing:
            return existing
        return self.upsert_state_rows(build_state_rows(load, times))

    def refresh_actuals(self, rows: list[StateRow], times: TimeData | None) -> list[StateRow]:
        """Hydrate planned / committed / actual from ops TimeData after catch-up.
        Never clears a clock. Does not complete rows - catch-up still owns completed."""
        changed = False
        for row in rows:
            planned = _planned_for(row.current_state, times)
            if planned is not None and row.planned != planned:
                row.planned = planned
                changed = True
            committed = _committed_for(row.current_state, times)
            if committed is not None and row.committed != committed:
                row.committed = committed
                changed = True
            actual = _actual_for(row.current_state, times)
            if actual is not None and row.actual != actual:
                row.actual = actual
                changed = True
            if row.forecasted is None and row.planned is not None:
                row.forecasted = row.planned
                changed = True
        if changed:
            return self.upsert_state_rows(rows)
        return rows

    def catch_up_state_rows(self, load: LoadData, rows: list[StateRow]) -> list[StateRow]:
        """Mark rows complete when canonical_status has moved past them."""
        changed = False
        for row in rows:
            if not row.completed and row_completed(row.seq, load.canonical_status):
                row.completed = True
                changed = True
        if changed:
            return self.upsert_state_rows(rows)
        return rows

    def upsert_state_rows(self, rows: list[StateRow]) -> list[StateRow]:
        """Insert or replace the ledger rows (typically all 5 for one load)."""
        if not rows:
            raise ValueError("upsert_state_rows requires at least one StateRow")
        load_id = rows[0].load_id
        if not load_id:
            raise ValueError("upsert_state_rows requires load_id on each row")
        if any(r.load_id != load_id for r in rows):
            raise ValueError("upsert_state_rows: all rows must share load_id")

        self.conn.executemany(
            """
            INSERT INTO state_rows (
                load_id, seq, current_state, next_state,
                planned, committed, forecasted, actual, completed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(load_id, seq) DO UPDATE SET
                current_state = excluded.current_state,
                next_state = excluded.next_state,
                planned = excluded.planned,
                committed = excluded.committed,
                forecasted = excluded.forecasted,
                actual = excluded.actual,
                completed = excluded.completed
            """,
            [
                (
                    r.load_id,
                    r.seq,
                    r.current_state,
                    r.next_state,
                    _dt_to_db(r.planned),
                    _dt_to_db(r.committed),
                    _dt_to_db(r.forecasted if r.forecasted is not None else r.planned),
                    _dt_to_db(r.actual),
                    1 if r.completed else 0,
                )
                for r in rows
            ],
        )
        self.conn.commit()
        return self.get_state_rows(load_id)

    def set_forecasted(self, load_id: str, seq: int, forecasted: datetime | None) -> None:
        if forecasted is None:
            cur = self.conn.execute(
                "SELECT planned FROM state_rows WHERE load_id = ? AND seq = ?",
                (load_id, seq),
            )
            row = cur.fetchone()
            forecasted = _dt(row["planned"]) if row else None
        self.conn.execute(
            """
            UPDATE state_rows
            SET forecasted = ?
            WHERE load_id = ? AND seq = ?
            """,
            (_dt_to_db(forecasted), load_id, seq),
        )
        self.conn.commit()

    # ----- incidents -----

    def get_open_incidents(self, load_id: str) -> list[Incident]:
        cur = self.conn.execute(
            """
            SELECT * FROM incidents
            WHERE load_id = ? AND status = 'OPEN'
            ORDER BY opened_at ASC
            """,
            (load_id,),
        )
        return [row_to_incident(r) for r in cur.fetchall()]

    def get_incident_by_correlation(self, key: str) -> Incident | None:
        cur = self.conn.execute(
            "SELECT * FROM incidents WHERE correlation_key = ?",
            (key,),
        )
        row = cur.fetchone()
        return row_to_incident(row) if row else None

    def upsert_incident(self, incident: Incident) -> Incident:
        """
        Insert if correlation_key is new; else update the existing row
        (re-open if it was RESOLVED). Never deletes.
        """
        if not incident.correlation_key:
            raise ValueError("upsert_incident requires correlation_key")

        now = now_ist_naive()
        existing = self.get_incident_by_correlation(incident.correlation_key)

        if existing is None:
            incident_id = incident.incident_id or _new_id("in")
            opened = incident.opened_at or now
            self.conn.execute(
                """
                INSERT INTO incidents (
                    incident_id, correlation_key, watch_id, load_id,
                    incident_type, status,
                    opened_at, resolved_at, last_updated_at,
                    recommended_action,
                    severity, narrative, suppressed, times_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    incident.correlation_key,
                    incident.watch_id,
                    incident.load_id,
                    incident.incident_type,
                    incident.status or "OPEN",
                    _dt_to_db(opened),
                    _dt_to_db(incident.resolved_at),
                    _dt_to_db(incident.last_updated_at or now),
                    incident.recommended_action,
                    incident.severity,
                    incident.narrative,
                    1 if incident.suppressed else 0,
                    1,  # a brand-new correlation key is always a first sighting
                ),
            )
            self.conn.commit()
            out = self.get_incident_by_correlation(incident.correlation_key)
            assert out is not None
            return out

        # update existing (may re-open)
        new_status = incident.status or "OPEN"
        resolved_at = incident.resolved_at
        if new_status == "OPEN":
            resolved_at = None
            if existing.status == "RESOLVED":
                # re-opening after being resolved reads as new again, not a continuation
                next_times_seen = 1
            else:
                next_times_seen = (existing.times_seen or 1) + 1
        else:
            next_times_seen = existing.times_seen or 1  # resolving doesn't change the count
        self.conn.execute(
            """
            UPDATE incidents SET
                watch_id = COALESCE(?, watch_id),
                incident_type = COALESCE(?, incident_type),
                status = ?,
                resolved_at = ?,
                last_updated_at = ?,
                recommended_action = COALESCE(?, recommended_action),
                severity = ?,
                narrative = ?,
                suppressed = ?,
                times_seen = ?
            WHERE correlation_key = ?
            """,
            (
                incident.watch_id,
                incident.incident_type,
                new_status,
                _dt_to_db(resolved_at),
                _dt_to_db(incident.last_updated_at or now),
                incident.recommended_action,
                incident.severity,
                incident.narrative,
                1 if incident.suppressed else 0,
                next_times_seen,
                incident.correlation_key,
            ),
        )
        self.conn.commit()
        out = self.get_incident_by_correlation(incident.correlation_key)
        assert out is not None
        return out

    def resolve_incident(
        self,
        correlation_key: str,
        *,
        at: datetime | None = None,
    ) -> None:
        """Mark OPEN incident RESOLVED (never delete)."""
        when = at or now_ist_naive()
        self.conn.execute(
            """
            UPDATE incidents
            SET status = 'RESOLVED',
                resolved_at = ?,
                last_updated_at = ?
            WHERE correlation_key = ? AND status = 'OPEN'
            """,
            (_dt_to_db(when), _dt_to_db(when), correlation_key),
        )
        self.conn.commit()

    def resolve_open_incidents_for_watch(self, watch_id: str, *, at: datetime | None = None,) -> None:
        """Resolve leftover OPEN incidents when a Watch completes."""
        when = at or now_ist_naive()
        self.conn.execute(
            """
            UPDATE incidents
            SET status = 'RESOLVED',
                resolved_at = ?,
                last_updated_at = ?
            WHERE watch_id = ? AND status = 'OPEN'
            """,
            (_dt_to_db(when), _dt_to_db(when), watch_id),
        )
        self.conn.commit()

    def get_incidents(self, load_id: str) -> list[Incident]:
        """All incidents for a load (OPEN and RESOLVED)."""
        cur = self.conn.execute(
            """
            SELECT * FROM incidents
            WHERE load_id = ?
            ORDER BY opened_at ASC
            """,
            (load_id,),
        )
        return [row_to_incident(r) for r in cur.fetchall()]

    # ----- investigator memory -----

    def write_investigator_run(
        self,
        *,
        load_id: str,
        input_context: str,
        output: str,
        timestamp: datetime | None = None,
    ) -> None:
        when = timestamp or now_ist_naive()
        self.conn.execute(
            """
            INSERT INTO investigator_runs (
                load_id, input_context, output, timestamp
            ) VALUES (?, ?, ?, ?)
            """,
            (
                load_id,
                input_context,
                output,
                _dt_to_db(when),
            ),
        )
        self.conn.commit()

    def build_memory_context(self, load: LoadData, current_state: str) -> LoadMemory:
        """Patterns, similar closed loads, and prior TRIAGE decisions for this load."""
        patterns: dict[str, PatternSummary] = {}
        for key in _pattern_keys(load, current_state):
            row = self.conn.execute(
                "SELECT * FROM pattern_store WHERE pattern_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                continue
            count = int(row["sample_count"] or 0)
            if count < PATTERN_MIN_SAMPLES:
                continue
            patterns[key] = PatternSummary(
                pattern_key=key,
                sample_count=count,
                avg_delta_min=row["avg_delta_min"],
                incident_rate=row["incident_rate"],
                commitment_breach_rate=row["commitment_breach_rate"],
            )

        episodes: list[LoadEpisode] = []
        if load.carrier_name:
            rows = self.conn.execute(
                """
                SELECT * FROM load_memory
                WHERE carrier_name = ?
                ORDER BY closed_at DESC
                LIMIT 5
                """,
                (load.carrier_name,),
            ).fetchall()
            episodes = [_row_to_episode(r) for r in rows]

        prior: list[dict] = []
        if load.load_id:
            rows = self.conn.execute(
                """
                SELECT output, timestamp FROM investigator_runs
                WHERE load_id = ?
                ORDER BY timestamp DESC
                LIMIT 3
                """,
                (load.load_id,),
            ).fetchall()
            for r in rows:
                parsed = None
                try:
                    parsed = json.loads(r["output"])
                except (TypeError, json.JSONDecodeError):
                    parsed = r["output"]
                prior.append({"output": parsed, "timestamp": r["timestamp"]})

        return LoadMemory(
            patterns=patterns,
            similar_episodes=episodes,
            prior_wake_decisions=prior,
        )

    def write_load_memory(
        self,
        load: LoadData,
        watch: Watch,
        state_rows: list[StateRow],
        *,
        at: datetime | None = None,
    ) -> None:
        if not load.load_id:
            return
        when = at or now_ist_naive()
        incidents = self.get_incidents(load.load_id)
        incidents_by_state: dict[str, list[Incident]] = {}
        for incident in incidents:
            parts = [p.strip() for p in (incident.correlation_key or "").split("|")]
            state = parts[1] if len(parts) == 3 else None
            if state:
                incidents_by_state.setdefault(state, []).append(incident)

        state_actuals: dict[str, dict] = {}
        for row in state_rows:
            if row.actual is None or row.planned is None:
                continue
            try:
                delta = int((row.actual - row.planned).total_seconds() / 60)
            except TypeError:
                continue
            state_incidents = incidents_by_state.get(row.current_state, [])
            severities = [i.severity for i in state_incidents if i.severity]
            state_actuals[row.current_state] = {
                "delta_min": delta,
                "incident": bool(state_incidents),
                "severity": severities[0] if severities else None,
            }

        dropoff = next(
            (r for r in state_rows if r.current_state == "ENROUTE_DROPOFF"),
            None,
        )
        total_delay_min = None
        if dropoff is not None and dropoff.actual is not None and dropoff.planned is not None:
            try:
                total_delay_min = int((dropoff.actual - dropoff.planned).total_seconds() / 60)
            except TypeError:
                total_delay_min = None

        decisions = self.conn.execute(
            """
            SELECT output FROM investigator_runs
            WHERE load_id = ?
            ORDER BY timestamp ASC
            """,
            (load.load_id,),
        ).fetchall()
        severities: list[str] = []
        for d in decisions:
            try:
                out = json.loads(d["output"])
                if isinstance(out, dict) and out.get("overall_severity"):
                    severities.append(str(out["overall_severity"]))
            except (TypeError, json.JSONDecodeError):
                continue
        severity_rank = {"NONE": 0, "CLEAR": 1, "WATCH": 2, "ELEVATED": 3, "CRITICAL": 4}
        highest = max(severities, key=lambda s: severity_rank.get(s, 0), default="NONE")

        self.conn.execute(
            """
            INSERT INTO load_memory (
                load_id, carrier_name, shipper_name, origin, destination, closed_at,
                commitment_met, total_delay_min,
                highest_severity, state_actuals, investigator_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                load.load_id,
                load.carrier_name,
                load.shipper_name,
                load.origin,
                load.destination,
                _dt_to_db(when),
                0 if watch.commitment_state == "BREACHED" else 1,
                total_delay_min,
                highest,
                json.dumps(state_actuals),
                json.dumps({"severity_trajectory": severities}),
            ),
        )
        self.conn.commit()

    def update_patterns(
        self,
        load: LoadData,
        state_rows: list[StateRow],
        watch: Watch | None = None,
    ) -> None:
        incidents = self.get_incidents(load.load_id) if load.load_id else []
        incident_states = set()
        for incident in incidents:
            parts = [p.strip() for p in (incident.correlation_key or "").split("|")]
            if len(parts) == 3:
                incident_states.add(parts[1])
        was_breached = bool(watch and watch.commitment_state == "BREACHED")

        for row in state_rows:
            if row.actual is None or row.planned is None:
                continue
            try:
                delta_min = (row.actual - row.planned).total_seconds() / 60
            except TypeError:
                continue
            had_incident = row.current_state in incident_states
            for key in _pattern_keys(load, row.current_state):
                self._upsert_pattern(key, delta_min, had_incident, was_breached)

    def _upsert_pattern(
        self,
        key: str,
        delta_min: float,
        had_incident: bool,
        was_breached: bool,
    ) -> None:
        now = now_ist_naive()
        row = self.conn.execute(
            "SELECT * FROM pattern_store WHERE pattern_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO pattern_store (
                    pattern_key, sample_count, avg_delta_min, incident_rate,
                    commitment_breach_rate, last_updated, first_seen
                ) VALUES (?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    delta_min,
                    1.0 if had_incident else 0.0,
                    1.0 if was_breached else 0.0,
                    _dt_to_db(now),
                    _dt_to_db(now),
                ),
            )
        else:
            n = int(row["sample_count"] or 0)
            new_n = n + 1
            old_avg = float(row["avg_delta_min"] or 0)
            new_avg = (old_avg * n + delta_min) / new_n
            old_inc = float(row["incident_rate"] or 0)
            old_breach = float(row["commitment_breach_rate"] or 0)
            self.conn.execute(
                """
                UPDATE pattern_store SET
                    sample_count = ?,
                    avg_delta_min = ?,
                    incident_rate = ?,
                    commitment_breach_rate = ?,
                    last_updated = ?
                WHERE pattern_key = ?
                """,
                (
                    new_n,
                    new_avg,
                    (old_inc * n + (1.0 if had_incident else 0.0)) / new_n,
                    (old_breach * n + (1.0 if was_breached else 0.0)) / new_n,
                    _dt_to_db(now),
                    key,
                ),
            )
        self.conn.commit()

    # -------------------- Domino --------------------------

    def apply_projected_forecasts(self, rows: list[StateRow], projection) -> list[StateRow]:
        """Write the domino projection onto subsequent rows' forecasted clock. 
        Skip rows with an actual time; recomputed every wake."""
        if not projection or not projection.projected:
            return rows
        changed = False
        for row in rows:
            if row.actual is not None:
                continue 
            delay_time = projection.projected.get(row.current_state)
            if delay_time is not None and row.forecasted != delay_time:
                row.forecasted = delay_time
                changed = True
        if changed:
            return self.upsert_state_rows(rows)
        return rows 


def _pattern_keys(load: LoadData, state: str) -> list[str]:
    keys: list[str] = []
    if load.carrier_name:
        keys.append(f"carrier:{load.carrier_name}:{state}")
    if load.origin and load.destination:
        keys.append(f"lane:{load.origin}:{load.destination}:{state}")
    if load.shipper_name:
        keys.append(f"shipper:{load.shipper_name}:{state}")
    return keys


def _row_to_episode(row) -> LoadEpisode:
    r = dict(row)
    actuals: dict = {}
    try:
        parsed = json.loads(r.get("state_actuals") or "{}")
        if isinstance(parsed, dict):
            actuals = parsed
    except (TypeError, json.JSONDecodeError):
        actuals = {}
    return LoadEpisode(
        load_id=r.get("load_id") or "",
        closed_at=_dt(r.get("closed_at")),
        commitment_met=bool(r.get("commitment_met")),
        total_delay_min=r.get("total_delay_min"),
        highest_severity=r.get("highest_severity") or "NONE",
        state_actuals=actuals,
        carrier_name=r.get("carrier_name"),
        origin=r.get("origin"),
        destination=r.get("destination"),
    )

