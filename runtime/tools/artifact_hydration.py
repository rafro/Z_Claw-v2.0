"""
Artifact hydration utilities — Tier 0 (pure Python).
Provides read-if-fresh, cold manifest scanning, selective extraction,
and warn-only budget enforcement.
"""

import json
import logging
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from runtime.config import DIVISIONS_DIR

log = logging.getLogger(__name__)


def read_fresh(division: str, skill: str, max_age_minutes: int = 60) -> Optional[dict]:
    """Read a packet only if generated within max_age_minutes. Returns None if stale or missing."""
    pkt_path = DIVISIONS_DIR / division / "packets" / f"{skill}.json"
    if not pkt_path.exists():
        return None
    try:
        with open(pkt_path, encoding="utf-8") as f:
            pkt = json.load(f)
    except Exception:
        return None
    generated_at = pkt.get("generated_at")
    if not generated_at:
        return pkt  # no timestamp = treat as fresh (backward compat)
    try:
        ts = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - ts
        if age > timedelta(minutes=max_age_minutes):
            log.debug("Stale packet: %s/%s age=%s (max=%dm)", division, skill, age, max_age_minutes)
            return None
        return pkt
    except (ValueError, TypeError):
        return pkt


def cold_manifest(division: str) -> list[dict]:
    """Scan cold storage and return metadata about each archive without opening zips."""
    cold_dir = DIVISIONS_DIR / division / "cold"
    if not cold_dir.exists():
        return []
    results = []
    for zf in sorted(cold_dir.glob("*.zip")):
        entry = {
            "archive": zf.name,
            "bundle_id": zf.stem,
            "size_bytes": zf.stat().st_size,
            "created_at": datetime.fromtimestamp(zf.stat().st_mtime, tz=timezone.utc).isoformat(),
            "manifest": None,
            "file_count": None,
        }
        # Check for sidecar manifest
        sidecar = zf.with_suffix(".zip.manifest.json")
        if sidecar.exists():
            try:
                with open(sidecar, encoding="utf-8") as f:
                    entry["manifest"] = json.load(f)
                    entry["file_count"] = len(entry["manifest"].get("files", []))
            except Exception:
                pass
        results.append(entry)
    return results


def hydrate_from_cold(division: str, bundle_id: str, files: list[str] | None = None) -> list[Path]:
    """Extract specific files (or all) from a cold archive to hot."""
    cold_dir = DIVISIONS_DIR / division / "cold"
    hot_dir = DIVISIONS_DIR / division / "hot"
    hot_dir.mkdir(parents=True, exist_ok=True)

    # Find the archive
    archive = None
    for zf in cold_dir.glob("*.zip"):
        if zf.stem == bundle_id or bundle_id in zf.stem:
            archive = zf
            break
    if archive is None:
        raise FileNotFoundError(f"No cold archive matching '{bundle_id}' in {cold_dir}")

    extracted = []
    with zipfile.ZipFile(archive, "r") as z:
        members = z.namelist()
        to_extract = members if files is None else [m for m in members if m in files or Path(m).name in files]
        for member in to_extract:
            z.extract(member, hot_dir)
            extracted.append(hot_dir / member)
            log.info("Hydrated: %s → %s", archive.name, member)
    return extracted


def enforce_budget(division: str) -> dict:
    """Check hot directory size vs max_hot_mb. Warn only — no auto-deletion."""
    hot_dir = DIVISIONS_DIR / division / "hot"
    if not hot_dir.exists():
        return {"division": division, "over_budget": False, "current_mb": 0, "max_hot_mb": 0}

    # Read budget from config
    try:
        config_path = DIVISIONS_DIR / division / "config.json"
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        max_mb = config.get("artifact_policy", {}).get("max_hot_mb", 0)
    except Exception:
        max_mb = 0

    current_mb = _dir_size_mb(hot_dir)
    over = current_mb > max_mb if max_mb > 0 else False

    if over:
        log.warning("BUDGET WARNING: %s hot dir is %.1fMB / %dMB budget", division, current_mb, max_mb)

    return {
        "division": division,
        "max_hot_mb": max_mb,
        "current_mb": round(current_mb, 1),
        "over_budget": over,
    }


def _dir_size_mb(directory: Path) -> float:
    """Sum file sizes in directory (non-recursive), return megabytes."""
    total = sum(f.stat().st_size for f in directory.iterdir() if f.is_file())
    return total / (1024 * 1024)
