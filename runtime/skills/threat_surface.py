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

    messages = [
        {
            "role": "system",
            "content": (
                "You are the OP-Sec threat surface analyst for J_Claw reviewing a personal Windows 11 gaming/dev machine.\n\n"
                "CRITICAL — APPROVED SAFE LIST. You MUST NOT flag ANY of the following. These are verified legitimate:\n"
                "- PORT 17500: Dropbox LAN sync daemon — SAFE, DO NOT FLAG\n"
                "- PORT 27036: Steam Remote Play — SAFE, DO NOT FLAG\n"
                "- PORTS 27015-27030: Steam game servers — SAFE, DO NOT FLAG\n"
                "- PORT 1900: SSDP/UPnP — SAFE, DO NOT FLAG\n"
                "- PORT 5353: mDNS — SAFE, DO NOT FLAG\n"
                "- PORT 7680: Windows Update delivery optimization — SAFE, DO NOT FLAG\n"
                "- PORTS 49152-65535: Windows ephemeral/dynamic RPC ports — SAFE, DO NOT FLAG\n"
                "- PORT 5040: Windows Runtime Broker — SAFE, DO NOT FLAG\n"
                "- PORT 3702: WS-Discovery — SAFE, DO NOT FLAG\n"
                "- PORT 5357: WSDAPI — SAFE, DO NOT FLAG\n"
                "- TASK AMDRyzenMasterSDKTask: AMD Adrenalin GPU software — SAFE, DO NOT FLAG\n"
                "- PROCESS cpumetricsserver.exe: AMD Adrenalin CPU metrics — SAFE, DO NOT FLAG\n"
                "- Any scheduled task under AMD\\, NVIDIA\\, Intel\\, or Microsoft\\ paths — SAFE\n"
                "- PROCESSES: Steam.exe, Dropbox.exe, Discord.exe, chrome.exe, msedge.exe, node.exe, "
                "python.exe, ollama.exe, pm2, RadeonSoftware.exe, svchost.exe — all SAFE, DO NOT FLAG\n\n"
                "Only flag items that are NOT in the above list and are genuinely anomalous for a gaming/dev machine.\n"
                "If everything looks normal, return an empty anomalies array.\n\n"
                'Return JSON only: {"summary": "1-2 sentences", "anomalies": ['
                '{"type": "port|task|process", "detail": "", '
                '"severity": "HIGH|MEDIUM|LOW", "recommendation": ""}], '
                '"escalate": false}'
            ),
        },
        {"role": "user", "content": context},
    ]

    try:
        result     = chat_json(MODEL, messages, host=OLLAMA_HOST, temperature=0.1, max_tokens=800, task_type="threat-surface")
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
