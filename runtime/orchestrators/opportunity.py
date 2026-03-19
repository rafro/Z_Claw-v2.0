"""
Opportunity Division Orchestrator.
LLM agent (Qwen2.5 7B) that runs skills, interprets results,
compiles executive packets, and escalates Tier A jobs.
"""

import logging

from runtime.config import SKILL_MODELS
from runtime.ollama_client import chat, is_available
from runtime.skills import job_intake, hard_filter
from runtime import packet

log = logging.getLogger(__name__)

MODEL = SKILL_MODELS["hard-filter"]   # same model runs the orchestrator reasoning


# ── Orchestrator reasoning ────────────────────────────────────────────────────

def _interpret_results(intake_result: dict, filter_result: dict) -> str:
    """
    Ask the LLM to produce a one-paragraph summary of what happened this run.
    This is where the orchestrator adds value — not just reporting counts,
    but noticing patterns, flagging quality of sources, etc.
    """
    if not is_available(MODEL):
        # Fallback summary without LLM
        c = filter_result.get("counts", {})
        src = intake_result.get("source_status", {})
        ok_sources = [k for k, v in src.items() if v == "ok"]
        return (
            f"{intake_result['counts']['new']} new listings from "
            f"{', '.join(ok_sources) or 'no sources'}. "
            f"Tier A: {c.get('tier_a',0)}, Tier B: {c.get('tier_b',0)}, "
            f"Tier C: {c.get('tier_c',0)}."
        )

    tier_a_preview = ""
    if filter_result.get("tier_a"):
        previews = [
            f"- {j['title']} at {j.get('company','?')} ({j.get('location','?')}) "
            f"[score {j.get('score_composite',0):.1f}]"
            for j in filter_result["tier_a"][:3]
        ]
        tier_a_preview = "\nTier A jobs:\n" + "\n".join(previews)

    context = (
        f"Job intake run complete.\n"
        f"Sources: {intake_result['source_status']}\n"
        f"Fetched: {intake_result['counts']['fetched']}, New: {intake_result['counts']['new']}\n"
        f"Scoring: A={filter_result['counts'].get('tier_a',0)} "
        f"B={filter_result['counts'].get('tier_b',0)} "
        f"C={filter_result['counts'].get('tier_c',0)} "
        f"D={filter_result['counts'].get('tier_d',0)}"
        f"{tier_a_preview}"
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Opportunity Division orchestrator for J_Claw. "
                "Write a concise 1–2 sentence summary of this job intake run for the executive briefing. "
                "Be specific: mention source health, notable finds, and any concerns. "
                "Do not pad. J_Claw will read this and act on it."
            ),
        },
        {"role": "user", "content": context},
    ]
    return chat(MODEL, messages, temperature=0.2, max_tokens=150)


# ── Main run ──────────────────────────────────────────────────────────────────

def run_job_intake() -> dict:
    """
    Full job intake pipeline:
    1. Fetch + dedup (job_intake tool)
    2. Score + tier (hard_filter LLM skill)
    3. Orchestrator interprets results
    4. Builds and writes executive packet
    """
    log.info("=== Opportunity Division: job-intake run ===")

    # Step 1: Fetch jobs (pure Python tool)
    intake = job_intake.run()

    if intake["all_failed"]:
        pkt = packet.build(
            division="opportunity",
            skill="job-intake",
            status="failed",
            summary="All job sources failed. No listings retrieved.",
            escalate=True,
            escalation_reason=f"All sources failed: {intake['errors']}",
        )
        packet.write(pkt)
        return pkt

    # Step 2: Score + tier (LLM)
    filtered = hard_filter.run(intake["new_jobs"])

    # Step 3: Orchestrator synthesizes a summary
    summary = _interpret_results(intake, filtered)

    # Step 4: Build action items for Tier A + B
    action_items = []
    for job in filtered.get("tier_a", []) + filtered.get("tier_b", []):
        action_items.append(packet.job_action_item(job))

    # Step 5: Determine escalation (Tier A found)
    escalate = len(filtered.get("tier_a", [])) > 0
    escalation_reason = ""
    if escalate:
        n = len(filtered["tier_a"])
        escalation_reason = f"{n} Tier A job{'s' if n > 1 else ''} found requiring Matthew's review."

    # Also escalate if Adzuna quota hit (blocks Canadian coverage)
    if intake["source_status"].get("Adzuna") == "rate_limited":
        escalate = True
        escalation_reason += " Adzuna rate limited — Canadian coverage blocked this run."

    # Step 6: Determine status
    failed_sources = [k for k, v in intake["source_status"].items() if v == "failed"]
    if failed_sources and intake["counts"]["new"] == 0:
        status = "partial"
    elif not filtered.get("model_available"):
        status = "partial"
    else:
        status = "success"

    counts = filtered.get("counts", {})
    pkt = packet.build(
        division="opportunity",
        skill="job-intake",
        status=status,
        summary=summary,
        action_items=action_items,
        metrics={
            "new_jobs_found":   intake["counts"]["new"],
            "tier_a":           counts.get("tier_a", 0),
            "tier_b":           counts.get("tier_b", 0),
            "tier_c":           counts.get("tier_c", 0),
            "tier_d":           counts.get("tier_d", 0),
            "source_status":    intake["source_status"],
            "model_available":  filtered.get("model_available", True),
        },
        artifact_refs=[{"bundle_id": "job-intake-latest", "location": "hot"}],
        escalate=escalate,
        escalation_reason=escalation_reason,
    )

    packet.write(pkt)
    log.info(
        "Opportunity packet written. Status=%s Escalate=%s A=%d B=%d",
        status, escalate, counts.get("tier_a", 0), counts.get("tier_b", 0)
    )
    return pkt
