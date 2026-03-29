"""
artifact-manager — Tier 0 (pure Python).
Manages hot/cold artifact cache TTL for all divisions.
  Hot  → recent files (< TTL_HOT days). Readable by orchestrators.
  Cold → compressed archives (TTL_HOT–TTL_COLD days).
  Purge→ cold files older than TTL_COLD are deleted.
"""

import logging
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from runtime.config import DIVISIONS_DIR, LOGS_DIR
from runtime.tools.atomic_write import atomic_write_json
from runtime.tools.artifact_hydration import enforce_budget

log = logging.getLogger(__name__)

TTL_HOT  = 7    # days before hot → cold (archived + compressed)
TTL_COLD = 30   # days before cold → purged
DIVISIONS = ["opportunity", "trading", "personal", "dev-automation"]


def _infer_skill(filename: str) -> str:
    """Parse skill name from artifact filename. E.g. 'security-scan-2026-03-19.json' -> 'security-scan'"""
    # Remove date suffix and extension
    stem = Path(filename).stem
    # Try to strip date patterns like -2026-03-19 or -20260319
    parts = stem.rsplit("-", 3)
    if len(parts) >= 4 and parts[-3].isdigit():
        return "-".join(parts[:-3])
    if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) == 8:
        return "-".join(parts[:-1])
    return stem


def _archive_hot(division: str) -> tuple[int, int]:
    """Move hot files older than TTL_HOT to cold/ as .zip. Returns (archived, errors)."""
    hot_dir  = DIVISIONS_DIR / division / "hot"
    cold_dir = DIVISIONS_DIR / division / "cold"
    if not hot_dir.exists():
        return 0, 0

    cold_dir.mkdir(parents=True, exist_ok=True)
    cutoff   = datetime.now(timezone.utc) - timedelta(days=TTL_HOT)
    archived = errors = 0

    for f in hot_dir.iterdir():
        if not f.is_file():
            continue
        try:
            stat = f.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                dest = cold_dir / (f.name + ".zip")
                with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(f, f.name)

                # Write cold manifest sidecar
                manifest = {
                    "bundle_id": f.stem,
                    "division": division,
                    "source_skill": _infer_skill(f.name),
                    "created_at": mtime.isoformat(),
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                    "files": [{"name": f.name, "size_bytes": stat.st_size}],
                }
                sidecar_path = dest.with_suffix(".zip.manifest.json")
                atomic_write_json(sidecar_path, manifest)

                f.unlink()
                archived += 1
                log.debug("Archived %s -> %s (+sidecar)", f.name, dest.name)
        except Exception as e:
            log.warning("artifact-manager archive error %s: %s", f.name, e)
            errors += 1

    return archived, errors


def _purge_cold(division: str) -> tuple[int, int]:
    """Delete cold files older than TTL_COLD. Returns (purged, errors)."""
    cold_dir = DIVISIONS_DIR / division / "cold"
    if not cold_dir.exists():
        return 0, 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=TTL_COLD)
    purged = errors = 0

    for f in cold_dir.iterdir():
        if not f.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                purged += 1
                log.debug("Purged %s", f.name)
        except Exception as e:
            log.warning("artifact-manager purge error %s: %s", f.name, e)
            errors += 1

    return purged, errors


def run() -> dict:
    LOGS_DIR.mkdir(exist_ok=True)

    total_archived = total_purged = total_errors = 0
    per_division: dict[str, dict] = {}

    for div in DIVISIONS:
        archived, err_a = _archive_hot(div)
        purged,   err_p = _purge_cold(div)
        total_archived += archived
        total_purged   += purged
        total_errors   += err_a + err_p
        per_division[div] = {"archived": archived, "purged": purged}
        if archived or purged:
            log.info("artifact-manager %s: archived=%d purged=%d", div, archived, purged)

    # Budget checks after archive + purge cycle
    budget_warnings = []
    for div in DIVISIONS:
        status = enforce_budget(div)
        if status["over_budget"]:
            budget_warnings.append(f"{div}: {status['current_mb']}MB / {status['max_hot_mb']}MB")

    summary = (
        f"Artifact cleanup: {total_archived} files archived, "
        f"{total_purged} cold files purged."
    )
    if total_errors:
        summary += f" ({total_errors} errors)"
    if budget_warnings:
        summary += f" Budget warnings: {'; '.join(budget_warnings)}"

    return {
        "status":         "success" if not total_errors else "partial",
        "summary":        summary,
        "total_archived": total_archived,
        "total_purged":   total_purged,
        "total_errors":   total_errors,
        "per_division":   per_division,
        "budget_warnings": budget_warnings,
    }
