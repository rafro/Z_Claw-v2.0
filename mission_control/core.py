"""
MissionControl — central task queue and dispatch.
Backed by state/task-queue.json.
Operators submit tasks via /api/tasks; division orchestrators read them.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from runtime.config import STATE_DIR
from schemas.tasks import Task, ApprovalRequest
from mission_control.audit import AuditLog
from mission_control.approval import ApprovalGate

log = logging.getLogger(__name__)

TASK_QUEUE_FILE = STATE_DIR / "task-queue.json"

_audit = AuditLog()
_approval = ApprovalGate()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MissionControl:

    def _load(self) -> dict:
        try:
            if TASK_QUEUE_FILE.exists():
                return json.loads(TASK_QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("MissionControl load failed: %s", e)
        return {"tasks": []}

    def _save(self, data: dict) -> None:
        try:
            TASK_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            TASK_QUEUE_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        except Exception as e:
            log.error("MissionControl save failed: %s", e)

    # ── Task lifecycle ────────────────────────────────────────────────────────

    def submit_task(
        self,
        task_type: str,
        division: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Create and queue a task. Returns task_id."""
        task = Task(type=task_type, division=division, payload=payload or {})
        data = self._load()
        data["tasks"].append(task.model_dump())
        self._save(data)
        _audit.log("task_submitted", "mission_control", {
            "type": task_type, "division": division
        }, task_id=task.id)
        log.info("Task submitted: %s [%s/%s]", task.id, division, task_type)
        return task.id

    def get_task(self, task_id: str) -> Optional[dict]:
        data = self._load()
        for t in data["tasks"]:
            if t["id"] == task_id:
                return t
        return None

    def list_tasks(self, status: Optional[str] = None, limit: int = 50) -> list[dict]:
        data = self._load()
        tasks = data["tasks"]
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        return tasks[-limit:]

    def start_task(self, task_id: str) -> bool:
        data = self._load()
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] == "queued":
                t["status"] = "running"
                t["started_at"] = _utcnow()
                self._save(data)
                _audit.log("task_started", "mission_control", {}, task_id=task_id)
                return True
        return False

    def complete_task(self, task_id: str, result: Any, provider_used: str = "") -> bool:
        data = self._load()
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["status"] = "completed"
                t["completed_at"] = _utcnow()
                t["result"] = result
                t["provider_used"] = provider_used
                self._save(data)
                _audit.log("task_completed", "mission_control", {
                    "provider_used": provider_used
                }, task_id=task_id)
                return True
        return False

    def fail_task(self, task_id: str, error: str) -> bool:
        data = self._load()
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["status"] = "failed"
                t["completed_at"] = _utcnow()
                t["error"] = error
                self._save(data)
                _audit.log("task_failed", "mission_control", {"error": error}, task_id=task_id)
                return True
        return False

    def set_awaiting_approval(self, task_id: str) -> bool:
        data = self._load()
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["status"] = "awaiting_approval"
                self._save(data)
                return True
        return False

    # ── Approvals ─────────────────────────────────────────────────────────────

    def list_pending_approvals(self) -> list[dict]:
        return _approval.list_pending()

    def resolve_approval(self, approval_id: str, decision: str) -> bool:
        return _approval.resolve(approval_id, decision)

    def request_approval(
        self,
        task_id: str,
        summary: str,
        recommended_action: str,
        urgency: str = "normal",
        timeout_behavior: str = "reject",
        notify: bool = True,
    ) -> str:
        self.set_awaiting_approval(task_id)
        approval_id = _approval.request_approval(
            task_id=task_id,
            summary=summary,
            recommended_action=recommended_action,
            urgency=urgency,
            timeout_behavior=timeout_behavior,
        )
        if notify:
            try:
                from mission_control.notify import Notifier
                from schemas.tasks import ApprovalRequest
                req = ApprovalRequest(
                    id=approval_id,
                    task_id=task_id,
                    summary=summary,
                    recommended_action=recommended_action,
                    urgency=urgency,
                    timeout_behavior=timeout_behavior,
                )
                Notifier().send_approval_request(req)
            except Exception as e:
                log.warning("Could not send approval notification: %s", e)
        return approval_id

    # ── Convenience ───────────────────────────────────────────────────────────

    def dispatch(self, task_id: str) -> Optional[dict]:
        """
        Mark task as running and return it for the division to execute.
        Returns None if task not found or not in queued state.
        """
        task = self.get_task(task_id)
        if not task:
            return None
        if task["status"] != "queued":
            log.warning("Cannot dispatch task %s — status is %s", task_id, task["status"])
            return None
        self.start_task(task_id)
        return task
