"""
Dev Automation Division Orchestrator — code-specialized model routing.
Skills: repo-monitor, refactor-scan, security-scan → Coder 7B (local).
        debug-agent, doc-update → Coder 14B (friend's 9070 XT, Coder 7B fallback).
Orchestrator synthesis (dev-digest) → Qwen2.5 7B (local prose, not code analysis).
"""

import logging
from datetime import datetime, timezone

from runtime.config import SKILL_MODELS, MODEL_14B_HOST, MODEL_CODER_7B, MODEL_8B, OLLAMA_HOST
from runtime.ollama_client import chat, is_available
from runtime.skills import repo_monitor, debug_agent, refactor_scan, security_scan, doc_update, artifact_manager
from runtime import packet
from runtime.tools.xp import grant_skill_xp

log   = logging.getLogger(__name__)
MODEL = SKILL_MODELS["dev-digest"]   # Llama 3.1 8B — synthesis only, not code analysis


# ── Orchestrator reasoning ─────────────────────────────────────────────────────

def _synthesize_dev_state(
    repo_pkt:      dict | None,
    security_pkt:  dict | None,
    refactor_pkt:  dict | None,
) -> str:
    """
    Cross-skill synthesis: combine repo health, security posture, and refactor debt.
    Produces a technical executive summary for the daily dev digest.
    """
    repo_summary      = repo_pkt.get("summary", "No repo data.")      if repo_pkt      else "No repo data."
    security_summary  = security_pkt.get("summary", "No security data.") if security_pkt else "No security data."
    refactor_summary  = refactor_pkt.get("summary", "No refactor data.") if refactor_pkt else "No refactor data."

    security_high = security_pkt.get("metrics", {}).get("high", 0) if security_pkt else 0
    repo_flags    = (
        repo_pkt.get("metrics", {}).get("flags_high", 0)
        + repo_pkt.get("metrics", {}).get("flags_medium", 0)
    ) if repo_pkt else 0

    # Always local — synthesis is text aggregation, not code reasoning
    if is_available(MODEL, host=OLLAMA_HOST):
        use_model, use_host = MODEL, OLLAMA_HOST
    elif is_available(MODEL_CODER_7B, host=OLLAMA_HOST):
        use_model, use_host = MODEL_CODER_7B, OLLAMA_HOST
    else:
        parts = [s for s in [repo_summary, security_summary, refactor_summary] if s and "No " not in s]
        return " | ".join(parts) if parts else "Dev automation scan complete."

    context = (
        f"Repo health: {repo_summary}\n"
        f"Security posture ({security_high} HIGH findings): {security_summary}\n"
        f"Refactor debt: {refactor_summary}"
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Dev Automation orchestrator for J_Claw. "
                "Given the repo health scan, security findings, and refactor debt, "
                "write a 2-3 sentence executive summary of the OpenClaw codebase health. "
                "Prioritize: security issues first, then repo flags, then refactor debt. "
                "Flag anything requiring immediate action. Be direct — no filler."
            ),
        },
        {"role": "user", "content": context},
    ]
    try:
        result = chat(use_model, messages, host=use_host, temperature=0.2, max_tokens=180)
        lines = result.strip().splitlines()
        if lines and lines[0].rstrip().endswith(":"):
            result = "\n".join(lines[1:]).lstrip()
        return result
    except Exception as e:
        log.warning("dev automation orchestrator synthesis failed: %s", e)
        return repo_summary


