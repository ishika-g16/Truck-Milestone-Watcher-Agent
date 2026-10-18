

from __future__ import annotations

from datetime import timedelta
from project.data.types import DelayProjection
from project.constants import DOMINO_MIN_SLIP, DROPOFF_RISK_MARGIN


def _mins(time) -> int:
   """converting timedelta to whole minutes"""
   return int(time.total_seconds() // 60)

def propagate_delay(state_rows, times, active_row, eta, now) -> DelayProjection:
   """calculating the delay of an active row and adding to subsequent states if it is past the minimum risk."""
   if active_row is None or active_row.planned is None:
      return DelayProjection(
         delay=0,
         projected={},
         dropoff_risk="UNKNOWN",
         dropoff_buffer=None,
         affected_states=[]
      )

   rows = sorted(state_rows, key=lambda r: r.seq)

   # 1. how far behind is the active row, right now
   time_reference = (active_row.actual or (eta.ETA_value if eta and eta.ETA_value else None) or now)
   delay = max(0, _mins(time_reference - active_row.planned))

   if delay < _mins(DOMINO_MIN_SLIP):
      return DelayProjection(
         delay=delay,
         projected={},
         dropoff_risk="SAFE",
         dropoff_buffer=None,
         affected_states=[]
      )

   # 2. propagating the delay onto subsequent incomplete milestones, if any
   subsequent = [r for r in rows if not r.completed and r.seq > active_row.seq]

   projected = {}
   for r in subsequent:
      if r.planned is not None:
         projected[r.current_state] = r.planned + timedelta(minutes=delay)

   # affect on dropoff
   dropoff_row = next((r for r in rows if r.current_state == "ENROUTE_DROPOFF"), None)
   committed_dropoff = (dropoff_row.committed if dropoff_row and dropoff_row.committed
                        else (times.committed_dropoff_at if times else None))
   # If ENROUTE_DROPOFF is still ahead of us, its projected arrival is in `projected`
   # above. If we ARE ENROUTE_DROPOFF (no subsequent rows left, since it's the last
   # tracked row), our own reference time from step 1 *is* the projected dropoff
   # arrival - there's nothing left downstream to roll it onto, but that doesn't make
   # us safe.
   if active_row.current_state == "ENROUTE_DROPOFF":
      projected_dropoff = time_reference
   else:
      projected_dropoff = projected.get("ENROUTE_DROPOFF")

   if committed_dropoff is None or projected_dropoff is None:
      dropoff_risk, dropoff_buffer = "UNKNOWN", None
   else:
      dropoff_slip = _mins(projected_dropoff - committed_dropoff)
      dropoff_buffer = -dropoff_slip

      if dropoff_slip <= 0:
         dropoff_risk = "SAFE" # a negative value means its early not late
      elif dropoff_slip <= _mins(DROPOFF_RISK_MARGIN):
         dropoff_risk = "AT_RISK"
      else:
         dropoff_risk = "BREACH_LIKELY"

   # 4. which states are affected?
   if dropoff_risk in ("AT_RISK", "BREACH_LIKELY"):
      affected_states = [r.current_state for r in subsequent
                         if r.current_state in projected]
   else:
      affected_states = []
   
   return DelayProjection(
      delay=delay,
      projected=projected,
      dropoff_risk=dropoff_risk,
      dropoff_buffer=dropoff_buffer,
      affected_states=affected_states
   )