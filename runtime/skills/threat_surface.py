"""
threat-surface skill — Tier 1 LLM (Qwen2.5 7B local).
Scans open ports, running processes, and scheduled tasks for anomalies.
Daily evening run. Local only — no external calls.
"""

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import SKILL_MODELS, OLLAMA_HOST, ROOT
from runtime.ollama_client import chat_json, is_available

log        = logging.getLogger(__name__)
MODEL      = SKILL_MODELS["threat-surface"]
HOT_DIR    = ROOT / "divisions" / "op-sec" / "hot"
PACKET_DIR = ROOT / "divisions" / "op-sec" / "packets"


def _cmd(args: list, timeout: int = 15) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def _gather() -> dict:
    return {
        "tcp_connections": _cmd(["netstat", "-ano", "-p", "TCP"], timeout=10)[:3000],
        "scheduled_tasks": _cmd(
            ["schtasks", "/query", "/fo", "LIST", "/v"], timeout=20
        )[:4000],
        "running_processes": _cmd(["tasklist", "/fo", "csv"], timeout=15)[:3000],
    }


def run() -> dict:
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    PACKET_DIR.mkdir(parents=True, exist_ok=True)

    surface = _gather()

    if not is_available(MODEL, host=OLLAMA_HOST):
        result = {
            "status":   "partial",
            "summary":  "Threat surface data gathered — LLM unavailable for analysis",
            "escalate": False,
            "anomalies": [],
            "anomaly_count": 0,
            "high_severity": 0,
        }
        with open(PACKET_DIR / "threat-surface.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return result

    context = (
        f"TCP CONNECTIONS:\n{surface['tcp_connections'][:1500]}\n\n"
        f"SCHEDULED TASKS (excerpt):\n{surface['scheduled_tasks'][:2000]}\n\n"
        f"RUNNING PROCESSES:\n{surface['running_processes'][:1500]}"
    )

    # Known-safe ports and tasks for this machine — suppress false positives
    whitelist_note = (
        "KNOWN-SAFE WHITELIST for this machine — do NOT flag these:\n"
        "PORTS: 17500 (Dropbox LAN sync), 27036 (Steam Remote Play), "
        "27015-27030 (Steam game servers), 1900 (SSDP/UPnP), "
        "5353 (mDNS), 7680 (Windows Update delivery), "
        "49152-65535 (Windows ephemeral/RPC ports — normal), "
        "5040 (Windows runtime broker), 3702 (WSD), 5357 (WSDAPI).\n"
        "TASKS: AMDRyzenMasterSDKTask / cpumetricsserver.exe (AMD Adrenalin GPU software — legitimate), "
        "any task under AMD\\, NVIDIA\\, Intel\\, Microsoft\\, or standard vendor paths.\n"
        "PROCESSES: Steam.exe, Dropbox.exe, Discord.exe, chrome.exe, node.exe, python.exe, "
        "ollama.exe, pm2, cpumetricsserver.exe, RadeonSoftware.exe — all expected.\n"
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are the OP-Sec threat surface analyst for J_Claw. "
                "Review Windows 11 system data from a personal gaming/dev machine. "
                "Flag anything suspicious: unexpected listening ports, scheduled tasks "
                "that look like persistence mechanisms, or unknown processes. "
                f"{whitelist_note}"
                "Only flag items NOT on the whitelist that are genuinely anomalous. "
                'Return JSON: {"summary": "1-2 sentences", "anomalies": ['
                '{"type": "port|task|process", "detail": "", '
                '"severity": "HIGH|MEDIUM|LOW", "recommendation": ""}], '
                '"escalate": false} '
                "Return valid JSON only. Empty array if nothing suspicious."
            ),
        },
        {"role": "user", "content": context},
    ]

    try:
        result     = chat_json(MODEL, messages, host=OLLAMA_HOST, temperature=0.1, max_tokens=800)
        anomalies  = result.get("anomalies", []) if isinstance(result, dict) else []
        summary    = result.get("summary", "Threat surface scan complete.") if isinstance(result, dict) else "Scan complete."
        escalate   = result.get("escalate", False) if isinstance(result, dict) else False
        high_count = sum(1 for a in anomalies if a.get("severity") == "HIGH")
        if high_count > 0:
            escalate = True

        packet = {
            "status":        "success",
            "escalate":      escalate,
            "anomalies":     anomalies,
            "anomaly_count": len(anomalies),
            "high_severity": high_count,
            "summary":       summary,
            "scanned_at":    datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.error("threat-surface LLM failed: %s", e)
        packet = {
            "status":        "partial",
            "summary":       f"Threat surface data gathered — LLM analysis failed: {e}",
            "escalate":      False,
            "anomalies":     [],
            "anomaly_count": 0,
            "high_severity": 0,
        }

    with open(PACKET_DIR / "threat-surface.json", "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2)

    return packet
