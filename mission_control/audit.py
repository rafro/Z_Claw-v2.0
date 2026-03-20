"""
AuditLog — append-only JSONL audit trail.
One JSON entry per line in logs/audit.jsonl.
"""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path
from typing import Any, Optional

from runtime.config import LOGS_DIR
from schemas.logs import AuditEntry, ProviderEvent

log = logging.getLogger(__name__)

AUDIT_FILE = LOGS_DIR / "audit.jsonl"


class AuditLog:

    def _write(self, entry: dict) -> None:
        try:
            AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            log.error("AuditLog write failed: %s", e)

    def log(
        self,
        event_type: str,
        agent: str,
        data: dict[str, Any],
        task_id: Optional[str] = None,
    ) -> None:
        entry = AuditEntry(
            event_type=event_type,
            task_id=task_id,
            agent=agent,
            data=data,
        )
        self._write(entry.model_dump())

    def log_error(self, task_id: Optional[str], agent: str, error: str, tb: str = "") -> None:
        self.log(
            event_type="error",
            agent=agent,
            task_id=task_id,
            data={"error": error, "traceback": tb},
        )

    def log_provider_event(
        self,
        provider_id: str,
        event_type: str,
        latency_ms: Optional[int] = None,
        task_type: Optional[str] = None,
        error: str = "",
    ) -> None:
        entry = ProviderEvent(
            provider_id=provider_id,
            event_type=event_type,
            latency_ms=latency_ms,
            task_type=task_type,
            error=error,
        )
        self._write({"_type": "provider_event", **entry.model_dump()})

    def recent(self, n: int = 50) -> list[dict]:
        """Return last n audit entries."""
        try:
            if not AUDIT_FILE.exists():
                return []
            lines = AUDIT_FILE.read_text(encoding="utf-8").strip().splitlines()
            return [json.loads(l) for l in lines[-n:] if l.strip()]
        except Exception as e:
            log.error("AuditLog read failed: %s", e)
            return []
