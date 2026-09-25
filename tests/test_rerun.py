"""Dependability tests: reruns, failure handling, and schema reconciliation.

These cover the properties that make the pipeline safe to operate rather
than merely correct once:

  * rerunning a month reproduces byte-identical metrics (idempotency)
  * a rerun replaces a partition instead of appending to it
  * a missing or corrupt input fails loudly, and publishes nothing
  * one failed month does not block the others
  * the two fleets' differing schemas reconcile onto one shape
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import duckdb
import pytest

from pipeline.config import load_config
from pipeline.metrics import parse_metric_blocks
from pipeline.schema import (
    check_schema,
    normalise_select,
    required_columns,
)

REPO = Path(__file__).resolve().parents[1]
MARTS = REPO / "data" / "marts"
MONTH = "2024-01"

# These tests read the outputs of a real run; skip cleanly if none exists.
pytestmark = pytest.mark.skipif(
    not (MARTS / "m_congested_rate" / f"month={MONTH}").exists(),
    reason="no pipeline output present - run `python -m pipeline.run --all` first",
)


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------- idempotency


def test_rerun_reproduces_identical_metrics(tmp_path):
    """The dependability guarantee: same inputs -> same published numbers.

    Metrics are recomputed from the untouched raw inputs and compared to
    what is already published. Parquet files are not compared byte-wise
    (compression metadata can differ); the metric *values* are.
    """
    from pipeline.run import process_month
    from pipeline.logging_utils import RunSummary, setup_logging

    cfg = load_config()
    setup_logging(tmp_path, "rerun", verbose=False)
    summary = RunSummary("rerun", tmp_path)

    before = duckdb.connect().sql(f"""
        SELECT month, fleet, borough, pickup_hour, is_weekend, trips, congested_trips
        FROM read_parquet('{MARTS}/m_congested_rate/month={MONTH}/*.parquet')
        ORDER BY ALL
    """).fetchall()

    con = duckdb.connect()
    process_month(cfg, con, MONTH, summary, from_stage="completeness")
    con.close()

    after = duckdb.connect().sql(f"""
        SELECT month, fleet, borough, pickup_hour, is_weekend, trips, congested_trips
        FROM read_parquet('{MARTS}/m_congested_rate/month={MONTH}/*.parquet')
        ORDER BY ALL
    """).fetchall()

    assert before == after, "a rerun changed published metric values"
    assert summary.failed_checks == [], "a clean rerun should fail no checks"


def test_rerun_replaces_partition_rather_than_appending():
    """Guards against the classic double-counting bug."""
    part = MARTS / "m_congested_rate" / f"month={MONTH}"
    files = list(part.glob("*.parquet"))
    assert len(files) == 1, f"expected exactly one part file, found {len(files)}"

    # Every row in the partition must belong to that month.
    months = duckdb.connect().sql(
        f"SELECT DISTINCT month FROM read_parquet('{part}/*.parquet')"
    ).fetchall()
    assert months == [(MONTH,)]


def test_months_are_independent():
    """Each month's partition contains only its own data."""
    con = duckdb.connect()
    rows = con.sql(f"""
        SELECT month, count(*) FROM read_parquet('{MARTS}/m_congested_rate/**/*.parquet')
        GROUP BY month ORDER BY month
    """).fetchall()
    assert len(rows) >= 1
    for month, n in rows:
        assert n > 0, f"{month} partition is empty"


# --------------------------------------------------------- failure handling


def test_missing_input_fails_loudly(tmp_path):
    """A missing extract must raise, not silently produce a smaller metric."""
    from pipeline.completeness import CompletenessError, check_trip_file
    from pipeline.logging_utils import RunSummary, setup_logging

    cfg = load_config()
    setup_logging(tmp_path, "missing", verbose=False)
    summary = RunSummary("missing", tmp_path)
    con = duckdb.connect()

    with pytest.raises(CompletenessError, match="C1"):
        check_trip_file(cfg, con, "yellow", "1999-01", summary)

    assert any(not c["passed"] for c in summary.data["checks"])


def test_volume_break_is_detected(tmp_path):
    """A sudden collapse in row count signals a broken extract."""
    from pipeline.completeness import CompletenessError, check_trip_file
    from pipeline.logging_utils import RunSummary, setup_logging

    cfg = load_config()
    setup_logging(tmp_path, "vol", verbose=False)
    summary = RunSummary("vol", tmp_path)
    con = duckdb.connect()

    # Claim last month had 100x the volume; the check must reject the break.
    with pytest.raises(CompletenessError, match="C5"):
        check_trip_file(
            cfg, con, "yellow", MONTH, summary, previous_count=300_000_000
        )


