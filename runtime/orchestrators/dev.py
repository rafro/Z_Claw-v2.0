"""
Dev Division Orchestrator — pipeline-based code generation workflow.
Runs workers sequentially: generate → review → test → summarize → finalize.
Always gates on human approval before output is considered final.

This supplements (does not replace) dev_automation.py for pipeline tasks.
"""

from __future__ import annotations

import logging

from runtime import packet
from runtime.tools.xp import grant_skill_xp
from mission_control.core import MissionControl
from runtime.workers.dev.generator import CodeGenerator
from runtime.workers.dev.reviewer import CodeReviewer
from runtime.workers.dev.tester import TestRunner
from runtime.workers.dev.summarizer import DevSummarizer
from runtime.workers.dev.finalizer import DevFinalizer

log = logging.getLogger(__name__)

_mc = MissionControl()


def run_dev_pipeline(spec: dict) -> dict:
    """
    Run the full dev pipeline for a code generation request.

    spec: {
        "description": str,
        "language": str,         # default "python"
        "existing_code": str,    # optional
        "context": str,          # optional extra context
    }

    Returns an ExecutivePacket dict.
    """
    description = spec.get("description", "")
    language = spec.get("language", "python")
    existing_code = spec.get("existing_code", "")
    context = spec.get("context", "")

    if not description:
        pkt = packet.build(
            division="dev",
            skill="dev-pipeline",
            status="failed",
            summary="No description provided",
            escalate=False,
        )
        packet.write(pkt)
        return pkt

    # Register in Mission Control
    task_id = _mc.submit_task("dev-pipeline", "dev", {"spec": spec})
    _mc.start_task(task_id)

    log.info("Dev pipeline starting: task=%s lang=%s", task_id, language)

    # ── Step 1: Generate ────────────────────────────────────────────────────
    gen_result = CodeGenerator().run(description, language, existing_code, context)
    log.info("Generator: status=%s provider=%s", gen_result["status"], gen_result["provider_used"])

    if gen_result["status"] == "failed":
        _mc.fail_task(task_id, gen_result["error"])
        pkt = packet.build(
            division="dev",
            skill="dev-pipeline",
            status="failed",
            summary=f"Code generation failed: {gen_result['error']}",
            task_id=task_id,
            provider_used=gen_result["provider_used"],
            escalate=True,
            escalation_reason="Generator failed — no LLM available",
        )
        packet.write(pkt)
        return pkt

    # ── Step 2: Review ──────────────────────────────────────────────────────
    rev_result = CodeReviewer().run(gen_result["code"], language, description)
    log.info("Reviewer: verdict=%s confidence=%.2f", rev_result["verdict"], rev_result.get("confidence", 0))

    # ── Step 3: Test ────────────────────────────────────────────────────────
    test_result = TestRunner().run(gen_result["code"], language, safe_execute=False)
    log.info("Tester: syntax=%s tests=%d/%d",
             test_result["syntax_ok"], test_result["passed"], test_result["tests_run"])

    # ── Step 4: Summarize ───────────────────────────────────────────────────
    sum_result = DevSummarizer().run(description, gen_result, rev_result, test_result)
    log.info("Summarizer: confidence=%.2f provider=%s",
             sum_result["overall_confidence"], sum_result["provider_used"])

    # ── Step 5: Finalize ────────────────────────────────────────────────────
    fin_result = DevFinalizer().run(
        description, gen_result, rev_result, test_result, sum_result, task_id
    )
    log.info("Finalizer: artifact=%s recommendation=%s",
             fin_result.get("artifact_ref", "none"), fin_result.get("recommendation"))

    # ── Approval gate ───────────────────────────────────────────────────────
    approval_id = _mc.request_approval(
        task_id=task_id,
        summary=sum_result["summary"],
        recommended_action=fin_result.get("recommendation", "review"),
        urgency="normal" if fin_result.get("recommendation") == "approve" else "high",
        timeout_behavior="reject",
        notify=True,
    )

    _mc.complete_task(task_id, {
        "generator": gen_result,
        "reviewer": rev_result,
        "tester": test_result,
        "summarizer": sum_result,
        "finalizer": fin_result,
    }, provider_used=gen_result["provider_used"])

    grant_skill_xp("dev-pipeline")

    # Build combined provider attribution
    providers_used = ", ".join(filter(None, {
        gen_result.get("provider_used"),
        rev_result.get("provider_used"),
        sum_result.get("provider_used"),
    }))

    # Determine packet status
    if fin_result.get("recommendation") == "needs_revision":
        pkt_status = "partial"
    elif fin_result.get("status") == "failed":
        pkt_status = "failed"
    else:
        pkt_status = "success"

    escalate = fin_result.get("recommendation") == "escalate"

    action_items = [
        packet.action_item(
            f"Review generated code: {fin_result.get('artifact_ref', 'no artifact')}",
            priority="high",
            requires_matthew=True,
        )
    ]
    for issue in sum_result.get("key_issues", [])[:3]:
        action_items.append(packet.action_item(f"Issue: {issue}", priority="normal"))

    pkt = packet.build(
        division="dev",
        skill="dev-pipeline",
        status=pkt_status,
        summary=sum_result["summary"],
        action_items=action_items,
        metrics={
            "code_length": len(gen_result.get("code", "")),
            "review_verdict": rev_result.get("verdict"),
            "syntax_ok": test_result.get("syntax_ok"),
            "confidence": fin_result.get("confidence"),
            "issue_count": len(rev_result.get("issues", [])),
        },
        artifact_refs=[fin_result.get("artifact_ref", "")],
        escalate=escalate,
        escalation_reason="Low confidence — Claude review recommended" if escalate else "",
        task_id=task_id,
        confidence=fin_result.get("confidence"),
        urgency="high" if escalate else "normal",
        recommended_action=fin_result.get("recommendation", "review"),
        provider_used=providers_used,
        approval_required=True,
        approval_status="pending",
    )
    packet.write(pkt)
    return pkt
