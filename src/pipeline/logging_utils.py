"""Structured logging and the per-run summary record.

Two things are produced by every run:
  * a human-readable log at logs/run_<ts>.log (also echoed to the console)
  * a machine-readable logs/run_summary_<ts>.json holding row counts,
    check outcomes and timings, so a run can be audited after the fact.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER_NAME = "pipeline"


def setup_logging(logs_dir: Path, run_id: str, verbose: bool = True) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.FileHandler(logs_dir / f"run_{run_id}.log")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def git_commit() -> str:
    """Record which version of the code produced an output. Best effort."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


class RunSummary:
    """Accumulates what happened during a run and writes it as JSON."""

    def __init__(self, run_id: str, logs_dir: Path) -> None:
        self.run_id = run_id
        self.logs_dir = logs_dir
        self.started = time.time()
        self.data: dict[str, Any] = {
            "run_id": run_id,
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "git_commit": git_commit(),
            "months": {},
            "checks": [],
            "stages": {},
            "status": "running",
        }

    def record_stage(self, stage: str, month: str | None, **fields: Any) -> None:
        self.data["stages"].setdefault(stage, []).append({"month": month, **fields})

    def record_month(self, month: str, key: str, value: Any) -> None:
        self.data["months"].setdefault(month, {})[key] = value

    def record_check(
        self, name: str, passed: bool, month: str | None = None, **detail: Any
    ) -> None:
        self.data["checks"].append(
            {"check": name, "month": month, "passed": bool(passed), **detail}
        )

    @property
    def failed_checks(self) -> list[dict[str, Any]]:
        return [c for c in self.data["checks"] if not c["passed"]]

    def finish(self, status: str) -> Path:
        self.data["status"] = status
        self.data["duration_seconds"] = round(time.time() - self.started, 2)
        self.data["finished_utc"] = datetime.now(timezone.utc).isoformat()
        path = self.logs_dir / f"run_summary_{self.run_id}.json"
        path.write_text(json.dumps(self.data, indent=2, default=str))
        return path


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")
