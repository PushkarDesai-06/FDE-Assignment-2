# Source map

How the business question decomposes into information, and where that
information actually comes from.

## 1. The question chain

| # | Business question (asked by Ops) | Information required | Source | Grain |
|---|---|---|---|---|
| Q1 | How much of our service is running slowly? | Trip start/end time, distance | TLC trip extracts | 1 row / trip |
| Q2 | *Where* is it slow? | Pickup/dropoff zone → borough | TLC trip extracts + zone lookup | 1 row / trip; 1 row / zone |
| Q3 | *When* is it slow? | Pickup timestamp → hour, weekday | TLC trip extracts | 1 row / trip |
| Q4 | Is it the weather, or is it structural? | Hourly precipitation | Open-Meteo archive API | 1 row / hour |
| Q5 | Are airport runs affected differently? | Airport zone membership | Zone lookup (zones 1, 132, 138) | 1 row / zone |
| Q6 | Can we trust the answer? | Rejected-record counts by reason | Produced by the pipeline itself | 1 row / rule / fleet / month |

## 2. Sources in detail

### S1 — Yellow taxi trip records
- **Owner:** NYC Taxi & Limousine Commission (TLC). Records are submitted by
  authorised technology vendors (CMT, Curb), not by TLC itself.
- **Retrieval:** file download, Parquet over HTTPS (CloudFront).
- **Grain:** one row per completed trip.
- **Volume:** ~2.9–3.8M rows/month (~50–60 MB).
- **Refresh:** monthly, published roughly two months in arrears.
- **Authority:** this is the system of record for trip fares; it is the
  billing dataset, which is why fare fields are more reliable than
  operational fields like `passenger_count`.

### S2 — Green taxi trip records
- **Owner:** TLC, same vendor submission process.
- **Retrieval:** file download, Parquet over HTTPS.
- **Grain:** one row per completed trip.
- **Volume:** ~55k rows/month — roughly **1.6% of yellow volume**.
- **Why included:** green cabs (boro taxis) may only pick up outside the
  Manhattan core, so they are a structurally different service covering the
  outer boroughs. Including them tests whether the pipeline genuinely
  handles multiple source systems rather than one file in two copies.
- **Schema divergence from S1:** documented in `src/pipeline/schema.py`.
  Timestamps are `lpep_*` not `tpep_*`; green adds `ehail_fee` and
  `trip_type`; green has no airport-fee column.

### S3 — Taxi zone lookup
- **Owner:** TLC.
- **Retrieval:** file download, CSV.
- **Grain:** one row per zone; 265 zones.
- **Role:** the only bridge from `LocationID` to a human-meaningful borough
  or airport. Without it the trip data cannot answer Q2 at all.
- **Sentinel values:** `264 = Unknown`, `265 = Outside of NYC`. These are
  *codes, not zones*, and are handled by rule V06.

### S4 — Hourly weather (Open-Meteo archive API)
- **Owner:** Open-Meteo, serving ERA5 reanalysis data.
- **Retrieval:** **REST API returning JSON** — the second retrieval mode.
- **Grain:** one row per hour, for one point (Central Park, 40.7794/-73.9692).
- **Role:** the only source outside TLC's control. It supplies the
  confounder needed for Q4: without it, a congestion spike cannot be
  attributed.
- **Known weakness:** one point stands in for all five boroughs. See
  `known_unknowns.md`.

### S5 — TLC data dictionaries (reference, not ingested)
- Used to interpret coded fields (`payment_type`, `RatecodeID`,
  `store_and_fwd_flag`). Read by a human; not a pipeline input.

## 3. Ownership and trust ordering

Ranked by how much the pipeline trusts each field:

1. **Timestamps and fare fields** — the billing record; commercially
   audited, so errors are rare and usually extreme rather than subtle.
2. **Zone IDs** — system-assigned from GPS; reliable but with an explicit
   unknown code that must be handled, not ignored.
3. **`trip_distance`** — meter-reported. Generally sound, but the source of
   most implausible-speed records when it disagrees with the timestamps.
4. **`passenger_count`** — driver-entered, frequently unreported (~6% of
   rows). Deliberately used by **no** headline metric.

## 4. Gaps this data cannot fill

These bound what the KPI can legitimately claim:

| Gap | Consequence |
|---|---|
| No route or waypoints | Cannot tell *where along the trip* delay accumulated — only that it did. Interventions are zone-level, not street-level. |
| Zones, not coordinates | ~260 zones is a coarse geography; a slow zone may contain fast and slow streets. |
| No driver or vehicle ID | Cannot separate driver behaviour from road conditions. |
| No cancelled or unserved requests | Only *completed* trips are recorded. Unmet demand is structurally invisible — a zone nobody would serve looks fine. |
| No congestion/incident feed | Weather is the only external explanatory variable available. |
| Two-month publication lag | The KPI is a retrospective operational measure, not a real-time monitor. |

The last two are the most important for interpretation: **this pipeline
explains what happened, and can rank where to intervene — it cannot detect
a problem as it occurs.**
