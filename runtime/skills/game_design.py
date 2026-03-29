"""
game-design skill — Generates and iterates on game design document sections.
Uses LLM to brainstorm mechanics, progression systems, and player loops.
Reads existing GDD state from state/gamedev/gdd.json if it exists.
Tier 1 (7B local).
"""

import json
import logging
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_7B, STATE_DIR
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
MODEL = MODEL_7B
GDD_DIR = STATE_DIR / "gamedev"
GDD_FILE = GDD_DIR / "gdd.json"


def _load_gdd() -> dict:
    """Load the current game design document state, or return empty scaffold."""
    if GDD_FILE.exists():
        try:
            with open(GDD_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load gdd.json: %s", e)
    return {
        "title": "",
        "genre": "",
        "core_loop": "",
        "mechanics": [],
        "progression": {},
        "narrative_hook": "",
        "art_style": "",
        "target_platform": "",
        "sections": [],
    }


def _save_gdd(gdd: dict) -> None:
    """Persist GDD state."""
    GDD_DIR.mkdir(parents=True, exist_ok=True)
    with open(GDD_FILE, "w", encoding="utf-8") as f:
        json.dump(gdd, f, indent=2, ensure_ascii=False)


def run(**kwargs) -> dict:
    """
    Generate or iterate on game design document sections.

    kwargs:
        section (str): GDD section to work on (e.g. "core_loop", "mechanics", "progression").
                        Defaults to a general brainstorm if not specified.
        prompt (str):  Additional context or direction for the design pass.
        genre (str):   Genre hint if starting a new GDD.
    """
    GDD_DIR.mkdir(parents=True, exist_ok=True)

    section = kwargs.get("section", "general")
    prompt = kwargs.get("prompt", "")
    genre = kwargs.get("genre", "")

    gdd = _load_gdd()

    if genre and not gdd.get("genre"):
        gdd["genre"] = genre

    # Build context from existing GDD
    gdd_context = ""
    if gdd.get("title"):
        gdd_context += f"Project: {gdd['title']}\n"
    if gdd.get("genre"):
        gdd_context += f"Genre: {gdd['genre']}\n"
    if gdd.get("core_loop"):
        gdd_context += f"Core Loop: {gdd['core_loop']}\n"
    if gdd.get("mechanics"):
        gdd_context += f"Existing Mechanics: {', '.join(m if isinstance(m, str) else m.get('name', '') for m in gdd['mechanics'])}\n"
    if gdd.get("progression"):
        gdd_context += f"Progression: {json.dumps(gdd['progression'], indent=None)}\n"

    if not is_available(MODEL, host=OLLAMA_HOST):
        summary = f"Game design pass ({section}) — LLM unavailable, GDD state preserved."
        if gdd_context:
            summary += f" Current GDD has {len(gdd.get('mechanics', []))} mechanics defined."
        return {
            "status": "degraded",
            "summary": summary,
            "metrics": {
                "section": section,
                "mechanics_count": len(gdd.get("mechanics", [])),
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    system_prompt = (
        "You are ARDENT, the Game Design lead for Z_Claw. "
        "You write clear, actionable game design document sections. "
        "Focus on: player motivation loops, mechanic interactions, "
        "progression pacing, and emergent gameplay potential. "
        "Be specific — include numbers, formulas, or pseudocode where useful. "
        "Output a structured design proposal with clear headings."
    )

    user_prompt_parts = []
    if gdd_context:
        user_prompt_parts.append(f"Current GDD state:\n{gdd_context}")
    if section != "general":
        user_prompt_parts.append(f"Section to develop: {section}")
    if prompt:
        user_prompt_parts.append(f"Direction: {prompt}")
    if not user_prompt_parts:
        user_prompt_parts.append(
            "Generate a high-level game concept with core loop, "
            "3 key mechanics, and a progression hook. Keep it fresh and original."
        )

    user_prompt = "\n\n".join(user_prompt_parts)

    try:
        response = chat(MODEL, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.4, max_tokens=800, task_type="game-design")
    except Exception as e:
        log.error("game-design LLM call failed: %s", e)
        return {
            "status": "failed",
            "summary": f"Game design LLM call failed: {e}",
            "metrics": {"section": section, "model_available": True},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Store the design output in the GDD
    gdd.setdefault("sections", []).append({
        "section": section,
        "content": response,
        "prompt": prompt,
    })
    _save_gdd(gdd)

    # Trim response for summary (first 300 chars)
    summary_text = response[:300].rsplit(" ", 1)[0] + "..." if len(response) > 300 else response

    return {
        "status": "success",
        "summary": summary_text,
        "design_output": response,
        "metrics": {
            "section": section,
            "mechanics_count": len(gdd.get("mechanics", [])),
            "sections_total": len(gdd.get("sections", [])),
            "output_length": len(response),
            "model_available": True,
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
