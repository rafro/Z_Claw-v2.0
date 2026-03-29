"""
Training data manifest — tracks capture/review/training lineage.

Stores lightweight metadata (hashes only, no content) at
state/training-manifest.json.  Uses atomic_write_json for safe updates.

This is a utility module imported by scripts (format_for_qvac.py,
model_trainer.py) — it is NOT wired into orchestrators or server.js.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.tools.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = PROJECT_ROOT / "state" / "training-manifest.json"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty_manifest() -> dict[str, Any]:
    """Return a fresh, empty manifest structure."""
    return {
        "captures": {},
        "stats": {
            "total_captured": 0,
            "total_reviewed": 0,
            "total_approved": 0,
            "total_trained": 0,
            "by_domain": {},
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


def _load() -> dict[str, Any]:
    """Load the manifest from disk, returning an empty one if missing/corrupt."""
    if not MANIFEST_PATH.exists():
        return _empty_manifest()
    try:
        with MANIFEST_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Minimal validation
        if "captures" not in data or "stats" not in data:
            log.warning("Manifest missing required keys — reinitialising")
            return _empty_manifest()
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load training manifest (%s) — reinitialising", exc)
        return _empty_manifest()


def _save(manifest: dict[str, Any]) -> None:
    """Persist the manifest to disk atomically."""
    manifest["last_updated"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(MANIFEST_PATH, manifest)


def _ensure_domain_stats(stats: dict, domain: str) -> dict:
    """Ensure a by_domain entry exists, returning it."""
    by_domain = stats.setdefault("by_domain", {})
    if domain not in by_domain:
        by_domain[domain] = {"captured": 0, "approved": 0, "trained": 0}
    return by_domain[domain]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_capture(entry_hash: str, domain: str, timestamp: str) -> None:
    """Log that a new capture was recorded.

    Parameters
    ----------
    entry_hash : str
        Unique hash identifying this capture entry.
    domain : str
        Domain bucket (trading, coding, chat, opsec, personal, other).
    timestamp : str
        ISO-8601 timestamp of when the capture occurred.
    """
    manifest = _load()
    captures = manifest["captures"]

    if entry_hash in captures:
        # Already tracked — nothing to do.
        return

    captures[entry_hash] = {
        "domain": domain,
        "captured_at": timestamp,
        "reviewed": False,
        "approved": False,
        "reviewed_at": None,
        "reviewer": None,
        "trained": False,
        "training_run": None,
    }

    stats = manifest["stats"]
    stats["total_captured"] = stats.get("total_captured", 0) + 1
    domain_stats = _ensure_domain_stats(stats, domain)
    domain_stats["captured"] = domain_stats.get("captured", 0) + 1

    _save(manifest)


def record_review(entry_hash: str, approved: bool, reviewer: str) -> None:
    """Log a review decision for a previously captured entry.

    Parameters
    ----------
    entry_hash : str
        Hash of the capture to review.
    approved : bool
        Whether the capture was approved for training.
    reviewer : str
        Identifier of the reviewer (e.g. "kai", "auto-filter").
    """
    manifest = _load()
    captures = manifest["captures"]

    if entry_hash not in captures:
        log.warning("record_review: hash %s not found in manifest", entry_hash)
        return

    entry = captures[entry_hash]

    # Guard against re-reviewing (idempotent if same decision)
    if entry.get("reviewed"):
        log.debug("record_review: %s already reviewed — updating decision", entry_hash)
        # If changing from approved to rejected, adjust stats
        was_approved = entry.get("approved", False)
        if was_approved and not approved:
            stats = manifest["stats"]
            stats["total_approved"] = max(0, stats.get("total_approved", 0) - 1)
            domain_stats = _ensure_domain_stats(stats, entry["domain"])
            domain_stats["approved"] = max(0, domain_stats.get("approved", 0) - 1)
        elif not was_approved and approved:
            stats = manifest["stats"]
            stats["total_approved"] = stats.get("total_approved", 0) + 1
            domain_stats = _ensure_domain_stats(stats, entry["domain"])
            domain_stats["approved"] = domain_stats.get("approved", 0) + 1
    else:
        # First review
        stats = manifest["stats"]
        stats["total_reviewed"] = stats.get("total_reviewed", 0) + 1
        if approved:
            stats["total_approved"] = stats.get("total_approved", 0) + 1
            domain_stats = _ensure_domain_stats(stats, entry["domain"])
            domain_stats["approved"] = domain_stats.get("approved", 0) + 1

    entry["reviewed"] = True
    entry["approved"] = approved
    entry["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    entry["reviewer"] = reviewer

    _save(manifest)


def record_training(entry_hash: str, domain: str, training_run_id: str) -> None:
    """Log that an approved sample was used in a training run.

    Parameters
    ----------
    entry_hash : str
        Hash of the capture that was used for training.
    domain : str
        Domain bucket the sample belongs to.
    training_run_id : str
        Identifier of the training run (e.g. "run-20260328-trading-v2").
    """
    manifest = _load()
    captures = manifest["captures"]

    if entry_hash not in captures:
        log.warning("record_training: hash %s not found in manifest", entry_hash)
        return

    entry = captures[entry_hash]

    if not entry.get("approved"):
        log.warning(
            "record_training: hash %s not approved — skipping training record",
            entry_hash,
        )
        return

    if entry.get("trained"):
        log.debug("record_training: %s already marked as trained", entry_hash)
        return

    entry["trained"] = True
    entry["training_run"] = training_run_id

    stats = manifest["stats"]
    stats["total_trained"] = stats.get("total_trained", 0) + 1
    domain_stats = _ensure_domain_stats(stats, domain)
    domain_stats["trained"] = domain_stats.get("trained", 0) + 1

    _save(manifest)


def get_unreviewed_count(domain: str | None = None) -> int:
    """Count captures that have not yet been reviewed.

    Parameters
    ----------
    domain : str, optional
        If provided, count only captures in this domain.
    """
    manifest = _load()
    count = 0
    for entry in manifest["captures"].values():
        if entry.get("reviewed"):
            continue
        if domain is not None and entry.get("domain") != domain:
            continue
        count += 1
    return count


def get_untrained_count(domain: str | None = None) -> int:
    """Count approved samples that have not yet been used in training.

    Parameters
    ----------
    domain : str, optional
        If provided, count only captures in this domain.
    """
    manifest = _load()
    count = 0
    for entry in manifest["captures"].values():
        if not entry.get("approved"):
            continue
        if entry.get("trained"):
            continue
        if domain is not None and entry.get("domain") != domain:
            continue
        count += 1
    return count


def get_training_stats() -> dict[str, Any]:
    """Return a summary dict of training pipeline statistics.

    Returns
    -------
    dict
        {
            "total_captured": int,
            "total_reviewed": int,
            "total_approved": int,
            "total_trained": int,
            "by_domain": {
                "<domain>": {"captured": int, "approved": int, "trained": int},
                ...
            },
            "last_updated": str,
        }
    """
    manifest = _load()
    stats = dict(manifest["stats"])
    stats["last_updated"] = manifest.get("last_updated", "unknown")
    return stats


def is_duplicate(content_hash: str) -> bool:
    """Check whether a content hash already exists in the manifest.

    Parameters
    ----------
    content_hash : str
        Hash to check — typically a SHA-256 of the message+response content.
    """
    manifest = _load()
    return content_hash in manifest["captures"]
