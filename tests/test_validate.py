"""Validation rules must fire on exactly the rows they are meant to catch.

Each test builds a small synthetic staging table containing one deliberately
bad row plus a body of good rows, runs the real validation code, and asserts
both the flag and the resulting population membership.

The good rows are not padding for its own sake: the abort limits in
pipeline.yaml are *production* thresholds (a rule firing on >5-10% of trips
means the feed is broken). A fixture of one bad row in three would breach
them and mask what is being tested. Diluting to a realistic rate keeps the
production code path - limits included - exercised as it runs for real.

Using `apply_validation` itself, rather than re-implementing the predicates,
means these tests fail if a rule's SQL drifts from its documented intent.
"""

from __future__ import annotations

import duckdb
import pytest

from pipeline.config import load_config
from pipeline.logging_utils import RunSummary, setup_logging
from pipeline.validate import ValidationError, apply_validation, build_rules

MONTH = "2024-01"
PAD = 999  # good rows accompanying each bad one -> 0.1% violation rate

# A row that violates nothing: 10 miles in 30 minutes = 20 mph.
GOOD = {
    "fleet": "yellow",
    "pickup_ts": "2024-01-15 10:00:00",
    "dropoff_ts": "2024-01-15 10:30:00",
    "trip_distance_mi": 10.0,
    "pu_location_id": 100,
    "do_location_id": 150,
    "total_amount": 40.0,
    "passenger_count": 2,
}


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def summary(tmp_path):
    setup_logging(tmp_path, "test", verbose=False)
    return RunSummary("test", tmp_path)


def bad(**overrides) -> dict:
    return {**GOOD, **overrides}


