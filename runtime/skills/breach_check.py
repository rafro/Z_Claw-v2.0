"""
breach-check skill — Tier 0 (pure Python).
Checks configured email addresses against Have I Been Pwned (HIBP) v3 API.
Weekly run. Requires HIBP_API_KEY in .env. Emails configured in
divisions/op-sec/breach_emails.json. Data logged locally only.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import ROOT

log          = logging.getLogger(__name__)
HOT_DIR      = ROOT / "divisions" / "op-sec" / "hot"
PACKET_DIR   = ROOT / "divisions" / "op-sec" / "packets"
EMAILS_FILE  = ROOT / "divisions" / "op-sec" / "breach_emails.json"
HIBP_API_KEY = os.getenv("HIBP_API_KEY", "")


def _load_emails() -> list:
    if not EMAILS_FILE.exists():
        return []
    try:
        data = json.loads(EMAILS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data.get("emails", [])
    except Exception:
        return []


def _check_email(email: str) -> dict:
    url = (
        f"https://haveibeenpwned.com/api/v3/breachedaccount/"
        f"{urllib.parse.quote(email)}?truncateResponse=false"
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "OpenClaw-OPSec/1.0", "hibp-api-key": HIBP_API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return {
                "email":        email,
                "breached":     True,
                "breach_count": len(data),
                "breaches":     [b["Name"] for b in data],
                "newest":       max((b.get("BreachDate", "") for b in data), default=""),
            }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"email": email, "breached": False, "breach_count": 0, "breaches": []}
        return {"email": email, "error": f"HTTP {e.code}", "breached": False}
    except Exception as ex:
        return {"email": email, "error": str(ex), "breached": False}


def run() -> dict:
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    PACKET_DIR.mkdir(parents=True, exist_ok=True)

    if not HIBP_API_KEY:
        result = {
            "status":   "skipped",
            "reason":   "HIBP_API_KEY not set in .env — add key to enable breach monitoring",
            "escalate": False,
            "summary":  "Breach check skipped — HIBP_API_KEY not configured",
        }
        with open(PACKET_DIR / "breach-check.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return result

    emails = _load_emails()
    if not emails:
        result = {
            "status":   "skipped",
            "reason":   "No emails configured — add to divisions/op-sec/breach_emails.json",
            "escalate": False,
            "summary":  "Breach check skipped — no emails configured",
        }
        with open(PACKET_DIR / "breach-check.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return result

    results = []
    for email in emails:
        results.append(_check_email(email))
        time.sleep(1.6)  # HIBP rate limit: 1 req/1.5s

    breached = [r for r in results if r.get("breached")]
    errors   = [r for r in results if "error" in r]
    escalate = len(breached) > 0

    summary = (
        f"Breach check: {len(breached)}/{len(emails)} email(s) compromised — IMMEDIATE ACTION REQUIRED"
        if breached else
        f"Breach check: clean — {len(emails)} email(s) checked, no new breaches"
    )

    packet = {
        "status":         "success",
        "escalate":       escalate,
        "emails_checked": len(emails),
        "breached_count": len(breached),
        "breached":       breached,
        "errors":         errors,
        "summary":        summary,
        "checked_at":     datetime.now(timezone.utc).isoformat(),
    }

    with open(PACKET_DIR / "breach-check.json", "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2)

    return packet
