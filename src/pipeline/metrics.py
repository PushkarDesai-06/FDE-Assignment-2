"""Stage 5 - Metrics.

Executes the metric definitions in sql/metrics.sql against the month's
fct_trip and writes each to its own partitioned mart under data/marts/.

Metric SQL lives in a .sql file rather than in Python strings so the
business definitions can be read and reviewed without reading the
orchestration around them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

from .config import Config, REPO_ROOT
from .logging_utils import RunSummary, get_logger
from .transform import _write_partition
from .validate import build_rules

METRICS_SQL = REPO_ROOT / "sql" / "metrics.sql"


class MetricsError(RuntimeError):
    """Raised when a metric fails its reconciliation check."""


def parse_metric_blocks(sql_text: str) -> list[tuple[str, str]]:
    """Split metrics.sql into (name, statement) pairs using `-- name:` markers."""
    blocks: list[tuple[str, str]] = []
    current_name: str | None = None
    buf: list[str] = []

    for line in sql_text.splitlines():
        m = re.match(r"\s*--\s*name:\s*(\w+)", line)
        if m:
            if current_name and buf:
                blocks.append((current_name, "\n".join(buf).strip()))
            current_name, buf = m.group(1), []
            continue
        if current_name is not None:
            buf.append(line)

    if current_name and buf:
        blocks.append((current_name, "\n".join(buf).strip()))

    # Trim each statement at its terminating semicolon.
    cleaned = []
    for name, stmt in blocks:
        stmt = stmt.split(";")[0].strip()
        if stmt:
            cleaned.append((name, stmt))
    return cleaned


def _register_rule_actions(con: duckdb.DuckDBPyConnection, cfg: Config) -> None:
    """Expose rule metadata to SQL so m_data_quality can label each rule."""
    con.execute("DROP TABLE IF EXISTS rule_actions")
    con.execute("CREATE TABLE rule_actions (id VARCHAR, action VARCHAR)")
    con.executemany(
        "INSERT INTO rule_actions VALUES (?, ?)",
        [(r.id, r.action) for r in build_rules(cfg)],
    )


def compute_metrics(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    month: str,
    summary: RunSummary,
) -> dict[str, Any]:
    """Compute and persist every metric for one month."""
    log = get_logger()
    _register_rule_actions(con, cfg)

    sql_text = METRICS_SQL.read_text()
    blocks = parse_metric_blocks(sql_text)
    if not blocks:
        raise MetricsError(f"No metric blocks parsed from {METRICS_SQL}")

    results: dict[str, Any] = {}
    for name, stmt in blocks:
        statement = stmt.replace("{month}", month)
        rel = con.sql(statement)
        n = len(rel.fetchall())
        _write_partition(con, statement, cfg.marts_dir / name, month)
        results[name] = n
        log.info("    [metrics] %s: %d rows", name, n)

    _reconcile(con, cfg, month, summary, results)
    return results


def _reconcile(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    month: str,
    summary: RunSummary,
    results: dict[str, Any],
) -> None:
    """Prove the KPI's arithmetic against the source population.

    Identity checked: every staged trip is either counted as valid for core
    metrics or attributable to at least one exclusion rule. If this fails,
    trips are disappearing somewhere between raw and mart.
    """
    log = get_logger()
    row = con.sql("""
        SELECT
            count(*)                                            AS total,
            sum(CASE WHEN is_valid_core THEN 1 ELSE 0 END)      AS valid_core,
            sum(CASE WHEN NOT is_valid_core THEN 1 ELSE 0 END)  AS excluded_core
        FROM fct_trip
    """).fetchone()
    total, valid, excluded = (int(x) for x in row)

    ok = (valid + excluded) == total
    summary.record_check(
        "C9_population_reconciles", ok, month,
        total=total, valid_core=valid, excluded_core=excluded,
    )
    if not ok:
        raise MetricsError(
            f"Population does not reconcile for {month}: "
            f"{valid} valid + {excluded} excluded != {total} total"
        )

    # The KPI computed two ways must agree: once from the trip table, once by
    # re-aggregating the published mart. This catches a bad GROUP BY.
    kpi_direct = con.sql("""
        SELECT round(100.0 * sum(CASE WHEN is_congested THEN 1 ELSE 0 END)
                     / count(*), 4)
        FROM fct_trip WHERE is_valid_core AND is_valid_zone
    """).fetchone()[0]

    mart = cfg.marts_dir / "m_congested_rate" / f"month={month}" / "part.parquet"
    kpi_mart = con.sql(f"""
        SELECT round(100.0 * sum(congested_trips) / sum(trips), 4)
        FROM read_parquet('{mart}')
    """).fetchone()[0]

    agree = kpi_direct is not None and abs(float(kpi_direct) - float(kpi_mart)) < 0.01
    summary.record_check(
        "C10_kpi_agrees_with_mart", agree, month,
        kpi_direct=float(kpi_direct), kpi_mart=float(kpi_mart),
    )
    if not agree:
        raise MetricsError(
            f"KPI mismatch for {month}: fct_trip says {kpi_direct}%, "
            f"published mart says {kpi_mart}%"
        )

    summary.record_month(month, "kpi_congested_rate_pct", float(kpi_direct))
    summary.record_month(month, "trips_total", total)
    summary.record_month(month, "trips_valid_core", valid)
    log.info(
        "    [metrics] %s KPI: %.2f%% of trips congested (<%.0f mph), "
        "on %s valid trips",
        month, float(kpi_direct), cfg.thresholds["congested_speed_mph"], f"{valid:,}",
    )
