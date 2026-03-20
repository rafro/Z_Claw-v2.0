"""
QueueMonitor — checks for stale tasks and queue anomalies.
Pure deterministic — reads state/task-queue.json.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)

STALE_MINUTES = 30      # task "running" for > 30min is probably stuck
MAX_RETRIES_ALERT = 2   # alert if retry_count >= this


class QueueMonitor:

    def run(self) -> dict[str, Any]:
        """
        Inspect the task queue for anomalies.

        Returns:
        {
            "anomalies": [{"type": str, "task_id": str, "detail": str}],
            "queue_depth": int,
            "running_count": int,
            "failed_count": int,
            "summary": str,
        }
        """
        task_file = STATE_DIR / "task-queue.json"
        anomalies: list[dict] = []

        try:
            if not task_file.exists():
                return {
                    "anomalies": [],
                    "queue_depth": 0,
                    "running_count": 0,
                    "failed_count": 0,
                    "summary": "No task queue found",
                }

            data = json.loads(task_file.read_text(encoding="utf-8"))
            tasks = data.get("tasks", [])
        except Exception as e:
            log.error("QueueMonitor read failed: %s", e)
            return {
                "anomalies": [{"type": "read_error", "task_id": "", "detail": str(e)}],
                "queue_depth": 0,
                "running_count": 0,
                "failed_count": 0,
                "summary": f"Queue read error: {e}",
            }

        now = datetime.now(timezone.utc)
        queued = [t for t in tasks if t.get("status") == "queued"]
        running = [t for t in tasks if t.get("status") == "running"]
        failed = [t for t in tasks if t.get("status") == "failed"]

        # Check for stuck running tasks
        for t in running:
            started = t.get("started_at")
            if started:
                try:
                    dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    minutes = (now - dt).total_seconds() / 60
                    if minutes > STALE_MINUTES:
                        anomalies.append({
                            "type": "stale_task",
                            "task_id": t["id"],
                            "detail": f"Running for {minutes:.0f}min ({t.get('type', '?')})",
                        })
                except Exception:
                    pass

        # Check for excessive retries
        for t in tasks:
            if t.get("retry_count", 0) >= MAX_RETRIES_ALERT:
                anomalies.append({
                    "type": "excessive_retries",
                    "task_id": t["id"],
                    "detail": f"Retried {t['retry_count']}x ({t.get('type', '?')})",
                })

        summary = (
            f"Queue: {len(queued)} queued, {len(running)} running, {len(failed)} failed. "
            f"Anomalies: {len(anomalies)}"
        )

        return {
            "anomalies": anomalies,
            "queue_depth": len(queued),
            "running_count": len(running),
            "failed_count": len(failed),
            "summary": summary,
        }
