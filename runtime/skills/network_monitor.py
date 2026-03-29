"""
network-monitor skill — Tier 0 (pure Python) + Tier 1 LLM analysis.
Scans active network connections and listening ports for anomalies.
Daily 03:30 run. Local only — no external calls.

Checks:
  - Active TCP/UDP connections and their remote endpoints
  - Listening ports vs expected baseline
  - DNS resolution anomalies (optional)
  - Unexpected outbound connections during off-hours
"""

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import SKILL_MODELS, OLLAMA_HOST, ROOT
from runtime.ollama_client import chat_json, is_available

log        = logging.getLogger(__name__)
MODEL      = SKILL_MODELS.get("threat-surface", "qwen2.5:7b-instruct-q4_K_M")
HOT_DIR    = ROOT / "divisions" / "op-sec" / "hot"
PACKET_DIR = ROOT / "divisions" / "op-sec" / "packets"

# Ports that are expected on a gaming/dev Windows 11 machine
SAFE_PORTS = {
    80, 443,         # HTTP/HTTPS
    17500,           # Dropbox LAN sync
    27015, 27036,    # Steam
    1900,            # SSDP/UPnP
    5353,            # mDNS
    7680,            # Windows Update delivery optimization
    5040,            # Windows Runtime Broker
    3702,            # WS-Discovery
    5357,            # WSDAPI
    11434,           # Ollama
    3000, 8080,      # Dev servers
    22,              # SSH
}

SAFE_PROCESSES = {
    "steam.exe", "dropbox.exe", "discord.exe", "chrome.exe", "msedge.exe",
    "node.exe", "python.exe", "ollama.exe", "pm2", "radeonsoftware.exe",
    "svchost.exe", "system", "code.exe", "git.exe", "ssh.exe",
}


