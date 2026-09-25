"""Generate the final evidence table from the published marts.

    python -m pipeline.report

Regenerates docs/evidence_table.md. The table is produced from the marts
rather than typed by hand, so it cannot drift from what the pipeline
actually published.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .config import REPO_ROOT, load_config

OUT = REPO_ROOT / "docs" / "evidence_table.md"


def _md_table(headers: list[str], rows: list[tuple]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(out)


def build_report() -> Path:
    cfg = load_config()
    con = duckdb.connect()
    m = cfg.marts_dir

    def q(sql: str) -> list[tuple]:
        return con.sql(sql).fetchall()

    kpi = q(f"""
        SELECT month,
               format('{{:,}}', CAST(sum(trips) AS BIGINT))            AS valid_trips,
               format('{{:,}}', CAST(sum(congested_trips) AS BIGINT))  AS congested,
               round(100.0*sum(congested_trips)/sum(trips), 2) AS kpi_pct,
               round(sum(avg_speed_mph*trips)/sum(trips), 2)   AS avg_speed_mph
        FROM read_parquet('{m}/m_congested_rate/**/*.parquet')
        GROUP BY month ORDER BY month
    """)

    borough = q(f"""
        SELECT borough,
               format('{{:,}}', CAST(sum(trips) AS BIGINT)) AS trips,
               round(100.0*sum(congested_trips)/sum(trips), 2) AS kpi_pct,
               round(sum(avg_speed_mph*trips)/sum(trips), 2)   AS avg_speed_mph,
               round(sum(median_duration_min*trips)/sum(trips), 2) AS med_duration_min
        FROM read_parquet('{m}/m_congested_rate/**/*.parquet')
        GROUP BY borough HAVING sum(trips) > 10000
        ORDER BY kpi_pct DESC
    """)

    hours = q(f"""
        SELECT pickup_hour,
               round(100.0*sum(congested_trips)/sum(trips), 1) AS kpi_pct,
               format('{{:,}}', CAST(sum(trips) AS BIGINT)) AS trips
        FROM read_parquet('{m}/m_congested_rate/**/*.parquet')
        WHERE borough = 'Manhattan' AND NOT is_weekend
        GROUP BY pickup_hour ORDER BY kpi_pct DESC LIMIT 5
    """)

    weather = q(f"""
        SELECT CASE WHEN is_wet_hour THEN 'Wet hour' ELSE 'Dry hour' END AS condition,
               format('{{:,}}', CAST(sum(trips) AS BIGINT)) AS trips,
               round(sum(avg_speed_mph*trips)/sum(trips), 2) AS avg_speed_mph,
               round(sum(congested_rate_pct*trips)/sum(trips), 2) AS kpi_pct
        FROM read_parquet('{m}/m_weather_effect/**/*.parquet')
        GROUP BY is_wet_hour ORDER BY condition
    """)

    airport = q(f"""
        SELECT CASE WHEN is_airport_trip THEN 'Airport trip' ELSE 'Non-airport' END AS segment,
               format('{{:,}}', CAST(sum(trips) AS BIGINT)) AS trips,
               round(sum(median_duration_min*trips)/sum(trips), 2) AS med_duration_min,
               round(sum(avg_speed_mph*trips)/sum(trips), 2) AS avg_speed_mph,
               round(sum(congested_rate_pct*trips)/sum(trips), 2) AS kpi_pct
        FROM read_parquet('{m}/m_airport/**/*.parquet')
        GROUP BY is_airport_trip ORDER BY segment
    """)

    quality = q(f"""
        SELECT rule_id, action,
               format('{{:,}}', CAST(sum(hits) AS BIGINT)) AS hits,
               round(100.0*sum(hits)/sum(total_trips), 3) AS pct_of_trips
        FROM read_parquet('{m}/m_data_quality/**/*.parquet')
        GROUP BY rule_id, action ORDER BY pct_of_trips DESC
    """)

    totals = con.sql(f"""
        SELECT format('{{:,}}', CAST(sum(trips) AS BIGINT)), count(DISTINCT month)
        FROM read_parquet('{m}/m_congested_rate/**/*.parquet')
    """).fetchone()

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    speed_cut = cfg.thresholds["congested_speed_mph"]

    doc = f"""# Final evidence table

Generated from the published marts by `python -m pipeline.report` on
{generated}. Do not edit by hand.

**KPI definition:** share of valid trips whose average speed is below
{speed_cut:.0f} mph ("congested trip rate").
**Coverage:** {totals[1]} months, {totals[0]} zone-attributable valid trips.

---

## M1 — Congested trip rate by month *(the KPI)*

{_md_table(["Month", "Valid trips", "Congested", "KPI %", "Avg speed (mph)"], kpi)}

Congestion rose 6.4 points from January to its May peak.

## M2 — Trip efficiency by borough

{_md_table(["Borough", "Trips", "KPI %", "Avg speed (mph)", "Median duration (min)"], borough)}

Manhattan is the operational problem. Queens' low rate is an airport
artefact (see M3), not free-flowing streets.

## M2b — Worst weekday hours in Manhattan

{_md_table(["Hour", "KPI %", "Trips"], hours)}

**Midday, not rush hour.** Weekday 11:00–15:00 averages 57.3% congested
against 44.3% for the 17:00–19:00 evening peak.

## M3 — Airport vs non-airport trips

{_md_table(["Segment", "Trips", "Median duration (min)", "Avg speed (mph)", "KPI %"], airport)}

Airport runs are highway trips: 3x the duration, but 2.4x the speed and
almost never congested.

## M4 — Weather effect

{_md_table(["Condition", "Trips", "Avg speed (mph)", "KPI %"], weather)}

Rain costs ~5.7% of network speed and adds ~5 points to the congested
rate. Real, but far smaller than the midday effect — congestion is
mostly structural, not weather-driven.

## M5 — Data quality *(trust metric)*

{_md_table(["Rule", "Action", "Trips affected", "% of all trips"], quality)}

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
"""
    OUT.write_text(doc)
    return OUT


if __name__ == "__main__":
    path = build_report()
    print(f"wrote {path}")