def make_con(rows: list[dict], pad: int = PAD) -> duckdb.DuckDBPyConnection:
    """Build an in-memory stg_trips shaped like the real one.

    `trip_id` marks the rows under test (1) apart from the diluting good
    rows (0). Validation passes it through untouched via SELECT *.
    """
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE stg_trips_base (
            trip_id INTEGER, fleet VARCHAR, pickup_ts TIMESTAMP,
            dropoff_ts TIMESTAMP, trip_distance_mi DOUBLE,
            pu_location_id INTEGER, do_location_id INTEGER,
            total_amount DOUBLE, passenger_count BIGINT
        )
    """)
    payload = [(1, *(r[k] for k in (
        "fleet", "pickup_ts", "dropoff_ts", "trip_distance_mi",
        "pu_location_id", "do_location_id", "total_amount", "passenger_count",
    ))) for r in rows]
    payload += [(0, *(GOOD[k] for k in (
        "fleet", "pickup_ts", "dropoff_ts", "trip_distance_mi",
        "pu_location_id", "do_location_id", "total_amount", "passenger_count",
    ))) for _ in range(pad)]
    con.executemany(
        "INSERT INTO stg_trips_base VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", payload
    )

    # Derived measures normally added by build_stg_trips.
    con.execute("""
        CREATE TABLE stg_trips AS
        SELECT *,
            date_diff('second', pickup_ts, dropoff_ts) / 60.0 AS duration_min,
            CASE WHEN date_diff('second', pickup_ts, dropoff_ts) > 0
                 THEN trip_distance_mi / (date_diff('second', pickup_ts, dropoff_ts) / 3600.0)
            END AS speed_mph,
            date_trunc('hour', pickup_ts) AS pickup_hour_ts
        FROM stg_trips_base
    """)
    return con


def run(rows, cfg, summary, pad: int = PAD):
    con = make_con(rows, pad=pad)
    apply_validation(con, cfg, MONTH, summary)
    return con


def under_test(con, columns: str):
    """Fetch the marked row(s) only, ignoring the diluting good rows."""
    return con.sql(
        f"SELECT {columns} FROM stg_trips_flagged WHERE trip_id = 1"
    ).fetchone()


# ------------------------------------------------------------------ rules


def test_good_row_passes_every_rule(cfg, summary):
    con = run([GOOD], cfg, summary)
    assert under_test(con, "is_valid_core, is_valid_zone, is_valid_fare") == (
        True, True, True
    )


@pytest.mark.parametrize(
    "column,row",
    [
        # V01: dropoff before pickup
        ("v_v01", bad(dropoff_ts="2024-01-15 09:30:00")),
        # V02: 20-hour trip (meter left running)
        ("v_v02", bad(dropoff_ts="2024-01-16 06:00:00")),
        # V03: zero distance
        ("v_v03", bad(trip_distance_mi=0.0)),
        # V03: absurd distance
        ("v_v03", bad(trip_distance_mi=500.0)),
        # V04: 10 miles in 1 minute = 600 mph
        ("v_v04", bad(dropoff_ts="2024-01-15 10:01:00")),
        # V05: trip belongs to a different month
        ("v_v05", bad(pickup_ts="2023-12-15 10:00:00",
                      dropoff_ts="2023-12-15 10:30:00")),
    ],
)
def test_core_exclusion_rules(column, row, cfg, summary):
    """Each core rule fires on its own bad row and removes it from the KPI."""
    con = run([row], cfg, summary)
    flagged, valid = under_test(con, f"{column}, is_valid_core")
    assert flagged is True, f"{column} should have fired"
    assert valid is False, f"{column} should exclude the row from core metrics"


def test_unknown_zone_excluded_from_zone_metrics_only(cfg, summary):
    """V06 must not remove the trip from citywide metrics - only zone ones."""
    con = run([bad(pu_location_id=264)], cfg, summary)
    v06, core, zone = under_test(con, "v_v06, is_valid_core, is_valid_zone")
    assert v06 is True
    assert core is True, "an unknown zone does not invalidate the trip itself"
    assert zone is False, "but it cannot be attributed to a borough"


def test_negative_total_excluded_from_fare_metrics_only(cfg, summary):
    """V07: a refund is a real trip; it must not distort fare averages."""
    con = run([bad(total_amount=-25.0)], cfg, summary)
    v07, core, fare = under_test(con, "v_v07, is_valid_core, is_valid_fare")
    assert v07 is True
    assert core is True, "a refunded trip still happened and still has a speed"
    assert fare is False


def test_missing_passenger_count_is_flag_only(cfg, summary):
    """V08 must never remove a trip - the field feeds no headline metric."""
    con = run([bad(passenger_count=0)], cfg, summary)
    v08, core, zone, fare = under_test(
        con, "v_v08, is_valid_core, is_valid_zone, is_valid_fare"
    )
    assert v08 is True
    assert (core, zone, fare) == (True, True, True)


# -------------------------------------------------------------- behaviour


def test_no_rows_are_dropped_by_validation(cfg, summary):
    """The core guarantee: validation flags, it never deletes."""
    rows = [GOOD, bad(trip_distance_mi=0.0), bad(dropoff_ts="2024-01-15 09:00:00"),
            bad(pu_location_id=265), bad(total_amount=-5.0)]
    con = run(rows, cfg, summary)
    total = con.sql("SELECT count(*) FROM stg_trips_flagged").fetchone()[0]
    assert total == len(rows) + PAD


def test_population_reconciles(cfg, summary):
    """valid + excluded must always equal the total - no trips vanish."""
    rows = [GOOD, bad(trip_distance_mi=0.0), bad(dropoff_ts="2024-01-15 09:00:00")]
    con = run(rows, cfg, summary)
    total, valid, excluded = con.sql("""
        SELECT count(*),
               sum(CASE WHEN is_valid_core THEN 1 ELSE 0 END),
               sum(CASE WHEN NOT is_valid_core THEN 1 ELSE 0 END)
        FROM stg_trips_flagged
    """).fetchone()
    assert valid + excluded == total


def test_abort_limit_breach_raises(cfg, summary):
    """If quality collapses, the pipeline must refuse to publish.

    No dilution here: 100% of rows violate V01, far above its 5% limit.
    """
    rows = [bad(dropoff_ts="2024-01-15 09:00:00") for _ in range(10)]
    with pytest.raises(ValidationError, match="V01"):
        run(rows, cfg, summary, pad=0)


def test_rates_below_limits_do_not_raise(cfg, summary):
    """The complement: ordinary noise must not stop the pipeline."""
    run([bad(trip_distance_mi=0.0)], cfg, summary)  # 0.1%, well under 10%


def test_every_rule_has_an_abort_limit(cfg):
    """A rule with no configured limit could degrade unnoticed."""
    limits = cfg.checks["rule_abort_limits_pct"]
    for rule in build_rules(cfg):
        assert rule.id in limits, f"{rule.id} has no abort limit in pipeline.yaml"


def test_every_rule_documents_its_rationale(cfg):
    """Rules are judgement calls; an undocumented one is not defensible."""
    for rule in build_rules(cfg):
        assert len(rule.rationale) > 40, f"{rule.id} lacks a real rationale"
        assert rule.action in {
            "exclude_core", "exclude_zone", "exclude_fare", "flag_only"
        }
