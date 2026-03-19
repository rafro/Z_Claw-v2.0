"""
hard-filter skill — Tier 1 LLM (Qwen2.5 7B via Ollama, 3060 Ti).
The orchestrator LLM scores each job across 5 axes, assigns tier A–D,
and routes resume type. Returns scored + tiered job list.
"""

import json
import logging
from pathlib import Path

from runtime.config import SKILL_MODELS, ROOT
from runtime.ollama_client import chat_json, is_available
from runtime.tools.state import load_applications, save_applications, add_to_pipeline

log = logging.getLogger(__name__)

MODEL = SKILL_MODELS["hard-filter"]
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


def _score_job(job: dict) -> dict:
    """Ask the LLM to score a single job. Returns score dict."""
    weights = {
        "resume_compatibility":      0.25,
        "compensation_lifestyle_fit": 0.25,
        "interview_probability":      0.20,
        "career_leverage":            0.20,
        "application_complexity":     0.10,
    }

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

    result = chat_json(MODEL, messages, temperature=0.05)

    # Recalculate composite from scores to avoid model drift
    if not result.get("hard_rejected") and result.get("scores"):
        s = result["scores"]
        composite = sum(s.get(k, 0) * w for k, w in weights.items())
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
        "model_available": bool
    }
    """
    if not new_jobs:
        return {
            "scored_jobs": [], "tier_a": [], "tier_b": [],
            "tier_c": [], "tier_d": [], "counts": {},
            "model_available": True,
        }

    if not is_available(MODEL):
        log.error("hard-filter: model %s not available in Ollama", MODEL)
        # Fallback: mark all as Tier C so they don't disappear
        for job in new_jobs:
            job["tier"] = "C"
            job["scoring_notes"] = "Unscored — model unavailable"
        return {
            "scored_jobs": new_jobs,
            "tier_a": [], "tier_b": [], "tier_c": new_jobs, "tier_d": [],
            "counts": {"total": len(new_jobs), "scored": 0, "fallback_c": len(new_jobs)},
            "model_available": False,
        }

    scored = []
    score_errors = 0

    for job in new_jobs:
        try:
            score = _score_job(job)
            job = _apply_scores(job, score)
        except Exception as e:
            log.error("Scoring failed for job %s: %s", job.get("id"), e)
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
        "hard-filter: A=%d B=%d C=%d D=%d (errors=%d)",
        len(tier_a), len(tier_b), len(tier_c), len(tier_d), score_errors
    )

    return {
        "scored_jobs":     scored,
        "tier_a":          tier_a,
        "tier_b":          tier_b,
        "tier_c":          tier_c,
        "tier_d":          tier_d,
        "model_available": True,
        "counts": {
            "total":    len(scored),
            "tier_a":   len(tier_a),
            "tier_b":   len(tier_b),
            "tier_c":   len(tier_c),
            "tier_d":   len(tier_d),
            "errors":   score_errors,
        },
    }
