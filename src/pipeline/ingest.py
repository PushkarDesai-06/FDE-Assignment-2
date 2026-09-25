"""Stage 1 - Retrieval.

Three retrieval modes are used across the project:
  1. FILE  - monthly Parquet trip extracts over HTTP (TLC CloudFront)
  2. FILE  - the taxi zone lookup CSV (reference data)
  3. API   - hourly weather JSON from the Open-Meteo archive API
(SQL, the fourth mode, is how everything is queried downstream in DuckDB.)

Design rules enforced here:
  * Raw inputs are never modified. Downloads land byte-for-byte on disk.
  * Downloads are atomic: bytes go to a .part file that is renamed only on
    success, so an interrupted run can never leave a truncated file that a
    later run would mistake for complete.
  * Downloads are idempotent: a file already present with a matching size
    is not re-fetched unless --force-download is passed.
  * Every retrieved file is recorded in data/raw/manifest.json with its
    URL, SHA-256, byte size and retrieval time - the evidence that
    retrieval happened and what exactly was retrieved.
"""

from __future__ import annotations

import hashlib
import json
import time
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .config import Config
from .logging_utils import get_logger


class RetrievalError(RuntimeError):
    """Raised when a source cannot be retrieved after retries."""


# ---------------------------------------------------------------- manifest


def load_manifest(cfg: Config) -> dict[str, Any]:
    if cfg.manifest_path.exists():
        return json.loads(cfg.manifest_path.read_text())
    return {}


def save_manifest(cfg: Config, manifest: dict[str, Any]) -> None:
    cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def _record(manifest: dict[str, Any], key: str, path: Path, url: str, cfg: Config) -> None:
    manifest[key] = {
        "url": url,
        "path": str(path.relative_to(cfg.raw_dir.parent.parent)),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------- download


def _download(url: str, dest: Path, cfg: Config) -> None:
    """Download url -> dest atomically, with retries on transient failures."""
    log = get_logger()
    retries = cfg.retrieval["max_retries"]
    backoff = cfg.retrieval["backoff_seconds"]
    timeout = cfg.retrieval["timeout_seconds"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                expected = int(r.headers.get("content-length", 0))
                written = 0
                with part.open("wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                        written += len(chunk)
            # Guard against a silently truncated stream.
            if expected and written != expected:
                raise RetrievalError(
                    f"Truncated download: got {written} bytes, expected {expected}"
                )
            part.replace(dest)  # atomic
            log.info("    downloaded %s (%.1f MB)", dest.name, written / 1e6)
            return
        except Exception as exc:  # noqa: BLE001 - retried below
            last = exc
            part.unlink(missing_ok=True)
            if attempt < retries:
                wait = backoff * attempt
                log.warning(
                    "    attempt %d/%d failed for %s (%s); retrying in %ds",
                    attempt, retries, url, exc, wait,
                )
                time.sleep(wait)
    raise RetrievalError(f"Failed to retrieve {url} after {retries} attempts: {last}")


# ---------------------------------------------------------------- sources


def fetch_trip_file(cfg: Config, fleet: str, month: str, force: bool = False) -> Path:
    """Retrieve one fleet-month Parquet extract (retrieval mode: FILE)."""
    log = get_logger()
    dest = cfg.trip_path(fleet, month)
    url = cfg.trip_url(fleet, month)
    manifest = load_manifest(cfg)
    key = f"trips/{fleet}/{month}"

    if dest.exists() and not force:
        # Trust an existing file only if the manifest agrees with what is
        # on disk; a size mismatch means a previous run was interrupted.
        entry = manifest.get(key)
        if entry and entry.get("bytes") == dest.stat().st_size:
            log.info("    %s %s already present, skipping download", fleet, month)
            return dest
        log.warning("    %s %s present but unverified; re-downloading", fleet, month)

    log.info("    fetching %s", url)
    _download(url, dest, cfg)
    _record(manifest, key, dest, url, cfg)
    save_manifest(cfg, manifest)
    return dest


def fetch_zone_lookup(cfg: Config, force: bool = False) -> Path:
    """Retrieve the taxi zone reference table (retrieval mode: FILE/CSV)."""
    log = get_logger()
    dest = cfg.zone_lookup_path
    url = cfg.sources["zone_lookup"]["url"]
    manifest = load_manifest(cfg)

    if dest.exists() and not force:
        log.info("    zone lookup already present, skipping download")
        return dest

    log.info("    fetching %s", url)
    _download(url, dest, cfg)
    _record(manifest, "reference/taxi_zone_lookup", dest, url, cfg)
    save_manifest(cfg, manifest)
    return dest


def fetch_weather(cfg: Config, month: str, force: bool = False) -> Path:
    """Retrieve hourly NYC weather for a month (retrieval mode: API/JSON).

    The API response is written to disk unchanged, so the raw record of what
    the API returned survives even if the API itself changes later.
    """
    log = get_logger()
    dest = cfg.weather_path(month)
    if dest.exists() and not force:
        log.info("    weather %s already present, skipping API call", month)
        return dest

    src = cfg.sources["weather"]
    year, mon = (int(x) for x in month.split("-"))
    last_day = monthrange(year, mon)[1]
    params = {
        "latitude": src["latitude"],
        "longitude": src["longitude"],
        "start_date": f"{month}-01",
        "end_date": f"{month}-{last_day:02d}",
        "hourly": ",".join(src["hourly"]),
        "timezone": src["timezone"],
    }

    retries = cfg.retrieval["max_retries"]
    backoff = cfg.retrieval["backoff_seconds"]
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            log.info("    calling weather API for %s", month)
            r = requests.get(src["url"], params=params, timeout=60)
            r.raise_for_status()
            payload = r.json()
            if "hourly" not in payload or "time" not in payload.get("hourly", {}):
                raise RetrievalError("Weather API response missing 'hourly.time'")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".json.part")
            tmp.write_text(json.dumps(payload))
            tmp.replace(dest)
            manifest = load_manifest(cfg)
            _record(manifest, f"weather/{month}", dest, r.url, cfg)
            save_manifest(cfg, manifest)
            log.info("    weather %s: %d hourly rows", month, len(payload["hourly"]["time"]))
            return dest
        except Exception as exc:  # noqa: BLE001 - retried below
            last = exc
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RetrievalError(f"Weather API failed for {month}: {last}")


def ingest_month(cfg: Config, month: str, force: bool = False) -> dict[str, Path]:
    """Retrieve every source needed for one month."""
    log = get_logger()
    log.info("  [ingest] %s", month)
    paths: dict[str, Path] = {}
    fetch_zone_lookup(cfg, force=force)
    paths["zone_lookup"] = cfg.zone_lookup_path
    for fleet in cfg.fleets:
        paths[fleet] = fetch_trip_file(cfg, fleet, month, force=force)
    paths["weather"] = fetch_weather(cfg, month, force=force)
    return paths
