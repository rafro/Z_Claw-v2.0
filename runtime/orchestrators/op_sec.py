"""
OP-Sec Division Orchestrator.
Routes all security skills, synthesizes cross-skill posture summary.
Security data stays local — only breach-check makes an external call (HIBP API).
"""

import logging

from runtime.config import SKILL_MODELS, OLLAMA_HOST
from runtime.ollama_client import chat, is_available
from runtime.skills import (
    device_posture, breach_check, threat_surface,
    cred_audit, privacy_scan, security_scan,
)
from runtime import packet
from runtime.tools.xp import grant_skill_xp

log   = logging.getLogger(__name__)
MODEL = SKILL_MODELS["threat-surface"]   # Tier 1 — 7B local


def _synthesize_posture(pkts: list) -> str:
    """Cross-skill synthesis: produce a single security posture statement."""
    summaries = [p.get("summary", "") for p in pkts if p and p.get("summary")]

    if not is_available(MODEL, host=OLLAMA_HOST):
        return " | ".join(summaries[:3]) if summaries else "OP-Sec digest complete."

    context = "\n".join(f"- {s}" for s in summaries) or "No scan data available."

    messages = [
        {
            "role": "system",
            "content": (
                "You are the OP-Sec Division orchestrator for J_Claw. "
                "Given today's security scan summaries, write a 2-sentence executive posture statement. "
                "Lead with overall risk level (OK/WARNING/ALERT) and the top action item if any. "
                "Be direct — no preamble, no labels."
            ),
        },
        {"role": "user", "content": f"Security scan results:\n{context}"},
    ]
    try:
        return chat(MODEL, messages, host=OLLAMA_HOST, temperature=0.1, max_tokens=120)
    except Exception as e:
        log.warning("OP-Sec synthesis failed: %s", e)
        return summaries[0] if summaries else "OP-Sec digest complete."


# ── Individual skill runners ───────────────────────────────────────────────────

def run_device_posture() -> dict:
    log.info("=== OP-Sec Division: device-posture run ===")
    result = device_posture.run()
    issues = result.get("issues", [])
    action_items = [
        packet.action_item(issue, priority="high", requires_matthew=True)
        for issue in issues
    ]
    pkt = packet.build(
        division="op-sec",
        skill="device-posture",
        status=result["status"],
        summary=result["summary"],
        action_items=action_items,
        escalate=result["escalate"],
        escalation_reason="; ".join(issues) if issues else "",
        metrics={"severity": result.get("severity", "ok"), "issues": len(issues)},
    )
    packet.write(pkt)
    grant_skill_xp("device-posture")
    log.info("Device-posture packet written. Severity=%s", result.get("severity"))
    return pkt


def run_breach_check() -> dict:
    log.info("=== OP-Sec Division: breach-check run ===")
    result = breach_check.run()
    pkt = packet.build(
        division="op-sec",
        skill="breach-check",
        status=result["status"],
        summary=result["summary"],
        escalate=result.get("escalate", False),
        escalation_reason=(
            f"{result.get('breached_count', 0)} email(s) found in breach databases"
            if result.get("escalate") else ""
        ),
        metrics={
            "emails_checked": result.get("emails_checked", 0),
            "breached_count": result.get("breached_count", 0),
        },
    )
    packet.write(pkt)
    grant_skill_xp("breach-check")
    log.info("Breach-check packet written. Breached=%s", result.get("breached_count", 0))
    return pkt


def run_threat_surface() -> dict:
    log.info("=== OP-Sec Division: threat-surface run ===")
    result    = threat_surface.run()
    anomalies = result.get("anomalies", [])
    action_items = [
        packet.action_item(
            f"[{a.get('severity','?')}] {a.get('type','?').upper()}: {a.get('detail','')} — {a.get('recommendation','')}",
            priority="high" if a.get("severity") == "HIGH" else "normal",
            requires_matthew=a.get("severity") == "HIGH",
        )
        for a in anomalies[:8]
    ]
    pkt = packet.build(
        division="op-sec",
        skill="threat-surface",
        status=result["status"],
        summary=result["summary"],
        action_items=action_items,
        escalate=result.get("escalate", False),
        escalation_reason=(
            f"{result.get('high_severity', 0)} HIGH severity anomalies detected"
            if result.get("escalate") else ""
        ),
        metrics={
            "anomaly_count": result.get("anomaly_count", 0),
            "high_severity": result.get("high_severity", 0),
        },
    )
    packet.write(pkt)
    grant_skill_xp("threat-surface")
    log.info("Threat-surface packet written. Anomalies=%s", result.get("anomaly_count", 0))
    return pkt


