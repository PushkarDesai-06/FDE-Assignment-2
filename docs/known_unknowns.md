# Known / Unknown / Assumption / Limitation

Everything here bounds how far the published KPI can be trusted. The rule
applied throughout: state the boundary rather than smooth over it.

---

## KNOWN — established from the data

| # | Finding | Evidence |
|---|---|---|
| K1 | **Congestion rose through H1 2024**: 31.5% → 37.9% (May), ending at 35.1% in June. | `m_congested_rate`, 6 months, 20.7M trips |
| K2 | **Midday is worse than rush hour in Manhattan.** Weekday 11:00–15:00 averages **57.3%** congested vs **44.3%** for the 17:00–19:00 evening peak. | `m_congested_rate`, weekday Manhattan |
| K3 | **Manhattan is the problem borough**: 37.7% congested at 9.9 mph, vs Queens 2.8% at 21.7 mph. | `m_congested_rate` by borough |
| K4 | **Queens' good score is an airport artefact**, not free-flowing streets. Airport trips average 23.1 mph and are 0.5% congested — they are highway runs. | `m_airport` |
| K5 | **Rain has a real but modest effect**: wet hours 38.4% congested vs dry 33.4% (+5.0pp); average speed 10.55 vs 11.19 mph (−5.7%). | `m_weather_effect` |
| K6 | **Green cabs are far less affected** (22.7% vs yellow 34.6%) — consistent with their outer-borough-only service area. | `m_congested_rate` by fleet |
| K7 | **Data quality degraded mid-series**: missing `passenger_count` (V08) doubled from 5.8% (Jan) to ~12.5% (Mar–Jun). | `m_data_quality` |
| K8 | **2.6% of trips are unusable** for speed metrics, dominated by zero/implausible distance (1.85%) and out-of-range duration (1.26%). | `m_data_quality` |
| K9 | Exact duplicate trips: **zero** across all 6 months. | profiling, notebook 03 |

### On K7 — the discovered structural break

The jump in V08 between February and March is a genuine change in the
upstream feed, not noise:

| Month | V08 rate |
|---|---|
| 2024-01 | 5.81% |
| 2024-02 | 7.29% |
| 2024-03 | **12.89%** |
| 2024-04 | 12.62% |
| 2024-05 | 11.81% |
| 2024-06 | 12.49% |

This vindicates the decision to make V08 **flag-only**. Had missing
passenger count been an exclusion rule, the pipeline would have started
discarding ~12% of trips from March onward — and the KPI would have
shifted mid-series for a reason that has nothing to do with traffic. The
break would have been invisible in the headline number and nearly
impossible to diagnose after the fact. Because the rule only flags, the
break shows up in `m_data_quality` where it belongs, and the KPI is
untouched.

---

## UNKNOWN — cannot be answered with these sources

| # | Unknown | Why it matters |
|---|---|---|
| U1 | **Unmet demand.** Only completed trips are recorded. | A zone nobody will serve looks *healthy*. The KPI cannot see service refusal or unavailability — arguably the worst customer outcome. |
| U2 | **Cause of any individual delay.** No route, incident or roadworks data. | We can rank *where* to look, never say *why* without fieldwork. |
| U3 | **Where within a trip the delay accrued.** Only endpoints are recorded. | Interventions must be zone-level; street-level targeting is impossible. |
| U4 | **Ride-hail (Uber/Lyft) traffic**, which is several times taxi volume. | Taxis are a *sample* of street conditions, not a census. A change in taxi speeds may reflect a change in taxi behaviour rather than in traffic. |
| U5 | **Driver and vehicle identity.** | Cannot separate driver behaviour from road conditions. |
| U6 | **Whether a past intervention worked.** No intervention log exists. | The series can detect a change; attributing it requires external records. |

---

## ASSUMPTION — taken deliberately, each falsifiable

| # | Assumption | Basis | If wrong |
|---|---|---|---|
| A1 | Average speed is a valid proxy for congestion. | Distance-normalised; the only signal these fields support. | A trip slowed by a long passenger stop is miscounted as congestion. |
| A2 | **8 mph is a meaningful congestion threshold.** | Median trip speed is 9.7 mph; 8 mph sits just below it. | The *level* of the KPI changes materially — see L1. |
| A3 | `trip_distance` is the distance actually driven. | Meter-reported, commercially audited. | A meter fault inflates speed; bounded by V03/V04. |
| A4 | Central Park weather represents all five boroughs. | Standard NYC reference station. | Localised storms are misattributed; understates weather effect. |
| A5 | TLC and Open-Meteo timestamps are both local NYC wall-clock time. | Confirmed against API response and TLC dictionary. | Weather joins to the wrong hour — see L3. |
| A6 | A monthly extract is complete when published. | TLC publishes ~2 months in arrears, after vendor reconciliation. | Late-arriving trips would be missed; C5 volume check would catch a large shortfall. |
| A7 | Zero duplicates will continue. | Measured at zero across 6 months. | **Monitored, not guaranteed** — no dedup rule exists, so duplicates would inflate counts. Add a V09 rule if this ever changes. |

---

## LIMITATION — structural, not fixable within this project

**L1 — The KPI's absolute level is threshold-dependent.**
Measured across all 20.7M trips:

| Threshold | Congested |
|---|---|
| 6 mph | 14.7% |
| 7 mph | 23.8% |
| **8 mph** | **34.3%** |
| 9 mph | 45.0% |
| 10 mph | 55.0% |

**However, the borough ranking is completely stable:**

| Threshold | Ranking |
|---|---|
| 6 mph | Manhattan (16.2) > Brooklyn (12.0) > Bronx (9.7) > Queens (1.1) |
| 8 mph | Manhattan (37.7) > Brooklyn (28.1) > Bronx (21.2) > Queens (2.8) |
| 10 mph | Manhattan (60.3) > Brooklyn (46.8) > Bronx (36.4) > Queens (6.0) |

This is the single most important caveat for how the output should be
used. **"34% of trips are congested" is not a fact about New York — it is
a fact about an 8 mph line.** But *"Manhattan is worse than Brooklyn,
which is worse than the Bronx"* holds at every threshold tested.

The pipeline is therefore **dependable for prioritisation and for tracking
change over time; it should not be quoted as an absolute measure of
congestion.** Where this output is used to rank zones, compare months or
target interventions, the threshold choice does not affect the conclusion.

**L2 — Taxis are a biased sample of traffic.** Concentrated in Manhattan
and at airports, absent from much of the outer boroughs. The Bronx figure
rests on 55k trips over six months versus Manhattan's 17.9M; it is
directionally useful, not precise.

**L3 — DST misalignment on one day.** Open-Meteo returns a fixed-offset
series that includes `2024-03-10T02:00`, an hour that does not exist in NY
wall-clock time. On that one day weather is misaligned by up to an hour.
Impact: 1 day in 182, affecting only the weather metric. Documented rather
than corrected, because a correction would be invisible to downstream
users while this note is not.

**L4 — Six months cannot separate trend from season.** The Jan→May rise
is consistent with seasonal recovery from winter lows; the June dip may be
seasonal too. **Twelve months are needed before the trend in K1 should be
described as a trend.** The pipeline is built to extend — adding months to
`config/pipeline.yaml` and rerunning is the whole change.

**L5 — Retrospective, not operational.** The ~2-month publication lag
makes this a planning instrument, not a monitor. It answers "where should
we have intervened", which is the right question for budgeting and
targeting, and the wrong one for live dispatch.
