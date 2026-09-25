# Validation rules

## Governing principle: flag, never silently drop

Every rule below writes a boolean column onto the trip record. **No rule
deletes a row.** Metrics then select the population they are entitled to
use:

| Population flag | Meaning | Used by |
|---|---|---|
| `is_valid_core` | The trip's time/distance measurements are usable | Speed, duration, the KPI |
| `is_valid_zone` | …and it can be attributed to a borough | Borough/zone breakdowns |
| `is_valid_fare` | …and its money fields are meaningful | Fare and tip metrics |

Two consequences matter:

1. **Different metrics legitimately have different denominators.** Each
   published metric therefore carries its own `trips` count.
2. **Every exclusion stays countable.** `m_data_quality` reports exactly
   what was removed and why, so the KPI's denominator is auditable rather
   than asserted. This is the point of the whole approach: a reader can
   reconstruct the population the KPI was computed on.

## The rules

Observed rates are from yellow, 2024-01 (2,964,624 trips). Abort limits
carry deliberate headroom: they exist to catch a *structural break* — a
corrupted extract, a vendor change — not normal month-to-month noise.

| ID | Condition (TRUE = violation) | Action | Observed | Abort limit |
|---|---|---|---|---|
| V01 | `dropoff_ts <= pickup_ts` | exclude_core | 0.029% | 5% |
| V02 | duration < 1 min or > 180 min | exclude_core | 1.25% | 10% |
| V03 | distance ≤ 0 or > 100 mi | exclude_core | 2.04% | 10% |
| V04 | speed > 80 mph | exclude_core | 0.037% | 5% |
| V05 | pickup outside the extract's month | exclude_core | 0.001% | 5% |
| V06 | pickup or dropoff zone in (264, 265) | exclude_zone | 1.06% | 10% |
| V07 | `total_amount < 0` | exclude_fare | 1.20% | 10% |
| V08 | `passenger_count` null or 0 | flag_only | 5.79% | 25% |

### Rationale for each

**V01 — dropoff before pickup.** A trip cannot end before it starts.
Duration would be negative and would corrupt every speed and duration
metric. Non-negotiable exclusion.

**V02 — duration out of range.** Sub-minute trips are meter errors or
immediate cancellations, not journeys. Trips over three hours are almost
always a meter left running after the passenger left — including them
would drag average duration up and average speed down, biasing the KPI
*toward* congestion.

**V03 — distance out of range.** Zero distance means no journey occurred.
Over 100 miles leaves the NYC service area entirely (JFK to the city is
~15 miles), indicating a GPS or meter fault.

**V04 — implausible speed.** Above 80 mph is not achievable on NYC
streets. Either the distance or the timestamps are wrong. These rows are
individually rare but matter disproportionately: they sit at the opposite
tail from the congestion threshold, so leaving them in would bias the KPI
*downward*. V02/V03/V04 together bracket the speed calculation from both
ends.

**V05 — pickup outside the month.** The monthly extract is the unit of
reprocessing. A December trip sitting in the January file would be counted
twice once December is also processed. This rule is what makes per-month
reruns safe.

**V06 — unknown zone.** Zones 264 and 265 are *codes*, not places:
"Unknown" and "Outside of NYC". The trip genuinely happened and its speed
is valid, so it stays in citywide metrics — but it cannot be attributed to
a borough, so it is excluded from geographic breakdowns only. Treating
these as ordinary zone IDs would create a phantom "Unknown borough" in
every report; dropping the trips entirely would understate citywide volume.
This split is the reason `is_valid_zone` exists separately from
`is_valid_core`.

**V07 — negative total.** Negative totals are refunds and chargebacks
booked against the original trip. They are legitimate accounting records,
and the underlying trip still happened and still has a valid speed — so
the trip stays in operational metrics and is removed only from fare
averages.

**V08 — missing passenger count.** Reported by the driver and frequently
left blank (~6% of trips, and ~7% for green). The field feeds **no**
headline metric. Excluding 6% of the population to protect a field nobody
uses would do more damage than the missing data does; it is therefore
flagged and kept. This is deliberately the one rule that changes no
denominator — it exists so the gap is visible rather than unrecorded.

## What the rules deliberately do *not* do

- **No imputation.** No missing value is filled with a mean, a median or a
  neighbouring value. An absent measurement stays absent and its trip is
  excluded from the affected metric. Imputing speed would manufacture the
  very quantity the KPI measures.
- **No outlier trimming on the KPI variable.** Slow trips are not outliers
  to be removed — they *are* the phenomenon under study. Only physically
  impossible values (V04) are removed, and the threshold is set by what a
  car can do, not by a percentile of the data. Trimming at, say, the 99th
  percentile would quietly delete part of the signal.
- **No deduplication.** Exact duplicates were measured at **zero** in
  2024-01, so a dedup step would add risk (of removing two legitimately
  identical short trips) with no benefit. If duplicates appear in a future
  month, V-series coverage would need extending — this is recorded as a
  monitored assumption in `known_unknowns.md`.
