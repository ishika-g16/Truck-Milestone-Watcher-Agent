# Intervention conditions (implemented)

Five deterministic flags, computed in `flags.py`/`domino.py` - no LLM involved in whether
any of these fire. Each firing flag on the **active** state row upserts an incident
(`load_id | state | type`), except `DROPOFF_RISK` which always keys to `ENROUTE_DROPOFF`
(see `design_decisions.md` §10). `STATUS_UPDATE_MISSING` is not implemented. GPS never
completes a milestone.

The investigator (Claude) may suppress a finding or rewrite `recommended_action` once
overall severity is `ELEVATED`/`CRITICAL`. It never changes whether a flag itself fired.

---

## 1. `TRANSITION_OVERDUE` → `progress_state`

**Open when:** `now > planned + 30 minutes` on the active row (`OPERATIONAL_GRACE`).

There is **no** extra per-state due buffer beyond what's already inside `planned`.
Dispatch (+2h) and pickup dwell (+18h) are already folded into `planned` for the rows
below.

| Active row | `planned` |
|---|---|
| `ASSIGNED` | `assigned_at` + 2h |
| `ENROUTE_PICKUP` | pickup `arrive_time` |
| `AT_PICKUP` | planned pickup + 18h dwell |
| `LOADED` | planned pickup + 18h dwell + 2h dispatch |
| `ENROUTE_DROPOFF` | dropoff `arrive_time` |

No overdue flag on `AT_DROPOFF` - that status has no ledger row; the watch just closes.

---

## 2. `COMMITMENT_RISK` → `commitment_state`

**En route only** (`ENROUTE_PICKUP`, `ENROUTE_DROPOFF`). Dwell rows and `ASSIGNED` never
fire this flag.

| Condition | Dimension | Flag |
|---|---|---|
| No `committed` appointment | `UNAVAILABLE` | false |
| `now > committed` and `actual` is missing | `BREACHED` | true |
| `forecasted > committed` and `actual` is missing | `AT_RISK` | true |
| Not en route | `NOT_APPLICABLE` | false |

`forecasted` is the live GPS ETA while en route with a live ping; otherwise a copy of
`planned`, so `AT_RISK` can still fire without GPS.

---

## 3. `STALE_GPS` → `evidence_state`

**Live sources only:** `TELEMATICS`, `MOBILE_APP`, `CELLULAR_TRACKING`.

**Open when:** a live source and (`last_activity` missing, or `now − last_activity` is
more than 1 hour).

`FASTAG` pings are fetched from the same `location_log` table but are **not** live
tracking - a toll checkpoint scan, not continuous telemetry. `FASTAG`/no-GPS reads as
`evidence_state = GPS_UNAVAILABLE`, never `STALE_GPS`.

---

## 4. `STATUS_GPS_CONFLICT` → `evidence_state`

Live GPS vs. claimed stop, **1 km** haversine. Skip `ASSIGNED` (no location claim to
conflict with yet). 1 km is a **conflict radius**, not completion evidence - it never
advances or completes a milestone on its own.

| Active state | Conflict when |
|---|---|
| `ENROUTE_PICKUP` | GPS **near pickup** (status hasn't caught up to reality) |
| `AT_PICKUP` | GPS **not** near pickup (status is ahead of reality) |
| `LOADED` | GPS **near dropoff** (far too early to be there) |
| `ENROUTE_DROPOFF` | GPS **near dropoff** (status hasn't caught up to reality) |

`evidence_state` priority, most to least severe: `CONFLICTING`, `STALE_GPS`,
`UNMAPPED_STATUS`, `GPS_UNAVAILABLE`, `RELIABLE`.

---

## 5. `DROPOFF_RISK` → own incident type (`domino.py`)

Runs every wake as its own deterministic step, independent of whether any of the four
flags above fired - so it also catches delay accumulating gradually across several
milestones, each individually too small to trip its own flag.

**Steps (`propagate_delay()`):**
1. `delay = max(0, (active_row.actual or live ETA or now) − active_row.planned)`, in
   minutes. Below `DOMINO_MIN_SLIP` (30 min) → stop here, verdict `SAFE`.
2. Every incomplete row after the active one gets `planned + delay` written as its
   projected time - the same delay shifted uniformly onto each downstream milestone.
3. Compare the projected `ENROUTE_DROPOFF` arrival against the committed dropoff
   appointment:
   - `SAFE` - on time or early
   - `AT_RISK` - late, but within `DROPOFF_RISK_MARGIN` (60 min)
   - `BREACH_LIKELY` - late beyond that margin
   - `UNKNOWN` - either time is missing

**Open when:** verdict is `AT_RISK` or `BREACH_LIKELY`.

If the active row is already `ENROUTE_DROPOFF` itself, there's nothing downstream left to
shift - but that doesn't make the load safe by default: the live ETA/actual from step 1
is used directly as the projected dropoff arrival instead.