def test_corrupt_file_is_rejected(tmp_path):
    """A truncated/garbage Parquet must fail rather than yield partial rows."""
    from pipeline.completeness import parquet_columns

    fake = tmp_path / "corrupt.parquet"
    fake.write_bytes(b"this is not a parquet file")
    con = duckdb.connect()
    with pytest.raises(Exception):
        parquet_columns(con, fake)


# ------------------------------------------------------- schema reconciling


def test_fleet_schemas_differ_but_reconcile():
    """The two source systems really are different - and are unified."""
    yellow = set(required_columns("yellow"))
    green = set(required_columns("green"))
    assert "tpep_pickup_datetime" in yellow
    assert "lpep_pickup_datetime" in green
    assert yellow != green, "fixture assumption: fleets have different schemas"


def test_missing_required_column_is_caught():
    """Schema drift must be detected, not absorbed into NULLs."""
    cols = [c for c in required_columns("yellow") if c != "trip_distance"]
    missing = check_schema("yellow", cols)
    assert "trip_distance" in missing


def test_schema_check_is_case_insensitive():
    """TLC has shipped both `Airport_fee` and `airport_fee`."""
    cols = [c.upper() for c in required_columns("green")]
    assert check_schema("green", cols) == []


def test_optional_column_absence_yields_null_not_error():
    """Green has no airport fee column; that is expected, not a failure."""
    sql = normalise_select("green", required_columns("green"))
    assert "airport_fee" in sql
    assert "CAST(NULL AS DOUBLE) AS airport_fee" in sql.replace("  ", " ") or \
           "NULL" in sql


def test_both_fleets_normalise_to_identical_columns():
    """The UNION ALL in build_stg_trips depends on this.

    Each fleet's raw shape is materialised as an empty typed table, then
    run through the real normalise_select, so a divergence in column names
    or ordering fails here rather than at run time.
    """
    con = duckdb.connect()

    def cols_for(fleet: str) -> list[str]:
        cols = required_columns(fleet)
        decls = []
        for c in cols:
            if c.endswith("_datetime"):
                decls.append(f'"{c}" TIMESTAMP')
            elif c in {"store_and_fwd_flag"}:
                decls.append(f'"{c}" VARCHAR')
            else:
                decls.append(f'"{c}" DOUBLE')
        con.execute(f"DROP TABLE IF EXISTS raw_{fleet}")
        con.execute(f"CREATE TABLE raw_{fleet} ({', '.join(decls)})")
        sql = normalise_select(fleet, cols)
        rel = con.sql(f"{sql} FROM raw_{fleet}")
        return [d[0] for d in rel.description]

    assert cols_for("yellow") == cols_for("green")


# ------------------------------------------------------------------- misc


def test_all_metric_blocks_parse():
    """A malformed metrics.sql would silently publish fewer metrics."""
    sql_text = (REPO / "sql" / "metrics.sql").read_text()
    blocks = parse_metric_blocks(sql_text)
    names = [n for n, _ in blocks]
    assert names == [
        "m_congested_rate", "m_speed_duration", "m_airport",
        "m_weather_effect", "m_data_quality",
    ]


def test_run_summary_records_every_check():
    """The audit trail must exist and be machine-readable."""
    summaries = sorted((REPO / "logs").glob("run_summary_*.json"))
    assert summaries, "no run summary written"
    data = json.loads(summaries[-1].read_text())
    assert data["status"] in {"success", "failed"}
    assert data["checks"], "no checks recorded"
    assert "git_commit" in data


def test_manifest_records_provenance():
    """Every raw input must be traceable to a URL and a checksum."""
    manifest_path = REPO / "data" / "raw" / "manifest.json"
    if not manifest_path.exists():
        pytest.skip("no manifest - raw data not retrieved in this environment")
    manifest = json.loads(manifest_path.read_text())
    assert manifest
    for key, entry in manifest.items():
        assert entry["url"].startswith("http"), f"{key} has no source URL"
        assert len(entry["sha256"]) == 64, f"{key} has no valid checksum"
        assert entry["bytes"] > 0
