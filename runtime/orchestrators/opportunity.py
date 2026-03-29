"""
Opportunity Division Orchestrator.
LLM agent (Qwen2.5 7B) that runs skills, interprets results,
compiles executive packets, and escalates Tier A jobs.
"""

import logging

from providers.router import ProviderRouter
from providers.base import ProviderError
from runtime.skills import job_intake, hard_filter, funding_finder
from runtime import packet
from runtime.tools.xp import grant_skill_xp
from runtime.tools.state import load_applications, save_applications, add_to_pipeline

log = logging.getLogger(__name__)


# ── Orchestrator reasoning ────────────────────────────────────────────────────

def _interpret_results(intake_result: dict, filter_result: dict) -> str:
    """
    Ask the LLM to produce a one-paragraph summary of what happened this run.
    Uses ProviderRouter — ollama:7b primary, gemini fallback. Never Claude.
    """
    c = filter_result.get("counts", {})
    src = intake_result.get("source_status", {})
    ok_sources = [k for k, v in src.items() if v == "ok"]

    # Deterministic fallback summary
    def _fallback() -> str:
        return (
            f"{intake_result['counts']['new']} new listings from "
            f"{', '.join(ok_sources) or 'no sources'}. "
            f"Tier A: {c.get('tier_a',0)}, Tier B: {c.get('tier_b',0)}, "
            f"Tier C: {c.get('tier_c',0)}."
        )

    provider = ProviderRouter().get_provider("hard-filter")
    if provider is None or provider.provider_id == "deterministic":
        return _fallback()

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

    try:
        result = provider.chat(messages, temperature=0.2, max_tokens=150)
        lines = result.strip().splitlines()
        if lines and lines[0].rstrip().endswith(":"):
            result = "\n".join(lines[1:]).lstrip()
        return result
    except ProviderError as e:
        log.warning("Opportunity orchestrator LLM failed (%s): %s — using fallback", provider.provider_id, e)
        return _fallback()


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

    # Step 1b: Stage new jobs as pending_review before scoring so dashboard shows them
    if intake["new_jobs"]:
        apps = load_applications()
        staged = [dict(j, status="pending_review", score=None, tier=None) for j in intake["new_jobs"]]
        apps = add_to_pipeline(apps, staged)
        save_applications(apps)
        log.info("Staged %d new jobs to applications.json", len(intake["new_jobs"]))

    # Step 2: Score + tier (LLM)
    filtered = hard_filter.run(intake["new_jobs"])

    # Step 3: Orchestrator synthesizes a summary
    summary = _interpret_results(intake, filtered)

    # Step 4: Build action items for Tier A + B
    action_items = []
    for job in filtered.get("tier_a", []) + filtered.get("tier_b", []):
        action_items.append(packet.job_action_item(job))

    # Step 4b: Burnout throttle — cap Tier A escalations during high burnout
    burnout_high = False
    try:
        from pathlib import Path
        import json as _json
        bm_path = Path("divisions/personal/packets/burnout-monitor.json")
        if bm_path.exists():
            bm = _json.loads(bm_path.read_text())
            if bm.get("escalate"):
                burnout_high = True
                tier_a_list = filtered.get("tier_a", [])
                if len(tier_a_list) > 2:
                    log.warning(
                        "Burnout throttle: capping Tier A from %d to 2",
                        len(tier_a_list),
                    )
                    filtered["tier_a"] = tier_a_list[:2]
                    filtered["counts"]["tier_a"] = len(filtered["tier_a"])
    except Exception:
        pass

    # Step 5: Determine escalation (Tier A found)
    escalate = len(filtered.get("tier_a", [])) > 0
    escalation_reason = ""
    if escalate:
        n = len(filtered["tier_a"])
        escalation_reason = f"{n} Tier A job{'s' if n > 1 else ''} found requiring Matthew's review."
        if burnout_high:
            escalation_reason += " (Burnout throttle active — showing top 2 only.)"

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
    provider_used = filtered.get("provider_used", "ollama" if filtered.get("model_available") else "deterministic")
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
        provider_used=provider_used,
    )

    packet.write(pkt)
    grant_skill_xp("job-intake")
    grant_skill_xp("hard-filter")
    log.info(
        "Opportunity packet written. Status=%s Escalate=%s A=%d B=%d",
        status, escalate, counts.get("tier_a", 0), counts.get("tier_b", 0)
    )
    return pkt


def run_funding_finder() -> dict:
    """Daily 14:00 — scan grant sources, score opportunities, write packet."""
    log.info("=== Opportunity Division: funding-finder run ===")

    result = funding_finder.run()

    if result["all_failed"]:
        pkt = packet.build(
            division="opportunity",
            skill="funding-finder",
            status="failed",
            summary="All funding sources failed.",
            escalate=True,
            escalation_reason=f"Sources failed: {result['source_errors']}",
        )
        packet.write(pkt)
        return pkt

    opps   = result["opportunities"]
    counts = result["counts"]

    # Build action items for qualifying opportunities
    action_items = []
    for opp in sorted(opps, key=lambda o: o.get("composite", 0), reverse=True):
        amount   = opp.get("amount", "amount unspecified")
        deadline = opp.get("deadline", "no deadline listed")
        score    = opp.get("composite", 0)
        action_items.append(packet.action_item(
            f"[{score:.1f}/10] {opp['name']} | {amount} | Deadline: {deadline} "
            f"| {opp.get('eligibility_notes','')} | {opp.get('url', opp.get('source',''))}",
            priority="medium",
            requires_matthew=True,
        ))

    if opps:
        top = opps[0]
        summary = (
            f"{counts['opportunities_found']} new funding opportunit"
            f"{'y' if counts['opportunities_found'] == 1 else 'ies'} found. "
            f"Top: {top['name']} — {top.get('amount','?')} (score {top.get('composite',0):.1f}/10)."
        )
    else:
        summary = "No new funding opportunities found this run."

    pkt = packet.build(
        division="opportunity",
        skill="funding-finder",
        status="success" if not result["source_errors"] else "partial",
        summary=summary,
        action_items=action_items,
        metrics={
            "funding_opportunities": counts["opportunities_found"],
            "sources_failed":        counts["sources_failed"],
            "model_available":       result["model_available"],
        },
        artifact_refs=[{"bundle_id": f"funding-{__import__('datetime').date.today()}", "location": "hot"}],
    )

    packet.write(pkt)
    grant_skill_xp("funding-finder")
    log.info("Funding-finder packet written. Found=%d", counts["opportunities_found"])
    return pkt
