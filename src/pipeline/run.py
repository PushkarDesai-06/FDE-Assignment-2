"""Pipeline entry point.

    python -m pipeline.run --month 2024-03
    python -m pipeline.run --all
    python -m pipeline.run --all --from-stage validate
    python -m pipeline.run --month 2024-03 --force-download

Stage order:  ingest -> completeness -> validate -> transform -> metrics

Failure behaviour:
  * Any failed check aborts the month it belongs to; no partial metrics are
    published for that month.
  * In --all, a failed month does not stop the remaining months; the run
    finishes and exits non-zero, so a single bad extract does not block
    five good ones.
  * Exit codes: 0 all months succeeded, 1 at least one month failed.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import duckdb

from .config import load_config, Config
from .logging_utils import RunSummary, get_logger, new_run_id, setup_logging
from .ingest import ingest_month
from .completeness import check_trip_file, check_weather
from .validate import apply_validation
from .transform import build_dim_zone, build_fct_trip, build_stg_trips, build_weather
from .metrics import compute_metrics

STAGES = ["ingest", "completeness", "validate", "transform", "metrics"]


def process_month(
    cfg: Config,
    con: duckdb.DuckDBPyConnection,
    month: str,
    summary: RunSummary,
    from_stage: str = "ingest",
    force_download: bool = False,
    prev_counts: dict[str, int] | None = None,
) -> dict[str, int]:
    """Run every stage for one month. Raises on the first failed check."""
    log = get_logger()
    log.info("=" * 68)
    log.info("MONTH %s", month)
    log.info("=" * 68)
    start_idx = STAGES.index(from_stage)
    counts: dict[str, int] = {}

    # --- 1. ingest -------------------------------------------------------
    if start_idx <= 0:
        ingest_month(cfg, month, force=force_download)
    else:
        log.info("  [ingest] skipped (--from-stage %s)", from_stage)

    # --- 2. completeness -------------------------------------------------
    if start_idx <= 1:
        for fleet in cfg.fleets:
            res = check_trip_file(
                cfg, con, fleet, month, summary,
                previous_count=(prev_counts or {}).get(fleet),
            )
            counts[fleet] = res["rows"]
            summary.record_month(month, f"raw_rows_{fleet}", res["rows"])
        check_weather(cfg, month, summary)

    # --- 3. model the sources -------------------------------------------
    build_dim_zone(con, cfg)
    build_weather(con, cfg, month)
    staged = build_stg_trips(con, cfg, month)
    for fleet in cfg.fleets:
        counts.setdefault(fleet, staged.get(fleet, 0))

    # --- 4. validate -----------------------------------------------------
    val = apply_validation(con, cfg, month, summary)
    summary.record_month(month, "staged_rows", val["total"])

    # --- 5. transform ----------------------------------------------------
    build_fct_trip(con, cfg, month, summary)

    # --- 6. metrics ------------------------------------------------------
    compute_metrics(con, cfg, month, summary)

    log.info("  MONTH %s COMPLETE", month)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pipeline.run",
        description="NYC TLC trip-efficiency metric pipeline",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--month", help="single month to process, e.g. 2024-03")
    group.add_argument("--all", action="store_true", help="process every configured month")
    parser.add_argument(
        "--from-stage", choices=STAGES, default="ingest",
        help="skip earlier stages (data must already be present)",
    )
    parser.add_argument(
        "--force-download", action="store_true",
        help="re-retrieve sources even if already present",
    )
    parser.add_argument("--config", default=None, help="path to pipeline.yaml")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    cfg.ensure_dirs()

    run_id = new_run_id()
    log = setup_logging(cfg.logs_dir, run_id)
    summary = RunSummary(run_id, cfg.logs_dir)

    months = cfg.months if args.all else [args.month]
    if not args.all and args.month not in cfg.months:
        log.warning(
            "%s is not in the configured month list; processing it anyway", args.month
        )

    log.info("Run %s starting | months=%s | from-stage=%s",
             run_id, ",".join(months), args.from_stage)

    con = duckdb.connect(str(cfg.warehouse))
    failed: list[str] = []
    prev_counts: dict[str, int] | None = None

    for month in months:
        try:
            prev_counts = process_month(
                cfg, con, month, summary,
                from_stage=args.from_stage,
                force_download=args.force_download,
                prev_counts=prev_counts,
            )
            summary.record_month(month, "status", "success")
        except Exception as exc:  # noqa: BLE001 - recorded, run continues
            failed.append(month)
            summary.record_month(month, "status", "failed")
            summary.record_month(month, "error", f"{type(exc).__name__}: {exc}")
            log.error("MONTH %s FAILED: %s: %s", month, type(exc).__name__, exc)
            log.debug("%s", traceback.format_exc())
            if not args.all:
                break

    con.close()

    status = "failed" if failed else "success"
    path = summary.finish(status)

    log.info("=" * 68)
    if failed:
        log.error("Run %s finished with failures: %s", run_id, ", ".join(failed))
    else:
        log.info("Run %s finished successfully (%d months)", run_id, len(months))
    for check in summary.failed_checks:
        log.error("  failed check: %s (%s)", check["check"], check.get("month"))
    log.info("Run summary: %s", path)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
