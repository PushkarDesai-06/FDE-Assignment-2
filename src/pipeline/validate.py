"""Stage 3 - Profiling and validation.

The governing principle: **flag, never silently drop**.

Each rule writes a boolean column onto the staging table rather than
deleting rows. Downstream metrics then choose which population they need
(`is_valid_core` for speed/duration metrics, `is_valid_fare` for money
metrics). Because nothing is deleted, every excluded trip remains
countable, and `m_data_quality` can report exactly what was removed and
why - which is what makes the headline KPI auditable.

Rules are declared as data, not code, so that docs/validation_rules.md,
the abort limits in pipeline.yaml and the unit tests all refer to the
same definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import duckdb

from .config import Config
from .logging_utils import RunSummary, get_logger


class ValidationError(RuntimeError):
    """Raised when a validation rule breaches its abort limit."""


@dataclass(frozen=True)
class Rule:
    """One validation rule.

    id:        stable identifier, used in config and docs
    predicate: SQL that is TRUE when the row VIOLATES the rule
    action:    exclude_core | exclude_fare | exclude_zone | flag_only
    rationale: why this matters to the business question
    """

    id: str
    predicate: str
    action: str
    rationale: str

    @property
    def column(self) -> str:
        return f"v_{self.id.split('_')[0].lower()}"  # V01_... -> v_v01


def build_rules(cfg: Config) -> list[Rule]:
    """Rules are built from config so thresholds stay in one place."""
    t = cfg.thresholds
    unknown = ", ".join(str(z) for z in cfg.raw["unknown_zone_ids"])
    return [
        Rule(
            "V01_dropoff_before_pickup",
            "dropoff_ts <= pickup_ts",
            "exclude_core",
            "A trip cannot end before it starts; duration would be negative "
            "and would corrupt any speed or duration metric.",
        ),
        Rule(
            "V02_duration_out_of_range",
            f"duration_min < {t['min_duration_min']} OR duration_min > {t['max_duration_min']}",
            "exclude_core",
            "Sub-minute trips are usually meter errors or immediate cancellations; "
            f"trips over {t['max_duration_min']:.0f} minutes are typically a meter "
            "left running, not a real journey.",
        ),
        Rule(
            "V03_distance_out_of_range",
            f"trip_distance_mi <= {t['min_distance_mi']} OR trip_distance_mi > {t['max_distance_mi']}",
            "exclude_core",
            "Zero distance means no journey took place; over "
            f"{t['max_distance_mi']:.0f} miles leaves the NYC service area and is "
            "almost always a GPS or meter fault.",
        ),
        Rule(
            "V04_implausible_speed",
            f"speed_mph > {t['max_speed_mph']}",
            "exclude_core",
            f"Above {t['max_speed_mph']:.0f} mph is not achievable on NYC streets; "
            "the distance or the timestamps must be wrong. Critically, these rows "
            "would bias the congestion KPI downward.",
        ),
        Rule(
            "V05_pickup_outside_month",
            "date_trunc('month', pickup_ts) <> DATE '{month}-01'",
            "exclude_core",
            "The monthly extract is the unit of reprocessing; a trip from another "
            "month would be double-counted when that month is also processed.",
        ),
        Rule(
            "V06_unknown_zone",
            f"pu_location_id IN ({unknown}) OR do_location_id IN ({unknown})",
            "exclude_zone",
            "Zones 264/265 mean 'unknown' or 'outside NYC'. The trip is real and "
            "still counts in citywide totals, but it cannot be attributed to a "
            "borough, so it is excluded from zone-level metrics only.",
        ),
        Rule(
            "V07_negative_total",
            "total_amount < 0",
            "exclude_fare",
            "Negative totals are refunds and chargebacks booked against the "
            "original trip. They are legitimate accounting records but would "
            "understate revenue metrics if averaged in.",
        ),
        Rule(
            "V08_missing_passenger_count",
            "passenger_count IS NULL OR passenger_count = 0",
            "flag_only",
            "Passenger count is frequently unreported by the vendor system. It is "
            "not used by any headline metric, so the trip is kept and merely "
            "flagged - dropping ~6% of trips for an unused field would be worse.",
        ),
    ]


def apply_validation(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    month: str,
    summary: RunSummary,
) -> dict[str, Any]:
    """Add validation flag columns to stg_trips and enforce abort limits.

    Expects a table `stg_trips` holding the normalised month, with
    duration_min and speed_mph already derived.
    """
    log = get_logger()
    rules = build_rules(cfg)

    flag_cols = []
    for rule in rules:
        predicate = rule.predicate.replace("{month}", month)
        flag_cols.append(f"({predicate}) AS {rule.column}")

    # Population flags express which metrics a row may legitimately feed.
    core_violations = " OR ".join(
        f"({r.predicate.replace('{month}', month)})"
        for r in rules if r.action == "exclude_core"
    )
    zone_violations = " OR ".join(
        f"({r.predicate.replace('{month}', month)})"
        for r in rules if r.action in ("exclude_core", "exclude_zone")
    )
    fare_violations = " OR ".join(
        f"({r.predicate.replace('{month}', month)})"
        for r in rules if r.action in ("exclude_core", "exclude_fare")
    )

    con.execute("DROP TABLE IF EXISTS stg_trips_flagged")
    con.execute(f"""
        CREATE TABLE stg_trips_flagged AS
        SELECT
            *,
            {', '.join(flag_cols)},
            NOT ({core_violations}) AS is_valid_core,
            NOT ({zone_violations}) AS is_valid_zone,
            NOT ({fare_violations}) AS is_valid_fare
        FROM stg_trips
    """)

    total = con.sql("SELECT count(*) FROM stg_trips_flagged").fetchone()[0]

    # Per-rule hit rates, split by fleet so a fleet-specific break is visible.
    select_parts = ", ".join(
        f"sum(CASE WHEN {r.column} THEN 1 ELSE 0 END) AS {r.column}" for r in rules
    )
    rows = con.sql(
        f"SELECT fleet, count(*) AS n, {select_parts} FROM stg_trips_flagged GROUP BY fleet"
    ).fetchall()
    cols = [d[0] for d in con.sql(
        f"SELECT fleet, count(*) AS n, {select_parts} FROM stg_trips_flagged GROUP BY fleet"
    ).description]

    limits = cfg.checks["rule_abort_limits_pct"]
    breaches: list[str] = []
    rule_stats: list[dict[str, Any]] = []

    for row in rows:
        rec = dict(zip(cols, row))
        fleet, n = rec["fleet"], rec["n"]
        for rule in rules:
            hits = rec[rule.column] or 0
            pct = 100.0 * hits / n if n else 0.0
            rule_stats.append({
                "month": month, "fleet": fleet, "rule": rule.id,
                "action": rule.action, "hits": hits,
                "pct": round(pct, 4), "population": n,
            })
            limit = limits.get(rule.id)
            passed = limit is None or pct <= limit
            summary.record_check(
                f"V_{rule.id}", passed, month,
                fleet=fleet, pct=round(pct, 4), limit=limit,
            )
            if not passed:
                breaches.append(
                    f"{rule.id} on {fleet}/{month}: {pct:.2f}% > limit {limit}%"
                )

    valid_core = con.sql(
        "SELECT count(*) FROM stg_trips_flagged WHERE is_valid_core"
    ).fetchone()[0]

    log.info(
        "    [validate] %s: %s rows, %s valid for core metrics (%.2f%% excluded)",
        month, f"{total:,}", f"{valid_core:,}",
        100.0 * (total - valid_core) / total if total else 0.0,
    )
    for rs in sorted(rule_stats, key=lambda r: -r["pct"])[:4]:
        log.debug("      %s %s: %.3f%%", rs["fleet"], rs["rule"], rs["pct"])

    if breaches:
        raise ValidationError(
            "Validation abort limits breached (data quality has degraded beyond "
            "what the pipeline will publish):\n  - " + "\n  - ".join(breaches)
        )

    # Reconciliation identity: nothing vanishes silently.
    assert con.sql(
        "SELECT count(*) FROM stg_trips_flagged"
    ).fetchone()[0] == total, "row count changed during validation"

    return {"total": total, "valid_core": valid_core, "rule_stats": rule_stats}
