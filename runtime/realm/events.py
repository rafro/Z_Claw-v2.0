"""
Canonical realm game-event stream.

UI layers should consume these emitted facts instead of reconstructing gameplay
 state from partial files.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)

EVENTS_FILE = STATE_DIR / "game-events.jsonl"


def emit(event: str, **data) -> dict:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "run_id": os.getenv("JCLAW_RUN_ID", ""),
        "source": "python_runtime",
        **data,
    }
    try:
        EVENTS_FILE.parent.mkdir(exist_ok=True)
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("game event write failed: %s", e)
    return entry


def recent(limit: int = 25) -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    try:
        with open(EVENTS_FILE, encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        return entries
    except Exception as e:
        log.error("game event read failed: %s", e)
        return []
