# Final evidence table

Generated from the published marts by `python -m pipeline.report` on
2026-09-25 11:36 UTC. Do not edit by hand.

**KPI definition:** share of valid trips whose average speed is below
8 mph ("congested trip rate").
**Coverage:** 6 months, 20,009,812 zone-attributable valid trips.

---

## M1 — Congested trip rate by month *(the KPI)*

| Month | Valid trips | Congested | KPI % | Avg speed (mph) |
|---|---|---|---|---|
| 2024-01 | 2,915,737 | 917,599 | 31.47 | 11.45 |
| 2024-02 | 2,950,680 | 993,132 | 33.66 | 11.1 |
| 2024-03 | 3,502,288 | 1,159,929 | 33.12 | 11.12 |
| 2024-04 | 3,473,877 | 1,202,497 | 34.62 | 11.04 |
| 2024-05 | 3,680,008 | 1,393,812 | 37.88 | 10.59 |
| 2024-06 | 3,487,222 | 1,222,560 | 35.06 | 10.84 |

Congestion rose 6.4 points from January to its May peak.

## M2 — Trip efficiency by borough

| Borough | Trips | KPI % | Avg speed (mph) | Median duration (min) |
|---|---|---|---|---|
| Manhattan | 17,904,014 | 37.71 | 9.93 | 11.8 |
| Brooklyn | 277,960 | 28.05 | 11.78 | 24.58 |
| Bronx | 55,460 | 21.16 | 13.45 | 30.72 |
| Queens | 1,771,736 | 2.77 | 21.71 | 34.19 |

Manhattan is the operational problem. Queens' low rate is an airport
artefact (see M3), not free-flowing streets.

## M2b — Worst weekday hours in Manhattan

| Hour | KPI % | Trips |
|---|---|---|
| 11 | 59.0 | 637,951 |
| 12 | 58.2 | 682,826 |
| 15 | 57.5 | 791,581 |
| 14 | 57.2 | 762,284 |
| 13 | 55.0 | 698,096 |

**Midday, not rush hour.** Weekday 11:00–15:00 averages 57.3% congested
against 44.3% for the 17:00–19:00 evening peak.

## M3 — Airport vs non-airport trips

| Segment | Trips | Median duration (min) | Avg speed (mph) | KPI % |
|---|---|---|---|---|
| Airport trip | 1,953,414 | 35.6 | 23.09 | 0.5 |
| Non-airport | 18,243,058 | 11.71 | 9.79 | 37.93 |

Airport runs are highway trips: 3x the duration, but 2.4x the speed and
almost never congested.

## M4 — Weather effect

| Condition | Trips | Avg speed (mph) | KPI % |
|---|---|---|---|
| Dry hour | 16,555,476 | 11.19 | 33.41 |
| Wet hour | 3,640,996 | 10.55 | 38.38 |

Rain costs ~5.7% of network speed and adds ~5 points to the congested
rate. Real, but far smaller than the midday effect — congestion is
mostly structural, not weather-driven.

## M5 — Data quality *(trust metric)*

| Rule | Action | Trips affected | % of all trips |
|---|---|---|---|
| V08_missing_passenger_count | flag_only | 2,214,430 | 10.712 |
| V03_distance_out_of_range | exclude_core | 382,955 | 1.853 |
| V02_duration_out_of_range | exclude_core | 259,976 | 1.258 |
| V07_negative_total | exclude_fare | 258,613 | 1.251 |
| V06_unknown_zone | exclude_zone | 212,900 | 1.03 |
| V04_implausible_speed | exclude_core | 7,772 | 0.038 |
| V01_dropoff_before_pickup | exclude_core | 6,512 | 0.032 |
| V05_pickup_outside_month | exclude_core | 193 | 0.001 |

2.6% of trips are excluded from speed metrics. `V08` is flag-only and
excludes nothing — its rate doubled in March 2024, a change in the
upstream feed rather than in traffic. See `known_unknowns.md` (K7).

---

## What decision this supports

Rank zones and hours for curb-management, signal-retiming and dispatch
intervention, and measure month over month whether an intervention moved
the number.

**Use for ranking and trend. Do not quote the absolute level** — it
depends on the 8 mph threshold. The borough ranking is stable at every
threshold tested (6–10 mph); the headline percentage is not. See
`known_unknowns.md` (L1).
