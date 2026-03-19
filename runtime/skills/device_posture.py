"""
device-posture skill — Tier 0 (pure Python, no LLM).
Checks Windows security posture: Defender, firewall, UAC, BitLocker, auto-updates.
Runs daily. Local only — no external calls.
"""

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import ROOT

log        = logging.getLogger(__name__)
HOT_DIR    = ROOT / "divisions" / "op-sec" / "hot"
PACKET_DIR = ROOT / "divisions" / "op-sec" / "packets"


def _ps(cmd: str) -> str:
    """Run a PowerShell one-liner, return stdout stripped."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def _check_defender() -> dict:
    out = _ps("(Get-MpComputerStatus).AMServiceEnabled")
    return {"enabled": out.lower() == "true", "raw": out}


def _check_firewall() -> dict:
    out = _ps(
        "(Get-NetFirewallProfile | Where-Object {$_.Enabled -eq $false}).Name -join ','"
    )
    disabled = [p.strip() for p in out.split(",") if p.strip()] if out else []
    return {"all_on": len(disabled) == 0, "disabled_profiles": disabled}


def _check_uac() -> dict:
    out = _ps(
        "(Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System' "
        "-Name EnableLUA -ErrorAction SilentlyContinue).EnableLUA"
    )
    return {"enabled": out.strip() == "1"}


def _check_auto_update() -> dict:
    out = _ps(
        "(Get-ItemProperty -Path "
        "'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU' "
        "-Name NoAutoUpdate -ErrorAction SilentlyContinue).NoAutoUpdate"
    )
    # NoAutoUpdate=1 means updates disabled; missing/empty = updates enabled
    return {"enabled": out.strip() != "1"}


def _check_bitlocker() -> dict:
    out = _ps(
        "(Get-BitLockerVolume -MountPoint C: -ErrorAction SilentlyContinue).ProtectionStatus"
    )
    return {"c_drive_status": out.strip() or "Unknown"}


def run() -> dict:
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    PACKET_DIR.mkdir(parents=True, exist_ok=True)

    checks = {
        "defender":    _check_defender(),
        "firewall":    _check_firewall(),
        "uac":         _check_uac(),
        "auto_update": _check_auto_update(),
        "bitlocker":   _check_bitlocker(),
    }

    issues = []
    if not checks["defender"]["enabled"]:
        issues.append("Windows Defender is DISABLED")
    if not checks["firewall"]["all_on"]:
        profiles = ", ".join(checks["firewall"]["disabled_profiles"])
        issues.append(f"Firewall disabled on: {profiles}")
    if not checks["uac"]["enabled"]:
        issues.append("UAC (User Account Control) is DISABLED")
    if not checks["auto_update"]["enabled"]:
        issues.append("Windows Auto-Updates are DISABLED via policy")

    severity = "ok" if not issues else ("warning" if len(issues) == 1 else "alert")
    escalate = severity == "alert"
    summary = (
        f"Device posture: {severity.upper()} — {len(issues)} issue(s): {'; '.join(issues)}"
        if issues else "Device posture: OK — all security checks passed"
    )

    result = {
        "status":     "success",
        "escalate":   escalate,
        "severity":   severity,
        "issues":     issues,
        "checks":     checks,
        "summary":    summary,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(PACKET_DIR / "device-posture.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return result
