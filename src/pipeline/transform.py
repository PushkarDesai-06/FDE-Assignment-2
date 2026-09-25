"""Stage 4 - Model the workflow.

Turns the validated per-fleet extracts into a small star-shaped model:

    dim_zone          one row per taxi zone (265)
    dim_time          one row per hour present in the data
    fct_weather_hour  one row per hour, the operating conditions
    fct_trip          one row per trip - the event table, carrying the trip
                      lifecycle (pickup -> dropoff -> payment), derived
                      duration/speed, validation flags and the KPI outcome

Idempotency: each month is written to its own partition directory, which is
removed and rewritten on every run. Reprocessing a month can therefore never
double-count, and reprocessing one month never touches another.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import duckdb

from .config import Config
from .logging_utils import RunSummary, get_logger
from .schema import normalise_select
from .completeness import parquet_columns


class TransformError(RuntimeError):
    """Raised when the model fails an integrity check."""


def _write_partition(
    con: duckdb.DuckDBPyConnection, relation: str, base: Path, month: str
) -> Path:
    """Write a relation to base/month=<month>/part.parquet, replacing it."""
    part_dir = base / f"month={month}"
    if part_dir.exists():
        shutil.rmtree(part_dir)  # idempotent rerun: replace, never append
    part_dir.mkdir(parents=True, exist_ok=True)
    out = part_dir / "part.parquet"
    con.execute(f"COPY ({relation}) TO '{out}' (FORMAT PARQUET)")
    return out


def build_dim_zone(con: duckdb.DuckDBPyConnection, cfg: Config) -> int:
    """Reference dimension: zone -> borough, with airport/unknown flags."""
    airports = ", ".join(str(z) for z in cfg.raw["airport_zone_ids"])
    unknown = ", ".join(str(z) for z in cfg.raw["unknown_zone_ids"])
    con.execute("DROP TABLE IF EXISTS dim_zone")
    con.execute(f"""
        CREATE TABLE dim_zone AS
        SELECT
            CAST(LocationID AS INTEGER) AS zone_id,
            Borough                     AS borough,
            Zone                        AS zone_name,
            service_zone,
            LocationID IN ({airports})  AS is_airport,
            LocationID IN ({unknown})   AS is_unknown
        FROM read_csv('{cfg.zone_lookup_path}', header=true, AUTO_DETECT=true)
    """)
    n = con.sql("SELECT count(*) FROM dim_zone").fetchone()[0]
    # The zone table is a fixed reference list; a change means TLC restructured it.
    if n < 260:
        raise TransformError(f"dim_zone has {n} rows, expected ~265")
    return n


def build_weather(con: duckdb.DuckDBPyConnection, cfg: Config, month: str) -> int:
    """Parse the raw Open-Meteo JSON into an hourly fact table.

    Timestamps are local NYC time, matching the TLC trip timestamps, so the
    join is on naive local wall-clock hours (see docs/known_unknowns.md for
    the DST caveat).
    """
    payload = json.loads(cfg.weather_path(month).read_text())
    h = payload["hourly"]
    wet = cfg.thresholds["wet_hour_precip_mm"]

    rows = list(zip(
        h["time"], h["temperature_2m"], h["precipitation"], h["snowfall"]
    ))
    con.execute("DROP TABLE IF EXISTS raw_weather")
    con.execute(
        "CREATE TABLE raw_weather (t VARCHAR, temp_c DOUBLE, precip_mm DOUBLE, snow_cm DOUBLE)"
    )
    con.executemany("INSERT INTO raw_weather VALUES (?, ?, ?, ?)", rows)

    con.execute("DROP TABLE IF EXISTS fct_weather_hour")
    con.execute(f"""
        CREATE TABLE fct_weather_hour AS
        SELECT
            CAST(strptime(t, '%Y-%m-%dT%H:%M') AS TIMESTAMP) AS hour_ts,
            temp_c,
            COALESCE(precip_mm, 0)          AS precip_mm,
            COALESCE(snow_cm, 0)            AS snow_cm,
            COALESCE(precip_mm, 0) >= {wet} AS is_wet
        FROM raw_weather
    """)
    return con.sql("SELECT count(*) FROM fct_weather_hour").fetchone()[0]


def build_stg_trips(
    con: duckdb.DuckDBPyConnection, cfg: Config, month: str
) -> dict[str, int]:
    """Normalise both fleets onto one schema and derive the workflow measures."""
    log = get_logger()
    selects = []
    counts: dict[str, int] = {}
    for fleet in cfg.fleets:
        path = cfg.trip_path(fleet, month)
        cols = parquet_columns(con, path)
        body = normalise_select(fleet, cols)
        selects.append(f"{body} FROM read_parquet('{path}')")
        counts[fleet] = con.sql(
            f"SELECT count(*) FROM read_parquet('{path}')"
        ).fetchone()[0]

    union = "\nUNION ALL\n".join(selects)

    con.execute("DROP TABLE IF EXISTS stg_trips")
    con.execute(f"""
        CREATE TABLE stg_trips AS
        WITH unioned AS ({union})
        SELECT
            *,
            -- Derived workflow measures. Guarded so that a zero or negative
            -- duration yields NULL rather than raising or producing infinity;
            -- rule V01/V02 flag those rows separately.
            date_diff('second', pickup_ts, dropoff_ts) / 60.0 AS duration_min,
            CASE
                WHEN date_diff('second', pickup_ts, dropoff_ts) > 0
                THEN trip_distance_mi / (date_diff('second', pickup_ts, dropoff_ts) / 3600.0)
            END AS speed_mph,
            date_trunc('hour', pickup_ts) AS pickup_hour_ts
        FROM unioned
    """)

    total = con.sql("SELECT count(*) FROM stg_trips").fetchone()[0]
    expected = sum(counts.values())
    if total != expected:
        raise TransformError(
            f"Row count changed during normalisation: {total} != {expected}"
        )
    log.info(
        "    [transform] %s: %s rows staged (%s)",
        month, f"{total:,}",
        ", ".join(f"{k} {v:,}" for k, v in counts.items()),
    )
    counts["total"] = total
    return counts


def build_fct_trip(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    month: str,
    summary: RunSummary,
) -> dict[str, Any]:
    """Join the validated trips to zone and weather context; check the joins."""
    log = get_logger()
    speed_cut = cfg.thresholds["congested_speed_mph"]

    con.execute("DROP TABLE IF EXISTS fct_trip")
    con.execute(f"""
        CREATE TABLE fct_trip AS
        SELECT
            t.*,
            '{month}'          AS month,
            pu.borough         AS pu_borough,
            pu.zone_name       AS pu_zone,
            pu.is_airport      AS pu_is_airport,
            dz.borough         AS do_borough,
            dz.zone_name       AS do_zone,
            dz.is_airport      AS do_is_airport,
            (pu.is_airport OR dz.is_airport) AS is_airport_trip,
            w.precip_mm,
            w.snow_cm,
            w.temp_c,
            COALESCE(w.is_wet, FALSE) AS is_wet_hour,
            hour(t.pickup_ts)         AS pickup_hour,
            dayofweek(t.pickup_ts)    AS pickup_dow,
            dayofweek(t.pickup_ts) IN (0, 6) AS is_weekend,
            -- The KPI outcome. Only meaningful where the trip is valid for
            -- core metrics; NULL elsewhere so it cannot be averaged in.
            CASE WHEN t.is_valid_core THEN t.speed_mph < {speed_cut} END AS is_congested
        FROM stg_trips_flagged t
        LEFT JOIN dim_zone pu ON t.pu_location_id = pu.zone_id
        LEFT JOIN dim_zone dz ON t.do_location_id = dz.zone_id
        LEFT JOIN fct_weather_hour w ON t.pickup_hour_ts = w.hour_ts
    """)

    staged = con.sql("SELECT count(*) FROM stg_trips_flagged").fetchone()[0]
    joined = con.sql("SELECT count(*) FROM fct_trip").fetchone()[0]

    # A LEFT JOIN that changes the row count means a duplicated dimension key -
    # the classic silent fan-out bug. Fail loudly.
    ok = staged == joined
    summary.record_check(
        "C7_join_no_fanout", ok, month, staged=staged, joined=joined
    )
    if not ok:
        raise TransformError(
            f"Join fan-out: {staged:,} staged rows became {joined:,} after joins"
        )

    # Join coverage: how many trips actually found their dimension rows.
    cov = con.sql(f"""
        SELECT
            100.0 * sum(CASE WHEN pu_borough IS NOT NULL THEN 1 ELSE 0 END) / count(*) AS zone_pct,
            100.0 * sum(CASE WHEN temp_c IS NOT NULL THEN 1 ELSE 0 END) / count(*)     AS weather_pct
        FROM fct_trip
        WHERE is_valid_core
    """).fetchone()
    zone_pct, weather_pct = float(cov[0]), float(cov[1])
    limit = cfg.checks["min_join_match_pct"]

    for name, pct in (("zone", zone_pct), ("weather", weather_pct)):
        passed = pct >= limit
        summary.record_check(
            f"C8_{name}_join_coverage", passed, month,
            pct=round(pct, 4), limit=limit,
        )
        if not passed:
            raise TransformError(
                f"{name} join covered only {pct:.3f}% of valid trips "
                f"(limit {limit}%) for {month}"
            )

    log.info(
        "    [transform] %s: fct_trip built, zone join %.3f%%, weather join %.3f%%",
        month, zone_pct, weather_pct,
    )

    _write_partition(
        con, "SELECT * FROM fct_trip", cfg.staging_dir / "fct_trip", month
    )
    return {
        "rows": joined,
        "zone_join_pct": zone_pct,
        "weather_join_pct": weather_pct,
    }
