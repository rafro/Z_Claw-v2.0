"""
hard-filter skill — Tier 1 LLM (Qwen2.5 7B via Ollama, 3060 Ti).
The orchestrator LLM scores each job across 5 axes, assigns tier A–D,
and routes resume type. Returns scored + tiered job list.
"""

import json
import logging
from pathlib import Path

from providers.router import ProviderRouter
from providers.base import ProviderError
from runtime.config import ROOT
from runtime.tools.state import load_applications, save_applications, add_to_pipeline

log = logging.getLogger(__name__)

FILTERS_PATH = ROOT / "divisions" / "opportunity" / "job-filters.json"

# ── Scoring prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Opportunity Division orchestrator for J_Claw, a personal AI system.
Your job is to score job listings for Matthew — a freelance developer and trader based in Campbellton, NB.

Matthew's stack: Python, JavaScript, Solidity, Node.js, Web3/DeFi, algorithmic trading, full-stack dev.
He is actively seeking employment. He is precise, technical, and does not want his time wasted.

You will receive a job listing and must return a JSON scoring object. Be strict. Be honest.
A generous Tier A score means Matthew will act on it — do not inflate scores.

Scoring axes (0–10 each):
1. resume_compatibility: How well does this match Matthew's actual stack and experience?
2. compensation_lifestyle_fit: Does pay meet thresholds? Remote-friendly? Lifestyle compatible?
3. interview_probability: Is Matthew realistically likely to get a callback?
4. career_leverage: Does this role open strategic doors, build credibility, or add valued skills?
5. application_complexity: How much effort to apply? (10 = trivially easy, 0 = extremely complex)

Tier rules:
- Tier A: composite ≥ 8.0 — escalate immediately to Matthew
- Tier B: composite 6.0–7.9 — include in next briefing
- Tier C: composite 4.0–5.9 — acceptable but not strategic
- Tier D: composite < 4.0 OR hard-rejected — do not surface

Hard reject immediately (return tier D, score 0) if ANY of:
- Local job under $25/hr
- Retail, trades, healthcare, unrelated career
- Obvious scam or MLM
- Vague posting with no pay and no clear role
- Toronto/GTA role without clear 6-figure trajectory

Resume routing (REQUIRED):
- "technical" → software dev, AI, automation, blockchain/crypto/DeFi/Web3, fintech, trading, technical analyst
- "general" → telecom sales, customer support, call centers, non-technical roles

