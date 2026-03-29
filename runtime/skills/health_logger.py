"""
health-logger skill — Tier 1 LLM (Llama 3.1 8B, LOCAL ONLY).
Parses Matthew's free-form Telegram health reply into structured JSON.
Health/medication data NEVER leaves the machine.

Note: This skill expects the Telegram interaction to have already happened
via the gateway. It receives the reply text and parses it.
"""

import logging
from datetime import date, datetime, timezone

from runtime.config import SKILL_MODELS
from runtime.ollama_client import chat_json, is_available
from runtime.tools.state import (
    load_health_log, save_health_log, append_health_entry
)
from runtime.config import ROOT

log = logging.getLogger(__name__)
MODEL = SKILL_MODELS["health-logger"]   # Llama 3.1 8B — privacy, stays local
HOT_DIR = ROOT / "divisions" / "personal" / "hot"

PARSE_PROMPT = """You are the Personal Division orchestrator for J_Claw.
Parse the user's health check-in reply and extract structured data.
Be liberal in parsing — infer reasonable values from partial information.

Return ONLY valid JSON with exactly these fields:
{
  "food": [],
  "hydration": "",
  "adderall_dose": "",
  "adderall_time": "",
  "exercise_type": "",
  "exercise_duration_min": null,
  "sleep_hours": null,
  "sleep_quality": null,
  "skipped": false,
  "parse_notes": ""
}

Rules:
- If reply is "skip" or "s": set skipped=true, all other fields null/empty
- sleep_hours: number (e.g. "7h" → 7, "6.5" → 6.5)
- sleep_quality: number 1-10
- exercise_duration_min: integer minutes or null
- adderall_dose: string like "20mg", "40mg", or "" if not mentioned
- hydration: string like "2L", "~1.5L", "not much"
- food: array of strings, each a meal description
- parse_notes: note any ambiguity or missing required fields"""


def parse_reply(reply_text: str) -> dict:
    """Use local LLM to parse health reply. Never sends data externally."""
    if not is_available(MODEL):
        log.error("health-logger: model %s not available — cannot parse", MODEL)
        return {
            "food": [], "hydration": "", "adderall_dose": "",
            "adderall_time": "", "exercise_type": "", "exercise_duration_min": None,
            "sleep_hours": None, "sleep_quality": None,
            "skipped": False, "parse_notes": "Model unavailable — entry not parsed",
        }

    messages = [
        {"role": "system", "content": PARSE_PROMPT},
        {"role": "user",   "content": f"Health check-in reply:\n{reply_text}"},
    ]
    try:
        return chat_json(MODEL, messages, temperature=0.05, max_tokens=512, task_type="health-logger")
    except Exception as e:
        log.error("health-logger parse failed: %s", e)
        return {
            "food": [], "hydration": "", "adderall_dose": "",
            "adderall_time": "", "exercise_type": "", "exercise_duration_min": None,
            "sleep_hours": None, "sleep_quality": None,
            "skipped": False, "parse_notes": f"Parse error: {e}",
        }


def run(reply_text: str) -> dict:
    """
    Parse and save a health log entry.
    Returns result dict for the personal orchestrator.
    """
    today = date.today().isoformat()
    now   = datetime.now(timezone.utc).isoformat()

    parsed = parse_reply(reply_text)

    entry = {
        "date":                 today,
        "logged_at":            now,
        "skipped":              parsed.get("skipped", False),
        "food":                 parsed.get("food", []),
        "hydration":            parsed.get("hydration", ""),
        "adderall_dose":        parsed.get("adderall_dose", ""),
        "adderall_time":        parsed.get("adderall_time", ""),
        "exercise_type":        parsed.get("exercise_type", ""),
        "exercise_duration_min": parsed.get("exercise_duration_min"),
        "sleep_hours":          parsed.get("sleep_hours"),
        "sleep_quality":        parsed.get("sleep_quality"),
    }

    # Save to state/health-log.json
    hl = load_health_log()
    hl = append_health_entry(hl, entry)
    save_health_log(hl)

    # Save to hot cache (division artifact)
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    import json
    hot_path = HOT_DIR / f"health-{today}.json"
    with open(hot_path, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2)

    # Build summary (safe for Telegram — no raw medication data)
    if entry["skipped"]:
        summary = f"Health log: skipped for {today}"
    else:
        sleep_str = f"Sleep: {entry['sleep_hours']}h / quality {entry['sleep_quality']}/10" \
                    if entry['sleep_hours'] else "Sleep: not logged"
        ex_str = entry["exercise_type"] or "no exercise"
        summary = f"Health logged | {sleep_str} | Exercise: {ex_str}"

    missing_required = not entry["sleep_hours"] and not entry["skipped"]

    return {
        "entry":            entry,
        "summary":          summary,
        "missing_required": missing_required,
        "model_available":  is_available(MODEL),
        "status":           "success" if not missing_required else "partial",
    }
