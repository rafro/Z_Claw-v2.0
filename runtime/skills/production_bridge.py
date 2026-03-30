"""
production-bridge skill — Fulfills asset requests by calling Production skills.
Reads pending requests from state/gamedev/asset-requests/pending.json,
dispatches to matching Production orchestrator functions, tracks fulfillment.
Tier 0 for dispatch logic + whatever tier Production skills use.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"
REQUESTS_DIR = GAMEDEV_DIR / "asset-requests"
PENDING_FILE = REQUESTS_DIR / "pending.json"
FULFILLMENT_FILE = REQUESTS_DIR / "fulfillment.json"

# Map target_skill values to (module-level import path, function name, default kwargs)
_SKILL_DISPATCH = {
    "image-generate":  "run_image_generate",
    "sprite-generate": "run_sprite_generate",
    "music-compose":   "run_music_compose",
    "sfx-generate":    "run_sfx_generate",
    "voice-generate":  "run_voice_generate",
}


def _load_pending() -> list[dict]:
    """Load pending asset requests from disk."""
    if not PENDING_FILE.exists():
        return []
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read %s: %s", PENDING_FILE, e)
        return []
    if not isinstance(data, list):
        log.warning("pending.json is not a list — ignoring")
        return []
    return data


def _save_pending(requests: list[dict]) -> None:
    """Write updated requests back to pending.json."""
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(requests, f, indent=2, ensure_ascii=False)


def _save_fulfillment(summary: dict) -> None:
    """Write fulfillment summary to fulfillment.json."""
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(FULFILLMENT_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)


def _get_production_func(func_name: str):
    """
    Lazily import the production orchestrator and return the requested function.
    Deferred import avoids circular-import issues and keeps the module lightweight
    when production.py's heavy imports aren't needed (e.g. dry_run).
    """
    from runtime.orchestrators import production
    fn = getattr(production, func_name, None)
    if fn is None:
        raise AttributeError(f"production module has no function '{func_name}'")
    return fn


def _dispatch_request(request: dict) -> dict:
    """
    Call the appropriate Production function for a single request.
    Returns a result dict with at least 'status' and 'summary' keys.
    """
    target_skill = request.get("target_skill", "")
    func_name = _SKILL_DISPATCH.get(target_skill)
    if func_name is None:
        return {
            "status": "failed",
            "summary": f"Unknown target_skill '{target_skill}'",
        }

    fn = _get_production_func(func_name)

    # Build kwargs from the request's prompt / metadata where the production
    # function signature expects them.
    kwargs: dict = {}
    prompt = request.get("prompt", "")
    source = request.get("source", "")
    name = request.get("name", source.split("/")[-1] if source else "asset")

    if target_skill == "image-generate":
        kwargs = {"asset_type": "portrait_bust", "subject": name}
    elif target_skill == "sprite-generate":
        kwargs = {"target": name, "sprite_type": "chibi_sprite"}
    elif target_skill == "music-compose":
        kwargs = {"track_type": "game_bgm", "division": "gamedev", "mood": "epic"}
    elif target_skill == "sfx-generate":
        sfx_type = request.get("sfx_type", "sword_slash")
        kwargs = {"sfx_type": sfx_type}
    elif target_skill == "voice-generate":
        kwargs = {"commander": name, "line_type": "greeting", "text": prompt}

    return fn(**kwargs)


def run(**kwargs) -> dict:
    """
    Fulfill pending asset requests by dispatching to Production skills.

    kwargs:
        max_requests (int):  Maximum number of pending requests to process.
                             Default 10.
        dry_run (bool):      If True, log what would be called without executing.
                             Default False.
    """
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)

    max_requests: int = kwargs.get("max_requests", 10)
    dry_run: bool = kwargs.get("dry_run", False)

    requests = _load_pending()
    if not requests:
        return {
            "status": "partial",
            "summary": (
                "No pending asset requests found. "
                "Run asset-requester first to generate requests."
            ),
            "metrics": {
                "requests_processed": 0,
                "delivered": 0,
                "failed": 0,
                "skipped": 0,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    delivered = 0
    failed = 0
    skipped = 0
    processed = 0
    errors: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    for req in requests:
        if processed >= max_requests:
            break

        # Only process requests that are still pending
        if req.get("status") != "pending":
            skipped += 1
            continue

        target_skill = req.get("target_skill", "")

        # Validate that we know how to handle this skill
        if target_skill not in _SKILL_DISPATCH:
            req["status"] = "failed"
            req["error"] = f"Unsupported target_skill: {target_skill}"
            req["fulfilled_at"] = now
            failed += 1
            processed += 1
            errors.append({
                "source": req.get("source", ""),
                "target_skill": target_skill,
                "error": req["error"],
            })
            continue

        if dry_run:
            func_name = _SKILL_DISPATCH[target_skill]
            log.info(
                "DRY RUN: would call production.%s() for %s (%s)",
                func_name,
                req.get("source", "unknown"),
                target_skill,
            )
            skipped += 1
            processed += 1
            continue

        # Dispatch — catch per-request so one failure doesn't kill the batch
        try:
            result = _dispatch_request(req)
            result_status = result.get("status", "failed")

            if result_status in ("success", "partial"):
                req["status"] = "delivered"
                req["fulfilled_at"] = now
                req["production_result"] = result.get("summary", "")
                delivered += 1
            else:
                req["status"] = "failed"
                req["error"] = result.get("summary", "Production function returned non-success")
                req["fulfilled_at"] = now
                failed += 1
                errors.append({
                    "source": req.get("source", ""),
                    "target_skill": target_skill,
                    "error": req["error"],
                })

        except Exception as e:
            log.error(
                "Production dispatch failed for %s (%s): %s",
                req.get("source", "unknown"),
                target_skill,
                e,
            )
            req["status"] = "failed"
            req["error"] = str(e)
            req["fulfilled_at"] = now
            failed += 1
            errors.append({
                "source": req.get("source", ""),
                "target_skill": target_skill,
                "error": str(e),
            })

        processed += 1

    # Persist updated request statuses
    _save_pending(requests)

    # Build fulfillment summary
    fulfillment = {
        "timestamp": now,
        "requests_processed": processed,
        "delivered": delivered,
        "failed": failed,
        "skipped": skipped,
        "dry_run": dry_run,
        "errors": errors,
    }
    _save_fulfillment(fulfillment)

    # Determine overall status
    if processed == 0:
        status = "partial"
        summary = "No actionable pending requests found (all already processed or skipped)."
    elif failed == processed:
        status = "failed"
        summary = f"All {processed} request(s) failed. Check Production service availability."
    elif dry_run:
        status = "success"
        summary = f"Dry run: {processed} request(s) inspected, no production calls made."
    elif failed > 0:
        status = "partial"
        summary = (
            f"Fulfilled {delivered}/{processed} request(s). "
            f"{failed} failed, {skipped} skipped."
        )
    else:
        status = "success"
        summary = f"Fulfilled {delivered}/{processed} request(s). Pipeline healthy."

    # Escalate if more than half the batch failed
    escalate = failed > 0 and failed >= (processed / 2)
    escalation_reason = (
        f"{failed}/{processed} production requests failed — "
        "check ComfyUI/Ollama service availability"
        if escalate else ""
    )

    # Action items for failures
    action_items = []
    if errors:
        action_items.append({
            "priority": "high" if escalate else "normal",
            "description": (
                f"{failed} asset request(s) failed. "
                "Review fulfillment.json for error details and retry after "
                "confirming production services are running."
            ),
            "requires_matthew": escalate,
        })

    return {
        "status": status,
        "summary": summary,
        "metrics": {
            "requests_processed": processed,
            "delivered": delivered,
            "failed": failed,
            "skipped": skipped,
            "dry_run": dry_run,
        },
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": action_items,
    }
