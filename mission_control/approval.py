"""
ApprovalGate — human-in-the-loop approval for high-stakes tasks.
Persists requests to state/approval-queue.json.
Matthew approves/rejects via /api/approvals or Telegram commands.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from runtime.config import STATE_DIR
from schemas.tasks import ApprovalRequest

log = logging.getLogger(__name__)

APPROVAL_FILE = STATE_DIR / "approval-queue.json"

# Default timeout: 30 minutes
DEFAULT_TIMEOUT_S = 1800


class ApprovalGate:

    def _load(self) -> dict:
        try:
            if APPROVAL_FILE.exists():
                return json.loads(APPROVAL_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("ApprovalGate load failed: %s", e)
        return {"approvals": []}

    def _save(self, data: dict) -> None:
        try:
            APPROVAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            APPROVAL_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        except Exception as e:
            log.error("ApprovalGate save failed: %s", e)

    def request_approval(
        self,
        task_id: str,
        summary: str,
        recommended_action: str,
        urgency: str = "normal",
        timeout_behavior: str = "reject",
    ) -> str:
        """Create an approval request. Returns the approval ID."""
        req = ApprovalRequest(
            task_id=task_id,
            summary=summary,
            recommended_action=recommended_action,
            urgency=urgency,
            timeout_behavior=timeout_behavior,
        )
        data = self._load()
        data["approvals"].append(req.model_dump())
        self._save(data)
        log.info("Approval requested: id=%s task=%s urgency=%s", req.id, task_id, urgency)
        return req.id

    def is_approved(self, task_id: str) -> bool:
        data = self._load()
        for a in data["approvals"]:
            if a["task_id"] == task_id:
                return a["status"] == "approved"
        return False

    def get_status(self, approval_id: str) -> Optional[str]:
        data = self._load()
        for a in data["approvals"]:
            if a["id"] == approval_id:
                return a["status"]
        return None

    def resolve(self, approval_id: str, decision: str, resolved_by: str = "matthew") -> bool:
        """
        Resolve an approval request.
        decision: "approve" | "reject" | "escalate"
        Returns True if found and updated.
        """
        valid = {"approve": "approved", "reject": "rejected", "escalate": "escalated"}
        if decision not in valid:
            log.error("Invalid approval decision: %r", decision)
            return False

        data = self._load()
        for a in data["approvals"]:
            if a["id"] == approval_id and a["status"] == "pending":
                a["status"] = valid[decision]
                a["resolved_at"] = datetime.now(timezone.utc).isoformat()
                a["resolved_by"] = resolved_by
                self._save(data)
                log.info("Approval %s resolved: %s by %s", approval_id, decision, resolved_by)
                return True
        return False

    def list_pending(self) -> list[dict]:
        data = self._load()
        return [a for a in data["approvals"] if a["status"] == "pending"]

    def block_until_resolved(
        self,
        approval_id: str,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        poll_interval: float = 5.0,
    ) -> str:
        """
        Poll until the approval is resolved or timeout.
        Returns the final status string.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self.get_status(approval_id)
            if status and status != "pending":
                return status
            time.sleep(poll_interval)

        # Timeout — apply default behavior
        data = self._load()
        for a in data["approvals"]:
            if a["id"] == approval_id and a["status"] == "pending":
                a["status"] = "timed_out"
                a["resolved_at"] = datetime.now(timezone.utc).isoformat()
                a["resolved_by"] = "timeout"
                self._save(data)
                log.warning("Approval %s timed out after %ds — default: %s",
                            approval_id, timeout_s, a.get("timeout_behavior", "reject"))
                return a.get("timeout_behavior", "reject") + "ed"

        return "timed_out"
