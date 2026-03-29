"""
asset-integration skill — Tier 0 (deterministic, no LLM).
Reads production division packets to check asset delivery status.
Compares delivered assets against game design requirements.
Returns a gap analysis.
"""

import json
import logging
from pathlib import Path

from runtime.config import STATE_DIR
from runtime import packet

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"


def _extract_required_assets(design_pkt: dict | None) -> list[dict]:
    """
    Extract required assets from the game design packet.
    Looks in metrics and summary for asset references.
    Also reads the GDD for structured asset requirements.
    """
    required = []

    # Read from GDD file if it exists
    gdd_file = GAMEDEV_DIR / "gdd.json"
    if gdd_file.exists():
        try:
            with open(gdd_file, encoding="utf-8") as f:
                gdd = json.load(f)
            for asset in gdd.get("required_assets", []):
                if isinstance(asset, str):
                    required.append({"name": asset, "type": "unspecified", "source": "gdd"})
                elif isinstance(asset, dict):
                    required.append({
                        "name": asset.get("name", "unknown"),
                        "type": asset.get("type", "unspecified"),
                        "priority": asset.get("priority", "normal"),
                        "source": "gdd",
                    })
        except Exception as e:
            log.warning("Failed to read GDD for asset requirements: %s", e)

    # Read asset requirements file if it exists
    req_file = GAMEDEV_DIR / "asset-requirements.json"
    if req_file.exists():
        try:
            with open(req_file, encoding="utf-8") as f:
                reqs = json.load(f)
            for asset in reqs if isinstance(reqs, list) else reqs.get("assets", []):
                if isinstance(asset, dict):
                    required.append({
                        "name": asset.get("name", "unknown"),
                        "type": asset.get("type", "unspecified"),
                        "priority": asset.get("priority", "normal"),
                        "source": "asset-requirements",
                    })
        except Exception as e:
            log.warning("Failed to read asset-requirements.json: %s", e)

    return required


def _extract_delivered_assets(catalog_pkt: dict | None, deliver_pkt: dict | None) -> list[dict]:
    """Extract delivered/available assets from production division packets."""
    delivered = []

    if catalog_pkt:
        # Asset catalog typically lists all available assets
        catalog_items = catalog_pkt.get("metrics", {}).get("assets", [])
        if isinstance(catalog_items, list):
            for item in catalog_items:
                if isinstance(item, str):
                    delivered.append({"name": item, "status": "cataloged"})
                elif isinstance(item, dict):
                    delivered.append({
                        "name": item.get("name", "unknown"),
                        "type": item.get("type", ""),
                        "status": "cataloged",
                    })

        # Also check summary for asset counts
        catalog_count = catalog_pkt.get("metrics", {}).get("total_assets", 0)
        if catalog_count and not catalog_items:
            log.info("Asset catalog reports %d total assets but no itemized list.", catalog_count)

    if deliver_pkt:
        # Delivery packet lists recently delivered assets
        deliveries = deliver_pkt.get("metrics", {}).get("delivered", [])
        if isinstance(deliveries, list):
            for item in deliveries:
                if isinstance(item, str):
                    delivered.append({"name": item, "status": "delivered"})
                elif isinstance(item, dict):
                    delivered.append({
                        "name": item.get("name", "unknown"),
                        "type": item.get("type", ""),
                        "status": "delivered",
                    })

    return delivered


def _extract_qa_status(qa_pkt: dict | None) -> dict:
    """Extract QA pipeline status from production packet."""
    if not qa_pkt:
        return {"available": False}
    return {
        "available": True,
        "status": qa_pkt.get("status", "unknown"),
        "summary": qa_pkt.get("summary", ""),
        "pass_rate": qa_pkt.get("metrics", {}).get("pass_rate"),
        "failed_assets": qa_pkt.get("metrics", {}).get("failed_assets", []),
    }


def _compare_assets(required: list[dict], delivered: list[dict]) -> list[dict]:
    """Compare required vs delivered assets. Return gaps."""
    delivered_names = {d["name"].lower() for d in delivered}
    gaps = []

    for req in required:
        req_name = req["name"].lower()
        if req_name not in delivered_names:
            gaps.append({
                "asset": req["name"],
                "type": req.get("type", "unspecified"),
                "priority": req.get("priority", "normal"),
                "reason": "not found in production catalog or deliveries",
            })

    return gaps


def run(**kwargs) -> dict:
    """
    Cross-division asset gap analysis. Reads production packets and compares
    against game design requirements. Pure Python — no LLM needed.
    """
    GAMEDEV_DIR.mkdir(parents=True, exist_ok=True)

    # Read cross-division packets
    catalog_pkt = packet.read_fresh("production", "asset-catalog", 1440)     # 24h
    deliver_pkt = packet.read_fresh("production", "asset-deliver", 720)      # 12h
    qa_pkt      = packet.read_fresh("production", "qa-pipeline", 1440)       # 24h
    design_pkt  = packet.read_fresh("gamedev", "game-design", 4320)          # 3 days

    # Extract data
    required = _extract_required_assets(design_pkt)
    delivered = _extract_delivered_assets(catalog_pkt, deliver_pkt)
    qa_status = _extract_qa_status(qa_pkt)
    gaps = _compare_assets(required, delivered)

    # Determine data availability
    has_production_data = bool(catalog_pkt or deliver_pkt)
    has_requirements = bool(required)

    if not has_production_data and not has_requirements:
        return {
            "status": "partial",
            "summary": (
                "Asset integration: no production packets and no asset requirements found. "
                "Add required_assets to state/gamedev/gdd.json or create state/gamedev/asset-requirements.json."
            ),
            "gaps": [],
            "metrics": {
                "required_count": 0,
                "delivered_count": 0,
                "gap_count": 0,
                "has_catalog": bool(catalog_pkt),
                "has_deliveries": bool(deliver_pkt),
                "has_qa": bool(qa_pkt),
                "has_design": bool(design_pkt),
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Build summary
    summary_parts = [
        f"Asset integration: {len(required)} required, {len(delivered)} delivered/cataloged.",
    ]
    if gaps:
        summary_parts.append(f"{len(gaps)} gap(s) identified.")
        high_priority_gaps = [g for g in gaps if g.get("priority") == "high"]
        if high_priority_gaps:
            summary_parts.append(f"{len(high_priority_gaps)} high-priority gap(s).")
    else:
        summary_parts.append("All required assets accounted for.")

    if qa_status.get("available"):
        if qa_status.get("failed_assets"):
            summary_parts.append(f"QA: {len(qa_status['failed_assets'])} asset(s) failed pipeline.")
        elif qa_status.get("pass_rate") is not None:
            summary_parts.append(f"QA pass rate: {qa_status['pass_rate']}%.")

    # Escalate if critical gaps
    high_priority_gaps = [g for g in gaps if g.get("priority") in ("high", "critical")]
    escalate = len(high_priority_gaps) >= 3
    escalation_reason = (
        f"{len(high_priority_gaps)} high-priority asset gaps detected"
        if escalate else ""
    )

    return {
        "status": "success",
        "summary": " ".join(summary_parts),
        "gaps": gaps,
        "qa_status": qa_status,
        "metrics": {
            "required_count": len(required),
            "delivered_count": len(delivered),
            "gap_count": len(gaps),
            "high_priority_gaps": len(high_priority_gaps),
            "has_catalog": bool(catalog_pkt),
            "has_deliveries": bool(deliver_pkt),
            "has_qa": bool(qa_pkt),
            "has_design": bool(design_pkt),
        },
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }
