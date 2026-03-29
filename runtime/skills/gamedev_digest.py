"""
gamedev-digest skill — Orchestrator-only synthesis stub.
The actual synthesis logic lives in runtime/orchestrators/gamedev.py run_gamedev_digest().
This module exists so the skill import pattern remains consistent across divisions.
"""

import logging

log = logging.getLogger(__name__)


def run(**kwargs) -> dict:
    """Stub — digest synthesis is performed by the orchestrator, not the skill."""
    return {
        "status": "skipped",
        "summary": "Digest is orchestrator-only.",
        "metrics": {},
    }
