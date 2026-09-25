-- Metric definitions.
-- Each block below is executed by src/pipeline/metrics.py against the
-- month's fct_trip table and written to its own partitioned mart.
-- The {month} placeholder is substituted by the pipeline.
--
-- Population rules:
--   speed/duration metrics use is_valid_core
--   borough/zone breakdowns additionally require is_valid_zone
--   fare metrics use is_valid_fare
-- This is why the same month can legitimately report different denominators
-- for different metrics - each is stated in the metric's own `trips` column.

-- ============================================================ M1 (the KPI)
-- name: m_congested_rate
-- Share of valid trips travelling below the congestion speed threshold,
-- cut by borough and hour of day. This is the number operations acts on.
SELECT
    '{month}'                       AS month,
    fleet,
    pu_borough                      AS borough,
    pickup_hour,
    is_weekend,
    count(*)                        AS trips,
    sum(CASE WHEN is_congested THEN 1 ELSE 0 END) AS congested_trips,
    round(100.0 * sum(CASE WHEN is_congested THEN 1 ELSE 0 END) / count(*), 2)
                                    AS congested_rate_pct,
    round(avg(speed_mph), 2)        AS avg_speed_mph,
    round(median(duration_min), 2)  AS median_duration_min
FROM fct_trip
WHERE is_valid_core AND is_valid_zone
GROUP BY ALL;

-- ==================================================================== M2
-- name: m_speed_duration
-- Trip efficiency by borough and hour: how long trips take and how fast
-- they move. Gives the KPI its context (a high congested rate in a borough
-- with long trips is a bigger operational problem than in one with short).
SELECT
    '{month}'                          AS month,
    fleet,
    pu_borough                         AS borough,
    pickup_hour,
    count(*)                           AS trips,
    round(median(duration_min), 2)     AS median_duration_min,
    round(avg(duration_min), 2)        AS avg_duration_min,
    round(median(speed_mph), 2)        AS median_speed_mph,
    round(avg(speed_mph), 2)           AS avg_speed_mph,
    round(median(trip_distance_mi), 2) AS median_distance_mi
FROM fct_trip
WHERE is_valid_core AND is_valid_zone
GROUP BY ALL;

-- ==================================================================== M3
-- name: m_airport
-- Airport trips (JFK/LGA/EWR at either end) against the rest of the network.
-- Airport runs are a distinct operational product: longer, more predictable,
-- and commercially important, so they are tracked separately.
SELECT
    '{month}'                       AS month,
    fleet,
    is_airport_trip,
    count(*)                        AS trips,
    round(median(duration_min), 2)  AS median_duration_min,
    round(avg(speed_mph), 2)        AS avg_speed_mph,
    round(100.0 * sum(CASE WHEN is_congested THEN 1 ELSE 0 END) / count(*), 2)
                                    AS congested_rate_pct,
    round(median(trip_distance_mi), 2) AS median_distance_mi,
    -- Fare stats use the fare-valid population, hence the FILTER clause.
    round(avg(total_amount) FILTER (WHERE is_valid_fare), 2) AS avg_total_amount,
    count(*) FILTER (WHERE is_valid_fare)                    AS fare_trips
FROM fct_trip
WHERE is_valid_core
GROUP BY ALL;

-- ==================================================================== M4
-- name: m_weather_effect
-- Does rain measurably slow the network? Compares wet against dry hours.
-- This is the "explains the KPI" metric: if congestion spikes are weather
-- driven, that is a different intervention than if they are structural.
SELECT
    '{month}'                       AS month,
    fleet,
    is_wet_hour,
    is_weekend,
    count(*)                        AS trips,
    round(avg(speed_mph), 2)        AS avg_speed_mph,
    round(median(duration_min), 2)  AS median_duration_min,
    round(100.0 * sum(CASE WHEN is_congested THEN 1 ELSE 0 END) / count(*), 2)
                                    AS congested_rate_pct,
    round(avg(precip_mm), 3)        AS avg_precip_mm
FROM fct_trip
WHERE is_valid_core
GROUP BY ALL;

-- ==================================================================== M5
-- name: m_data_quality
-- What the pipeline rejected and why. This is a trust metric: it is
-- published alongside the KPI so that a reader can see the denominator
-- the KPI was computed on, and spot a month where quality degraded.
SELECT
    '{month}'  AS month,
    fleet,
    rule_id,
    action,
    hits,
    total_trips,
    round(100.0 * hits / total_trips, 4) AS hit_pct
FROM (
    SELECT fleet, count(*) AS total_trips,
           sum(CASE WHEN v_v01 THEN 1 ELSE 0 END) AS "V01_dropoff_before_pickup",
           sum(CASE WHEN v_v02 THEN 1 ELSE 0 END) AS "V02_duration_out_of_range",
           sum(CASE WHEN v_v03 THEN 1 ELSE 0 END) AS "V03_distance_out_of_range",
           sum(CASE WHEN v_v04 THEN 1 ELSE 0 END) AS "V04_implausible_speed",
           sum(CASE WHEN v_v05 THEN 1 ELSE 0 END) AS "V05_pickup_outside_month",
           sum(CASE WHEN v_v06 THEN 1 ELSE 0 END) AS "V06_unknown_zone",
           sum(CASE WHEN v_v07 THEN 1 ELSE 0 END) AS "V07_negative_total",
           sum(CASE WHEN v_v08 THEN 1 ELSE 0 END) AS "V08_missing_passenger_count"
    FROM fct_trip GROUP BY fleet
)
UNPIVOT (hits FOR rule_id IN (
    "V01_dropoff_before_pickup", "V02_duration_out_of_range",
    "V03_distance_out_of_range", "V04_implausible_speed",
    "V05_pickup_outside_month", "V06_unknown_zone",
    "V07_negative_total", "V08_missing_passenger_count"
))
LEFT JOIN (SELECT * FROM rule_actions) ra ON ra.id = rule_id;