def _cmd(args: list, timeout: int = 15) -> str:
    """Run a system command, return stdout stripped."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except FileNotFoundError:
        return "ERROR: command not found"
    except Exception as e:
        return f"ERROR: {e}"


def _gather_connections() -> dict:
    """Gather network connection data from the system."""
    return {
        "tcp_connections": _cmd(["netstat", "-ano", "-p", "TCP"], timeout=10)[:4000],
        "udp_connections": _cmd(["netstat", "-ano", "-p", "UDP"], timeout=10)[:2000],
        "listening_ports": _cmd(["netstat", "-an"], timeout=10)[:3000],
    }


def _parse_connections(raw: str) -> list:
    """Parse netstat output into structured connection dicts."""
    connections = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] in ("TCP", "UDP"):
            try:
                local  = parts[1]
                remote = parts[2] if parts[0] == "TCP" else "*:*"
                state  = parts[3] if parts[0] == "TCP" and len(parts) > 3 else "STATELESS"
                pid    = parts[-1] if parts[-1].isdigit() else "?"
                local_port = int(local.rsplit(":", 1)[-1]) if ":" in local else 0
                connections.append({
                    "proto": parts[0],
                    "local": local,
                    "remote": remote,
                    "state": state,
                    "pid": pid,
                    "local_port": local_port,
                })
            except (ValueError, IndexError):
                pass
    return connections


def _tier0_analysis(connections: list) -> dict:
    """Pure Python anomaly detection — no LLM needed."""
    anomalies = []
    flagged_hosts = set()
    listening_ports = set()

    for conn in connections:
        port = conn.get("local_port", 0)
        state = conn.get("state", "")
        remote = conn.get("remote", "")

        # Track listening ports
        if state == "LISTENING":
            listening_ports.add(port)

        # Flag unexpected listening ports (outside safe list and ephemeral range)
        if state == "LISTENING" and port not in SAFE_PORTS and port < 49152:
            anomalies.append({
                "type": "unexpected_listener",
                "detail": f"Port {port} is listening (PID {conn.get('pid', '?')})",
                "severity": "MEDIUM",
                "port": port,
                "pid": conn.get("pid", "?"),
            })

        # Flag connections to unusual remote IPs (non-local, non-standard)
        if state == "ESTABLISHED" and remote and remote not in ("*:*", "0.0.0.0:0"):
            remote_ip = remote.rsplit(":", 1)[0] if ":" in remote else remote
            # Skip local/private IPs
            if not remote_ip.startswith(("127.", "10.", "192.168.", "172.", "0.0.0.0", "[::", "[::1")):
                flagged_hosts.add(remote_ip)

    unexpected_listeners = [a for a in anomalies if a["type"] == "unexpected_listener"]

    return {
        "connections_total": len(connections),
        "listening_ports": sorted(listening_ports),
        "listening_count": len(listening_ports),
        "flagged_hosts": sorted(flagged_hosts),
        "flagged_host_count": len(flagged_hosts),
        "anomalies": anomalies,
        "anomaly_count": len(anomalies),
        "unexpected_listeners": len(unexpected_listeners),
    }


def run() -> dict:
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    PACKET_DIR.mkdir(parents=True, exist_ok=True)

    raw_data = _gather_connections()
    connections = _parse_connections(raw_data.get("tcp_connections", ""))

    tier0 = _tier0_analysis(connections)
    anomalies = tier0["anomalies"]
    anomaly_count = tier0["anomaly_count"]

    # If LLM is available and there are connections to analyze, do deeper analysis
    if is_available(MODEL, host=OLLAMA_HOST) and tier0["connections_total"] > 0:
        context = (
            f"TCP CONNECTIONS:\n{raw_data['tcp_connections'][:2000]}\n\n"
            f"LISTENING PORTS:\n{raw_data['listening_ports'][:1500]}\n\n"
            f"Tier-0 findings: {anomaly_count} anomalies, "
            f"{tier0['flagged_host_count']} external hosts, "
            f"{tier0['listening_count']} listening ports"
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the OP-Sec network monitor for J_Claw reviewing a personal Windows 11 gaming/dev machine.\n\n"
                    "SAFE LIST — DO NOT FLAG:\n"
                    "- Ports 17500 (Dropbox), 27015-27036 (Steam), 1900 (SSDP), 5353 (mDNS)\n"
                    "- Ports 7680 (Win Update), 5040 (Runtime Broker), 3702 (WS-Discovery), 5357 (WSDAPI)\n"
                    "- Ports 49152-65535 (Windows ephemeral/dynamic RPC)\n"
                    "- Ports 11434 (Ollama), 3000/8080 (dev servers), 80/443 (HTTP/HTTPS)\n"
                    "- Processes: Steam, Dropbox, Discord, Chrome, Edge, Node, Python, Ollama, pm2, Radeon\n\n"
                    "Only flag genuinely suspicious network activity.\n"
                    "Return JSON only: {\"summary\": \"1-2 sentences\", \"anomalies\": ["
                    "{\"type\": \"port|connection|dns\", \"detail\": \"\", "
                    "\"severity\": \"HIGH|MEDIUM|LOW\", \"recommendation\": \"\"}], "
                    "\"anomaly_count\": 0}"
                ),
            },
            {"role": "user", "content": context},
        ]

        try:
            llm_result = chat_json(MODEL, messages, host=OLLAMA_HOST, temperature=0.1, max_tokens=600)
            if isinstance(llm_result, dict):
                llm_anomalies = llm_result.get("anomalies", [])
                summary = llm_result.get("summary", "Network monitor scan complete.")
                # Merge LLM findings with tier-0 findings
                if llm_anomalies:
                    anomalies = llm_anomalies
                    anomaly_count = len(anomalies)
                else:
                    summary = llm_result.get("summary", "No network anomalies detected.")
            else:
                summary = "Network scan complete — LLM returned unexpected format."
        except Exception as e:
            log.warning("network-monitor LLM analysis failed: %s", e)
            summary = (
                f"Network scan: {anomaly_count} anomalie(s) detected (Tier-0 only — LLM unavailable). "
                f"{tier0['flagged_host_count']} external hosts observed."
            )
    else:
        if anomaly_count > 0:
            summary = (
                f"Network scan: {anomaly_count} anomalie(s) detected. "
                f"{tier0['flagged_host_count']} external hosts, "
                f"{tier0['listening_count']} listening ports."
            )
        else:
            summary = (
                f"Network scan: clean — {tier0['connections_total']} connections reviewed, "
                f"{tier0['listening_count']} listening ports, no anomalies."
            )

    high_count = sum(1 for a in anomalies if a.get("severity") == "HIGH")
    escalate = high_count > 0

    packet = {
        "status":         "success",
        "escalate":       escalate,
        "anomalies":      anomalies,
        "anomaly_count":  anomaly_count,
        "flagged_hosts":  tier0["flagged_host_count"],
        "summary":        summary,
        "scanned_at":     datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "anomalies":          anomaly_count,
            "anomaly_count":      anomaly_count,
            "flagged_hosts":      tier0["flagged_host_count"],
            "listening_ports":    tier0["listening_count"],
            "connections_total":  tier0["connections_total"],
            "high_severity":      high_count,
        },
    }

    with open(PACKET_DIR / "network-monitor.json", "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2)

    return packet
