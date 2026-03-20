"""
Discord webhook notification utility.
Posts escalation alerts to a Discord channel via webhook.
Webhook URL is read from the .env file (DISCORD_WEBHOOK_URL).
"""

import json
import logging
import os
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

_ENV_FILE = Path(__file__).parents[2] / ".env"


def _get_webhook_url() -> str | None:
    # 1. Check environment variable first
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if url:
        return url

    # 2. Fall back to reading .env file directly
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DISCORD_WEBHOOK_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
                if url:
                    return url
    except Exception:
        pass

    return None


def notify(message: str) -> bool:
    """
    Post a message to the configured Discord webhook channel.
    Returns True on success, False on any failure (never raises).
    """
    url = _get_webhook_url()
    if not url:
        log.warning("discord_notify: DISCORD_WEBHOOK_URL not set — skipping notification")
        return False

    try:
        body = json.dumps({"content": message[:2000]}).encode()
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "J_Claw/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        log.error("discord_notify: webhook POST failed: %s", e)
        return False


def notify_escalation(division: str, skill: str, reason: str,
                      action_items: list | None = None) -> bool:
    """
    Convenience wrapper for orchestrator escalation alerts.
    Formats a clean Discord message from packet fields.
    """
    lines = [
        f"**J_Claw Escalation** — {division.upper()} / {skill}",
        f"> {reason}",
    ]
    if action_items:
        lines.append("")
        for item in action_items[:5]:
            label = item.get("description", item) if isinstance(item, dict) else str(item)
            lines.append(f"• {label[:200]}")
        if len(action_items) > 5:
            lines.append(f"_…and {len(action_items) - 5} more_")

    return notify("\n".join(lines))