Return ONLY valid JSON. No explanation, no markdown."""

SCORE_SCHEMA = """{
  "hard_rejected": false,
  "reject_reason": "",
  "scores": {
    "resume_compatibility": 0,
    "compensation_lifestyle_fit": 0,
    "interview_probability": 0,
    "career_leverage": 0,
    "application_complexity": 0
  },
  "score_composite": 0.0,
  "tier": "D",
  "resume": "technical",
  "scoring_notes": ""
}"""


_router = ProviderRouter()

WEIGHTS = {
    "resume_compatibility":       0.25,
    "compensation_lifestyle_fit":  0.25,
    "interview_probability":       0.20,
    "career_leverage":             0.20,
    "application_complexity":      0.10,
}


def _score_job_llm(job: dict, provider) -> dict:
    """Ask the LLM to score a single job. Returns score dict."""
    listing = (
        f"Title: {job.get('title', '')}\n"
        f"Company: {job.get('company', '')}\n"
        f"Location: {job.get('location', '')}\n"
        f"Remote: {job.get('remote', True)}\n"
        f"Pay: min={job.get('pay_min')} max={job.get('pay_max')} "
        f"type={job.get('pay_type')} raw={job.get('salary_raw','')}\n"
        f"Tags: {job.get('tags', '')}\n"
        f"Description: {job.get('description_summary', '')}"
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": f"Score this job listing:\n\n{listing}\n\nReturn JSON matching this schema:\n{SCORE_SCHEMA}"},
    ]

    result = provider.chat_json(messages, temperature=0.05)

    # Recalculate composite from scores to avoid model drift
    if not result.get("hard_rejected") and result.get("scores"):
        s = result["scores"]
        composite = sum(s.get(k, 0) * w for k, w in WEIGHTS.items())
        result["score_composite"] = round(composite, 2)
        # Reassign tier from composite
        if composite >= 8.0:
            result["tier"] = "A"
        elif composite >= 6.0:
            result["tier"] = "B"
        elif composite >= 4.0:
            result["tier"] = "C"
        else:
            result["tier"] = "D"

    return result


def _score_job_deterministic(job: dict) -> dict:
    """
    Rule-based scoring — no LLM.
    Applies hard-reject rules from job-filters.json + simple heuristics.
    Returns Tier C for ambiguous jobs (never lose them).
    provider_used will be "deterministic".
    """
    title = (job.get("title") or "").lower()
    location = (job.get("location") or "").lower()
    pay_min = job.get("pay_min") or 0

    # Load hard-reject rules from job-filters.json
    hard_reject_terms: list[str] = []
    try:
        if FILTERS_PATH.exists():
            fdata = json.loads(FILTERS_PATH.read_text(encoding="utf-8"))
            hard_reject_terms = [t.lower() for t in fdata.get("hard_reject_title_keywords", [])]
    except Exception:
        pass

    # Hard reject checks
    for term in hard_reject_terms:
        if term in title:
            return {
                "hard_rejected": True,
                "reject_reason": f"Hard-reject keyword: '{term}'",
                "scores": {}, "score_composite": 0.0, "tier": "D",
                "resume": "technical", "scoring_notes": "Deterministic fallback",
            }

    # Local job with low pay
    if not job.get("remote") and pay_min and pay_min < 25:
        return {
            "hard_rejected": True,
            "reject_reason": "Local job under $25/hr",
            "scores": {}, "score_composite": 0.0, "tier": "D",
            "resume": "technical", "scoring_notes": "Deterministic fallback",
        }

    # Default: Tier C (ambiguous, keep visible)
    return {
        "hard_rejected": False,
        "reject_reason": "",
        "scores": {k: 5.0 for k in WEIGHTS},
        "score_composite": 5.0,
        "tier": "C",
        "resume": "technical",
        "scoring_notes": "Deterministic fallback — model unavailable",
    }


def _apply_scores(job: dict, score: dict) -> dict:
    """Merge score results back into the job dict."""
    job["filtered"]         = score.get("hard_rejected", False)
    job["tier"]             = score.get("tier", "D")
    job["resume"]           = score.get("resume", "technical")
    job["score_composite"]  = score.get("score_composite", 0.0)
    job["scores"]           = score.get("scores", {})
    job["scoring_notes"]    = score.get("scoring_notes", "")
    if score.get("hard_rejected"):
        job["reject_reason"] = score.get("reject_reason", "")
    return job


# ── Main entry point ──────────────────────────────────────────────────────────

def run(new_jobs: list) -> dict:
    """
    Score and tier all new_jobs. Returns result dict for the orchestrator:
    {
        "scored_jobs": [...],        # all jobs with scores applied
        "tier_a": [...],
        "tier_b": [...],
        "tier_c": [...],
        "tier_d": [...],
        "counts": {...},
        "model_available": bool,
        "provider_used": str,
    }

    Provider chain (via ProviderRouter): ollama:7b → deterministic.
    health-logger and hard-filter are LOCAL ONLY — no cloud fallback by design.
    """
    if not new_jobs:
        return {
            "scored_jobs": [], "tier_a": [], "tier_b": [],
            "tier_c": [], "tier_d": [], "counts": {},
            "model_available": True, "provider_used": "deterministic",
        }

    # Get provider — routing table: hard-filter → [ollama:7b, deterministic]
    provider = _router.get_provider("hard-filter")
    use_llm = provider is not None and provider.provider_id != "deterministic"
    provider_label = provider.provider_id if provider else "deterministic"

    if not use_llm:
        log.warning("hard-filter: no LLM available — using deterministic fallback")

    scored = []
    score_errors = 0

    for job in new_jobs:
        try:
            if use_llm:
                score = _score_job_llm(job, provider)
            else:
                score = _score_job_deterministic(job)
            job = _apply_scores(job, score)
        except (ProviderError, Exception) as e:
            log.error("Scoring failed for job %s: %s", job.get("id"), e)
            # On LLM error, fall back to deterministic for this job
            try:
                score = _score_job_deterministic(job)
                job = _apply_scores(job, score)
            except Exception:
                job["tier"] = "C"
                job["scoring_notes"] = f"Scoring error — fallback Tier C: {e}"
            score_errors += 1
        scored.append(job)

    tier_a = [j for j in scored if j["tier"] == "A"]
    tier_b = [j for j in scored if j["tier"] == "B"]
    tier_c = [j for j in scored if j["tier"] == "C"]
    tier_d = [j for j in scored if j["tier"] == "D"]

    # Save to applications pipeline
    pipeline_jobs = tier_a + tier_b + tier_c
    if pipeline_jobs:
        apps = load_applications()
        apps = add_to_pipeline(apps, pipeline_jobs)
        save_applications(apps)

    log.info(
        "hard-filter: A=%d B=%d C=%d D=%d (errors=%d, provider=%s)",
        len(tier_a), len(tier_b), len(tier_c), len(tier_d), score_errors, provider_label
    )

    return {
        "scored_jobs":     scored,
        "tier_a":          tier_a,
        "tier_b":          tier_b,
        "tier_c":          tier_c,
        "tier_d":          tier_d,
        "model_available": use_llm,
        "provider_used":   provider_label,
        "counts": {
            "total":    len(scored),
            "tier_a":   len(tier_a),
            "tier_b":   len(tier_b),
            "tier_c":   len(tier_c),
            "tier_d":   len(tier_d),
            "errors":   score_errors,
        },
    }