def run_cred_audit() -> dict:
    log.info("=== OP-Sec Division: cred-audit run ===")
    result     = cred_audit.run()
    findings   = result.get("findings", [])
    high_count = sum(1 for f in findings if f.get("severity") == "HIGH")
    action_items = [
        packet.action_item(
            f"[{f.get('severity','?')}] {f.get('file','?')}: {f.get('detail', f.get('pattern',''))}",
            priority="high" if f.get("severity") == "HIGH" else "normal",
            requires_matthew=f.get("severity") == "HIGH",
        )
        for f in findings[:8]
    ]
    pkt = packet.build(
        division="op-sec",
        skill="cred-audit",
        status=result["status"],
        summary=result["summary"],
        action_items=action_items,
        escalate=result.get("escalate", False),
        escalation_reason=f"{high_count} HIGH severity credential exposures" if result.get("escalate") else "",
        metrics={"findings": len(findings), "high": high_count},
    )
    packet.write(pkt)
    grant_skill_xp("cred-audit")
    log.info("Cred-audit packet written. Findings=%s", len(findings))
    return pkt


def run_privacy_scan() -> dict:
    log.info("=== OP-Sec Division: privacy-scan run ===")
    result     = privacy_scan.run()
    findings   = result.get("findings", [])
    high_count = sum(1 for f in findings if f.get("severity") == "HIGH")
    action_items = [
        packet.action_item(
            f"[{f.get('severity','?')}] {f.get('file','?')}: {f.get('detail', f.get('type',''))}",
            priority="high" if f.get("severity") == "HIGH" else "normal",
            requires_matthew=f.get("severity") == "HIGH",
        )
        for f in findings[:8]
    ]
    pkt = packet.build(
        division="op-sec",
        skill="privacy-scan",
        status=result["status"],
        summary=result["summary"],
        action_items=action_items,
        escalate=result.get("escalate", False),
        escalation_reason=f"{high_count} HIGH severity PII exposures" if result.get("escalate") else "",
        metrics={"findings": len(findings), "high": high_count},
    )
    packet.write(pkt)
    grant_skill_xp("privacy-scan")
    log.info("Privacy-scan packet written. Findings=%s", len(findings))
    return pkt


def run_security_scan() -> dict:
    log.info("=== OP-Sec Division: security-scan run ===")
    result      = security_scan.run()
    findings    = result.get("findings", [])
    fp_count    = result.get("false_positive_count", 0)
    real        = [f for f in findings if not f.get("false_positive")]
    high_count  = sum(1 for f in real if f.get("severity") == "HIGH")
    action_items = [
        packet.action_item(
            f"[{f.get('severity','?')}] {f.get('file','?')}:{f.get('line','?')} — "
            f"{f.get('type','?')}: {f.get('detail','')}",
            priority="high" if f.get("severity") == "HIGH" else "normal",
            requires_matthew=f.get("severity") == "HIGH",
        )
        for f in real[:8]
    ]
    pkt = packet.build(
        division="op-sec",
        skill="security-scan",
        status=result["status"],
        summary=result["summary"],
        action_items=action_items,
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
        metrics={
            "findings":         len(real),
            "high":             high_count,
            "false_positives":  fp_count,
            "total_scanned":    len(findings),
        },
    )
    packet.write(pkt)
    grant_skill_xp("security-scan")
    log.info("Security-scan packet written. Real=%s High=%s FalsePositives=%s", len(real), high_count, fp_count)
    return pkt


def run_opsec_digest() -> dict:
    log.info("=== OP-Sec Division: opsec-digest synthesis ===")

    pkts = [
        packet.read("op-sec", "device-posture"),
        packet.read("op-sec", "breach-check"),
        packet.read("op-sec", "threat-surface"),
        packet.read("op-sec", "cred-audit"),
        packet.read("op-sec", "privacy-scan"),
        packet.read("op-sec", "security-scan"),
    ]

    synthesis = _synthesize_posture(pkts)
    escalate  = any(p.get("escalate", False) for p in pkts if p)
    reasons   = [p.get("escalation_reason", "") for p in pkts if p and p.get("escalation_reason")]

    output = packet.build(
        division="op-sec",
        skill="opsec-digest",
        status="success",
        summary=synthesis,
        escalate=escalate,
        escalation_reason=" | ".join(reasons) if reasons else "",
        metrics={"data_sources": sum(1 for p in pkts if p)},
    )
    packet.write(output)
    log.info("OP-Sec digest written. Escalate=%s", escalate)
    return output