def run_repo_monitor() -> dict:
    """Run repo scan every 3h. Dev-digest at 15:00 handles Telegram synthesis."""
    log.info("=== Dev Automation Division: repo-monitor run ===")

    result = repo_monitor.run()

    if result["status"] == "failed":
        pkt = packet.build(
            division="dev-automation",
            skill="repo-monitor",
            status="failed",
            summary="repo-monitor failed — gh CLI not authenticated.",
            escalate=True,
            escalation_reason=result.get("escalation_reason", ""),
        )
        packet.write(pkt)
        return pkt

    analysis  = result.get("analysis", {})
    flags     = result.get("flags", [])
    counts    = result.get("flag_counts", {})
    repos_n   = result.get("repos_checked", 0)

    summary = analysis.get("summary", f"{len(flags)} flags across {repos_n} repos.")

    # Build action items from high-priority findings
    action_items = []
    for finding in analysis.get("high_priority", [])[:5]:
        detail = finding if isinstance(finding, str) else finding.get("detail", str(finding))
        action_items.append(packet.action_item(
            detail, priority="high", requires_matthew=False
        ))
    for rec in analysis.get("recommendations", [])[:3]:
        action_items.append(packet.action_item(
            rec if isinstance(rec, str) else str(rec), priority="normal"
        ))

    pkt = packet.build(
        division="dev-automation",
        skill="repo-monitor",
        status=result["status"],
        summary=summary,
        action_items=action_items,
        metrics={
            "repos_checked": repos_n,
            "flags_high":    counts.get("high", 0),
            "flags_medium":  counts.get("medium", 0),
            "flags_low":     counts.get("low", 0),
        },
        artifact_refs=[{"bundle_id": "repo-scan-today", "location": "hot"}],
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    grant_skill_xp("repo-monitor")
    log.info(
        "Repo-monitor packet written. H=%d M=%d L=%d",
        counts.get("high", 0), counts.get("medium", 0), counts.get("low", 0)
    )
    return pkt


def run_debug_agent(error_text: str, context_files: list[str] | None = None) -> dict:
    """On-demand root cause analysis for a submitted error or stack trace."""
    log.info("=== Dev Automation Division: debug-agent run ===")

    result = debug_agent.run(error_text, context_files)

    summary = result.get("root_cause", "Debug analysis complete.")
    if result.get("suggested_fix"):
        summary += f"\nFix: {result['suggested_fix']}"

    action_items = []
    if result["status"] == "success" and result.get("suggested_fix"):
        action_items.append(packet.action_item(
            f"[{result.get('confidence','?')} confidence] "
            f"{result.get('file_location') or 'Unknown location'}: "
            f"{result['suggested_fix'][:120]}",
            priority="high", requires_matthew=True,
        ))

    pkt = packet.build(
        division="dev-automation",
        skill="debug-agent",
        status=result["status"],
        summary=summary,
        action_items=action_items,
        metrics={
            "confidence": result.get("confidence"),
            "model_used": result.get("model_used"),
            "tier":       result.get("tier"),
        },
        escalate=True,  # always escalate debug results — Matthew needs to see them
        escalation_reason="Debug analysis complete — review required.",
    )

    packet.write(pkt)
    grant_skill_xp("debug-agent")
    log.info("Debug-agent packet written. Confidence=%s", result.get("confidence"))
    return pkt


def run_refactor_scan() -> dict:
    """Weekly refactor scan of the OpenClaw runtime."""
    log.info("=== Dev Automation Division: refactor-scan run ===")

    result = refactor_scan.run()
    findings = result.get("findings", [])
    high = [f for f in findings if f.get("severity") == "high"]

    action_items = [
        packet.action_item(
            f"[{f.get('severity','?')}] {f.get('file','?')}: {f.get('detail','')} — {f.get('suggestion','')}",
            priority="normal" if f.get("severity") != "high" else "high",
        )
        for f in findings[:6]
    ]

    pkt = packet.build(
        division="dev-automation",
        skill="refactor-scan",
        status=result["status"],
        summary=result.get("summary", f"{len(findings)} refactor opportunities found."),
        action_items=action_items,
        metrics={
            "findings":      len(findings),
            "high":          len(high),
            "files_scanned": result.get("files_scanned", 0),
            "model_used":    result.get("model_used"),
        },
    )

    packet.write(pkt)
    if result["status"] == "success":
        grant_skill_xp("refactor-scan")
    log.info("Refactor-scan packet written. Findings=%d", len(findings))
    return pkt


def run_security_scan() -> dict:
    """Weekly security scan of the OpenClaw runtime."""
    log.info("=== Dev Automation Division: security-scan run ===")

    result   = security_scan.run()
    findings = result.get("findings", [])
    high     = [f for f in findings if f.get("severity") == "HIGH"]

    action_items = [
        packet.action_item(
            f"[{f.get('severity','?')}] {f.get('file','?')}:{f.get('line','?')} — "
            f"{f.get('type','?')}: {f.get('detail','')}"
            + (f" | Fix: {f.get('fix','')}" if f.get("fix") else ""),
            priority="high" if f.get("severity") == "HIGH" else "normal",
            requires_matthew=f.get("severity") == "HIGH",
        )
        for f in findings[:8]
    ]

    pkt = packet.build(
        division="dev-automation",
        skill="security-scan",
        status=result["status"],
        summary=result.get("summary", f"{len(findings)} security issues found."),
        action_items=action_items,
        metrics={
            "findings":   len(findings),
            "high":       len(high),
            "model_used": result.get("model_used"),
        },
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    grant_skill_xp("security-scan")
    log.info("Security-scan packet written. HIGH=%d total=%d", len(high), len(findings))
    return pkt


def run_doc_update() -> dict:
    """Weekly architecture documentation generation."""
    log.info("=== Dev Automation Division: doc-update run ===")

    result = doc_update.run()

    pkt = packet.build(
        division="dev-automation",
        skill="doc-update",
        status=result["status"],
        summary=result.get("summary", "Doc update complete."),
        metrics={
            "docs_updated": result.get("docs_updated", []),
            "model_used":   result.get("model_used"),
        },
        artifact_refs=[
            {"bundle_id": f"architecture-doc-{__import__('datetime').date.today()}", "location": "hot"}
        ],
    )

    packet.write(pkt)
    if result["status"] == "success":
        grant_skill_xp("doc-update")
    log.info("Doc-update packet written.")
    return pkt


def run_artifact_manager() -> dict:
    """Daily hot/cold cache cleanup across all divisions."""
    log.info("=== Dev Automation Division: artifact-manager run ===")

    result = artifact_manager.run()

    pkt = packet.build(
        division="dev-automation",
        skill="artifact-manager",
        status=result["status"],
        summary=result.get("summary", "Artifact cleanup complete."),
        metrics={
            "archived": result.get("total_archived", 0),
            "purged":   result.get("total_purged", 0),
            "errors":   result.get("total_errors", 0),
        },
    )

    packet.write(pkt)
    grant_skill_xp("artifact-manager")
    log.info(
        "Artifact-manager packet written. Archived=%d Purged=%d",
        result.get("total_archived", 0), result.get("total_purged", 0)
    )
    return pkt


def run_dev_digest() -> dict:
    """
    Daily 15:00 — orchestrator synthesizes across ALL dev-automation skills.
    Reads repo-monitor, security-scan, and refactor-scan packets,
    produces a single cross-skill executive summary for the daily briefing.
    This replaces the old send_digest=True flag on repo-monitor.
    """
    log.info("=== Dev Automation Division: dev-digest synthesis ===")

    repo_pkt     = packet.read("dev-automation", "repo-monitor")
    security_pkt = packet.read("dev-automation", "security-scan")
    refactor_pkt = packet.read("dev-automation", "refactor-scan")

    synthesis = _synthesize_dev_state(repo_pkt, security_pkt, refactor_pkt)

    # Aggregate escalation signals
    escalate = any(
        p.get("escalate", False)
        for p in [repo_pkt, security_pkt, refactor_pkt]
        if p
    )
    escalation_reasons = [
        p.get("escalation_reason", "")
        for p in [repo_pkt, security_pkt, refactor_pkt]
        if p and p.get("escalation_reason")
    ]

    # Aggregate counts for the digest metrics
    total_high = (
        (repo_pkt.get("metrics", {}).get("flags_high", 0) if repo_pkt else 0)
        + (security_pkt.get("metrics", {}).get("high", 0) if security_pkt else 0)
        + (refactor_pkt.get("metrics", {}).get("high", 0) if refactor_pkt else 0)
    )

    pkt = packet.build(
        division="dev-automation",
        skill="dev-digest",
        status="success",
        summary=synthesis,
        metrics={
            "data_sources":   sum(1 for p in [repo_pkt, security_pkt, refactor_pkt] if p),
            "total_high":     total_high,
            "repo_flags":     repo_pkt.get("metrics", {}).get("flags_high", 0) if repo_pkt else 0,
            "security_high":  security_pkt.get("metrics", {}).get("high", 0) if security_pkt else 0,
            "refactor_high":  refactor_pkt.get("metrics", {}).get("high", 0) if refactor_pkt else 0,
        },
        escalate=escalate,
        escalation_reason=" | ".join(escalation_reasons) if escalation_reasons else "",
    )

    packet.write(pkt)
    grant_skill_xp("repo-monitor")  # credit for cross-skill synthesis
    log.info("Dev-digest packet written. Escalate=%s TotalHigh=%d", escalate, total_high)
    return pkt
