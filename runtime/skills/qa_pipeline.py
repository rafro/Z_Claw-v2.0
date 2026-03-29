"""
QA Pipeline — orchestrates a full quality-assurance pass across all production assets.

Runs style_check, audio_test, video_review, and image_review in sequence over a
supplied asset manifest (or the current asset catalog), then produces a consolidated
QA report with pass/fail tallies and action items.

Depends only on existing Production skills:
  - style_check
  - audio_test
  - video_review
  - image_review
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timezone

from runtime.skills import style_check, audio_test, video_review, image_review

log = logging.getLogger(__name__)

# Asset catalog location (same path used by asset_catalog.py)
_CATALOG_PATH = Path("state/asset-catalog.json")

# File extension → QA skill mapping
_EXT_SKILL_MAP = {
    ".png":  "image",
    ".jpg":  "image",
    ".jpeg": "image",
    ".webp": "image",
    ".bmp":  "image",
    ".gif":  "image",
    ".wav":  "audio",
    ".mp3":  "audio",
    ".ogg":  "audio",
    ".flac": "audio",
    ".mp4":  "video",
    ".webm": "video",
    ".mov":  "video",
    ".avi":  "video",
    ".mkv":  "video",
}


def _load_asset_paths() -> list[dict]:
    """
    Load assets from the catalog. Each entry should have at least
    ``path`` and optionally ``commander`` and ``type``.
    Returns a list of dicts: [{path, commander, asset_type}, ...]
    """
    if not _CATALOG_PATH.exists():
        return []

    try:
        data = json.loads(_CATALOG_PATH.read_text())
        assets = data if isinstance(data, list) else data.get("assets", [])
        results = []
        for entry in assets:
            p = entry.get("path") or entry.get("file") or ""
            if not p:
                continue
            results.append({
                "path":       p,
                "commander":  entry.get("commander", "generic"),
                "asset_type": entry.get("type", "unknown"),
                "status":     entry.get("status", "unknown"),
            })
        return results
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("qa_pipeline: failed to read catalog — %s", exc)
        return []


def _classify_asset(file_path: str) -> str:
    """Return 'image', 'audio', 'video', or 'unknown' based on extension."""
    ext = Path(file_path).suffix.lower()
    return _EXT_SKILL_MAP.get(ext, "unknown")


def _run_single_qa(asset: dict) -> dict:
    """Run the appropriate QA skill on one asset and return the result."""
    file_path  = asset["path"]
    commander  = asset.get("commander", "generic")
    asset_kind = _classify_asset(file_path)

    if asset_kind == "image":
        # Run both image review (technical QA) and style check (art direction)
        img_result   = image_review.run(image_path=file_path)
        style_result = style_check.run(image_path=file_path, commander=commander)

        # Combine: fail if either fails
        img_passed   = img_result.get("status") == "success"
        style_passed = style_result.get("status") == "success"

        if img_passed and style_passed:
            return {
                "file":    file_path,
                "kind":    "image",
                "passed":  True,
                "checks":  ["image-review", "style-check"],
                "summary": f"Image QA + style passed: {Path(file_path).name}",
            }
        else:
            issues = []
            if not img_passed:
                issues.append(img_result.get("summary", "image-review failed"))
            if not style_passed:
                issues.append(style_result.get("summary", "style-check failed"))
            return {
                "file":    file_path,
                "kind":    "image",
                "passed":  False,
                "checks":  ["image-review", "style-check"],
                "summary": "; ".join(issues),
            }

    elif asset_kind == "audio":
        result = audio_test.run(audio_path=file_path)
        passed = result.get("status") == "success"
        return {
            "file":    file_path,
            "kind":    "audio",
            "passed":  passed,
            "checks":  ["audio-test"],
            "summary": result.get("summary", ""),
        }

    elif asset_kind == "video":
        result = video_review.run(video_path=file_path)
        passed = result.get("status") == "success"
        return {
            "file":    file_path,
            "kind":    "video",
            "passed":  passed,
            "checks":  ["video-review"],
            "summary": result.get("summary", ""),
        }

    else:
        return {
            "file":    file_path,
            "kind":    "unknown",
            "passed":  None,
            "checks":  [],
            "summary": f"Unsupported asset type for QA: {Path(file_path).suffix}",
        }


def run(asset_paths: list[str] | None = None, commander: str = "generic") -> dict:
    """
    QA Pipeline skill entry point.

    Parameters
    ----------
    asset_paths : list[str] | None
        Explicit list of file paths to QA.  If *None*, loads from the asset catalog.
    commander : str
        Default commander for style checks when not specified per-asset.

    Returns
    -------
    dict
        Standard skill result with status, summary, metrics, action_items, escalate.
    """
    # Gather assets
    if asset_paths:
        assets = [{"path": p, "commander": commander, "asset_type": "unknown"} for p in asset_paths]
    else:
        assets = _load_asset_paths()

    if not assets:
        return {
            "status":       "partial",
            "summary":      "QA pipeline has no assets to review. Provide asset_paths or populate the asset catalog.",
            "metrics":      {"total": 0, "passed": 0, "failed": 0, "skipped": 0},
            "action_items": [],
            "escalate":     False,
        }

    # Run QA on each asset
    results  = []
    passed   = 0
    failed   = 0
    skipped  = 0

    for asset in assets:
        try:
            qa = _run_single_qa(asset)
            results.append(qa)
            if qa["passed"] is True:
                passed += 1
            elif qa["passed"] is False:
                failed += 1
            else:
                skipped += 1
        except Exception as exc:
            log.error("qa_pipeline: error on %s — %s", asset.get("path"), exc)
            results.append({
                "file":    asset.get("path", "?"),
                "kind":    "error",
                "passed":  None,
                "checks":  [],
                "summary": f"QA error: {exc}",
            })
            skipped += 1

    total = len(results)

    # Build action items for failures
    action_items = []
    for r in results:
        if r["passed"] is False:
            action_items.append({
                "priority":        "normal",
                "description":     f"QA failure — {r['file']}: {r['summary']}",
                "requires_matthew": False,
            })

    # Determine overall status
    if failed == 0 and skipped == 0:
        status  = "success"
        summary = f"QA pipeline passed: {passed}/{total} assets cleared all checks."
    elif failed > 0:
        status  = "partial"
        summary = (
            f"QA pipeline found issues: {passed} passed, {failed} failed, "
            f"{skipped} skipped out of {total} assets."
        )
    else:
        status  = "partial"
        summary = f"QA pipeline partial: {passed} passed, {skipped} skipped (no failures) out of {total} assets."

    log.info("qa_pipeline: %s — %d/%d passed", status, passed, total)

    return {
        "status":       status,
        "summary":      summary,
        "metrics":      {
            "total":     total,
            "passed":    passed,
            "failed":    failed,
            "skipped":   skipped,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "details":   results,
        },
        "action_items": action_items,
        "escalate":     failed > (total // 2),  # escalate if >50% fail
    }
