"""Stage 2 - Completeness.

"Did we get all of the data, and is it the data we expected?"

Retrieval is not finished when a file lands on disk; it is finished when we
can show the file is whole and shaped as expected. These checks run before
any analysis, so a bad extract fails the run instead of quietly shifting a
metric.

Checks performed per fleet-month:
  C1  file exists and is non-empty
  C2  the schema contract holds (see schema.py)
  C3  the file is readable as Parquet and has > 0 rows
  C4  >= 99% of pickup timestamps fall inside the month the file claims
  C5  row count is within tolerance of the previous month (structural break)
And per month:
  C6  weather covers every hour of the month (24 x days)
"""

from __future__ import annotations

import json
from calendar import monthrange
from pathlib import Path
from typing import Any

import duckdb

from .config import Config
from .logging_utils import RunSummary, get_logger
from .schema import TIMESTAMP_COLUMNS, check_schema


class CompletenessError(RuntimeError):
    """Raised when retrieved data fails a completeness check."""


def parquet_columns(con: duckdb.DuckDBPyConnection, path: Path) -> list[str]:
    rows = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    return [r[0] for r in rows]


def check_trip_file(
    cfg: Config,
    con: duckdb.DuckDBPyConnection,
    fleet: str,
    month: str,
    summary: RunSummary,
    previous_count: int | None = None,
) -> dict[str, Any]:
    """Run C1-C5 for one fleet-month. Raises CompletenessError on failure."""
    log = get_logger()
    path = cfg.trip_path(fleet, month)
    tag = f"{fleet}/{month}"

    # C1 - file present and non-empty
    ok = path.exists() and path.stat().st_size > 0
    summary.record_check("C1_file_present", ok, month, fleet=fleet, path=str(path))
    if not ok:
        raise CompletenessError(f"C1 failed: missing or empty extract for {tag}")

    # C2 - schema contract
    cols = parquet_columns(con, path)
    missing = check_schema(fleet, cols)
    summary.record_check(
        "C2_schema_contract", not missing, month, fleet=fleet, missing=missing
    )
    if missing:
        raise CompletenessError(f"C2 failed: {tag} missing columns {missing}")

    # C3 - readable, non-zero rows
    n_rows = con.sql(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
    summary.record_check("C3_non_zero_rows", n_rows > 0, month, fleet=fleet, rows=n_rows)
    if n_rows == 0:
        raise CompletenessError(f"C3 failed: {tag} has zero rows")

    # C4 - the file's contents match the month it claims to cover
    pickup_col, _ = TIMESTAMP_COLUMNS[fleet]
    year, mon = (int(x) for x in month.split("-"))
    last_day = monthrange(year, mon)[1]
    start, end = f"{month}-01", f"{month}-{last_day:02d} 23:59:59"
    inside = con.sql(
        f"""SELECT count(*) FROM read_parquet('{path}')
            WHERE "{pickup_col}" BETWEEN TIMESTAMP '{start}' AND TIMESTAMP '{end}'"""
    ).fetchone()[0]
    pct_inside = 100.0 * inside / n_rows
    limit = cfg.checks["min_pct_inside_month"]
    ok = pct_inside >= limit
    summary.record_check(
        "C4_pickups_inside_month", ok, month, fleet=fleet,
        pct_inside=round(pct_inside, 4), limit=limit,
    )
    if not ok:
        raise CompletenessError(
            f"C4 failed: only {pct_inside:.2f}% of {tag} pickups fall inside {month} "
            f"(limit {limit}%) - the extract may be mislabelled"
        )

    # C5 - volume continuity against the previous month
    if previous_count:
        delta_pct = abs(n_rows - previous_count) / previous_count * 100.0
        tol = cfg.checks["row_count_tolerance_pct"]
        ok = delta_pct <= tol
        summary.record_check(
            "C5_volume_continuity", ok, month, fleet=fleet,
            rows=n_rows, previous=previous_count,
            delta_pct=round(delta_pct, 2), tolerance_pct=tol,
        )
        if not ok:
            raise CompletenessError(
                f"C5 failed: {tag} row count {n_rows:,} differs from previous month "
                f"{previous_count:,} by {delta_pct:.1f}% (tolerance {tol}%)"
            )

    log.info(
        "    [check] %s: %s rows, %.3f%% inside month, schema OK",
        tag, f"{n_rows:,}", pct_inside,
    )
    return {"rows": n_rows, "pct_inside_month": pct_inside, "columns": cols}


def check_weather(cfg: Config, month: str, summary: RunSummary) -> dict[str, Any]:
    """C6 - the weather series covers every hour of the month."""
    log = get_logger()
    path = cfg.weather_path(month)
    if not path.exists():
        summary.record_check("C6_weather_complete", False, month, reason="missing file")
        raise CompletenessError(f"C6 failed: no weather file for {month}")

    payload = json.loads(path.read_text())
    hours = payload.get("hourly", {}).get("time", [])
    year, mon = (int(x) for x in month.split("-"))
    expected = monthrange(year, mon)[1] * 24
    ok = len(hours) == expected
    summary.record_check(
        "C6_weather_complete", ok, month, hours=len(hours), expected=expected
    )
    if not ok:
        raise CompletenessError(
            f"C6 failed: weather for {month} has {len(hours)} hourly rows, "
            f"expected {expected}"
        )
    log.info("    [check] weather %s: %d/%d hours present", month, len(hours), expected)
    return {"hours": len(hours)}
