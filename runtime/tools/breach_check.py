"""Breach check cross-wire — reads op-sec breach packet, no LLM."""
import json
import logging
from pathlib import Path

from runtime.config import DIVISIONS_DIR

log = logging.getLogger(__name__)

BREACH_PACKET = DIVISIONS_DIR / "op-sec" / "packets" / "breach-check.json"


def is_breach_active() -> bool:
    """Return True if the last breach-check packet reported compromised emails."""
    try:
        if not BREACH_PACKET.exists():
            return False
        data = json.loads(BREACH_PACKET.read_text(encoding="utf-8"))
        return data.get("breached_count", 0) > 0 or data.get("escalate", False)
    except Exception as e:
        log.warning("breach_check read failed (non-fatal): %s", e)
        return False


def breach_summary() -> str:
    """One-line breach status for cross-division summaries."""
    try:
        if not BREACH_PACKET.exists():
            return ""
        data = json.loads(BREACH_PACKET.read_text(encoding="utf-8"))
        if data.get("breached_count", 0) > 0:
            return f"BREACH ACTIVE: {data['breached_count']} compromised email(s)"
        return ""
    except Exception:
        return ""
