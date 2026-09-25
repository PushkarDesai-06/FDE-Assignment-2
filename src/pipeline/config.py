"""Configuration loading and path helpers.

Everything the pipeline needs to know about *where* things live and *what*
thresholds apply is resolved here, so no other module hard-codes a path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Repo root = two levels up from src/pipeline/config.py
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "pipeline.yaml"


@dataclass(frozen=True)
class Config:
    """Parsed pipeline configuration with absolute paths."""

    raw: dict[str, Any]

    # --- sections ---
    @property
    def months(self) -> list[str]:
        return list(self.raw["run"]["months"])

    @property
    def fleets(self) -> list[str]:
        return list(self.raw["run"]["fleets"])

    @property
    def sources(self) -> dict[str, Any]:
        return self.raw["sources"]

    @property
    def thresholds(self) -> dict[str, Any]:
        return self.raw["thresholds"]

    @property
    def checks(self) -> dict[str, Any]:
        return self.raw["checks"]

    @property
    def retrieval(self) -> dict[str, Any]:
        return self.raw["retrieval"]

    # --- paths (always absolute) ---
    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def raw_dir(self) -> Path:
        return self.path("raw")

    @property
    def marts_dir(self) -> Path:
        return self.path("marts")

    @property
    def staging_dir(self) -> Path:
        return self.path("staging")

    @property
    def warehouse(self) -> Path:
        return self.path("warehouse")

    @property
    def logs_dir(self) -> Path:
        return self.path("logs")

    @property
    def manifest_path(self) -> Path:
        return self.raw_dir / "manifest.json"

    def trip_path(self, fleet: str, month: str) -> Path:
        return self.raw_dir / "trips" / fleet / f"{month}.parquet"

    def trip_url(self, fleet: str, month: str) -> str:
        return self.sources["trips"]["url_template"].format(fleet=fleet, month=month)

    def weather_path(self, month: str) -> Path:
        return self.raw_dir / "weather" / f"{month}.json"

    @property
    def zone_lookup_path(self) -> Path:
        return self.raw_dir / "reference" / "taxi_zone_lookup.csv"

    def ensure_dirs(self) -> None:
        for d in (self.raw_dir, self.marts_dir, self.staging_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
        (self.raw_dir / "reference").mkdir(parents=True, exist_ok=True)
        (self.raw_dir / "weather").mkdir(parents=True, exist_ok=True)
        for fleet in self.fleets:
            (self.raw_dir / "trips" / fleet).mkdir(parents=True, exist_ok=True)
        self.warehouse.parent.mkdir(parents=True, exist_ok=True)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load pipeline.yaml (or an override path, used by tests)."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")
    with cfg_path.open() as fh:
        return Config(raw=yaml.safe_load(fh))
