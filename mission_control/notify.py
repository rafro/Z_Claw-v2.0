"""
Notifier — sends messages to Matthew via Telegram.
Lifted and generalized from server.js inline handlers.
Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.parse
from typing import Optional

from schemas.tasks import ApprovalRequest

log = logging.getLogger(__name__)

URGENCY_EMOJI = {
    "low":      "📋",
    "normal":   "📬",
    "high":     "⚠️",
    "critical": "🚨",
}


class Notifier:

    def __init__(self):
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    def _is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def _post(self, text: str) -> bool:
        if not self._is_configured():
            log.info("Telegram not configured — message suppressed: %s", text[:80])
            return False
        try:
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            payload = json.dumps({
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            log.error("Telegram send failed: %s", e)
            return False

    def send(self, message: str, urgency: str = "normal") -> bool:
        emoji = URGENCY_EMOJI.get(urgency, "📬")
        return self._post(f"{emoji} {message}")

    def send_approval_request(self, approval: ApprovalRequest) -> bool:
        emoji = URGENCY_EMOJI.get(approval.urgency, "📬")
        text = (
            f"{emoji} *APPROVAL REQUIRED*\n\n"
            f"*Task ID:* `{approval.task_id}`\n"
            f"*Approval ID:* `{approval.id}`\n"
            f"*Urgency:* {approval.urgency.upper()}\n\n"
            f"*Summary:* {approval.summary}\n\n"
            f"*Recommended:* {approval.recommended_action}\n\n"
            f"Reply with:\n"
            f"`/approve {approval.id}` — proceed\n"
            f"`/reject {approval.id}` — cancel\n"
            f"`/escalate {approval.id}` — route to Claude"
        )
        return self._post(text)

    def send_packet_summary(self, packet: dict) -> bool:
        division = packet.get("division", "?")
        skill = packet.get("skill", "?")
        status = packet.get("status", "?")
        summary = packet.get("summary", "")
        escalate = packet.get("escalate", False)
        provider = packet.get("provider_used", "")

        emoji = "✅" if status == "success" else ("⚠️" if status == "partial" else "❌")
        esc_note = "\n🚨 *ESCALATION REQUIRED*" if escalate else ""

        text = (
            f"{emoji} *{division.upper()} / {skill}*\n"
            f"Status: `{status}` | Provider: `{provider or 'unknown'}`\n\n"
            f"{summary[:400]}"
            f"{esc_note}"
        )
        return self._post(text)
