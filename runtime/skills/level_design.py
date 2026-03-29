"""
level-design skill — Generates procedural level layout suggestions.
Uses LLM to create level specs with enemy placement, hazards, and pacing.
Returns structured level data.
Tier 1 (7B local).
"""

import json
import logging
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_7B, STATE_DIR
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
MODEL = MODEL_7B
GAMEDEV_DIR = STATE_DIR / "gamedev"
LEVELS_DIR = GAMEDEV_DIR / "levels"


def _load_gdd_context() -> str:
    """Load relevant GDD context for level generation."""
    gdd_file = GAMEDEV_DIR / "gdd.json"
    if not gdd_file.exists():
        return ""
    try:
        with open(gdd_file, encoding="utf-8") as f:
            gdd = json.load(f)
        parts = []
        if gdd.get("genre"):
            parts.append(f"Genre: {gdd['genre']}")
        if gdd.get("core_loop"):
            parts.append(f"Core loop: {gdd['core_loop']}")
        if gdd.get("mechanics"):
            mechs = [m if isinstance(m, str) else m.get("name", "") for m in gdd["mechanics"]]
            parts.append(f"Mechanics: {', '.join(mechs)}")
        return "\n".join(parts)
    except Exception:
        return ""


def _load_existing_levels() -> list[str]:
    """List existing level files to avoid regenerating."""
    if not LEVELS_DIR.exists():
        return []
    return [p.stem for p in LEVELS_DIR.glob("*.json")]


def _save_level(name: str, level_data: dict) -> None:
    """Persist a level spec."""
    LEVELS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = name.lower().replace(" ", "-").replace("/", "-")[:60]
    out_file = LEVELS_DIR / f"{safe_name}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(level_data, f, indent=2, ensure_ascii=False)


def run(**kwargs) -> dict:
    """
    Generate a level layout suggestion.

    kwargs:
        level_name (str):   Name or identifier for the level.
        theme (str):        Level theme (e.g. "forest", "dungeon", "space station").
        difficulty (str):   Target difficulty — "easy", "medium", "hard", "boss".
        constraints (str):  Additional design constraints.
        count (int):        Number of level variants to generate (default 1).
    """
    GAMEDEV_DIR.mkdir(parents=True, exist_ok=True)

    level_name = kwargs.get("level_name", "")
    theme = kwargs.get("theme", "")
    difficulty = kwargs.get("difficulty", "medium")
    constraints = kwargs.get("constraints", "")
    count = min(kwargs.get("count", 1), 5)  # cap at 5

    gdd_context = _load_gdd_context()
    existing = _load_existing_levels()

    if not is_available(MODEL, host=OLLAMA_HOST):
        return {
            "status": "degraded",
            "summary": f"Level design — LLM unavailable. {len(existing)} existing level(s) on file.",
            "metrics": {
                "existing_levels": len(existing),
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    system_prompt = (
        "You are a level designer for a game development team. "
        "Generate a detailed level specification including:\n"
        "1. LAYOUT: Room/area structure with connections (graph or linear).\n"
        "2. ENEMIES: Types, counts, and placement strategy per area.\n"
        "3. HAZARDS: Environmental challenges and traps.\n"
        "4. PACING: Intensity curve — when does tension rise/fall?\n"
        "5. REWARDS: Loot, secrets, checkpoints placement.\n"
        "6. NARRATIVE BEATS: Optional story moments or environmental storytelling.\n"
        "7. ESTIMATED PLAYTIME: In minutes.\n"
        "Be specific with numbers and layouts. Think about flow and player psychology."
    )

    user_parts = []
    if gdd_context:
        user_parts.append(f"Game context:\n{gdd_context}")
    if level_name:
        user_parts.append(f"Level name: {level_name}")
    if theme:
        user_parts.append(f"Theme: {theme}")
    user_parts.append(f"Difficulty: {difficulty}")
    if constraints:
        user_parts.append(f"Constraints: {constraints}")
    if existing:
        user_parts.append(f"Existing levels (avoid duplicating): {', '.join(existing[:10])}")
    if count > 1:
        user_parts.append(f"Generate {count} variant(s).")

    user_prompt = "\n".join(user_parts)

    try:
        response = chat(MODEL, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.5, max_tokens=900, task_type="level-design")
    except Exception as e:
        log.error("level-design LLM call failed: %s", e)
        return {
            "status": "failed",
            "summary": f"Level design LLM call failed: {e}",
            "metrics": {"model_available": True},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Save the level
    save_name = level_name or theme or f"level-{len(existing) + 1}"
    level_data = {
        "name": save_name,
        "theme": theme,
        "difficulty": difficulty,
        "constraints": constraints,
        "specification": response,
    }
    _save_level(save_name, level_data)

    summary_text = (
        f"Level '{save_name}' ({difficulty}): "
        + (response[:200].rsplit(" ", 1)[0] + "..." if len(response) > 200 else response)
    )

    return {
        "status": "success",
        "summary": summary_text,
        "level_spec": response,
        "metrics": {
            "level_name": save_name,
            "theme": theme,
            "difficulty": difficulty,
            "existing_levels": len(existing),
            "output_length": len(response),
            "model_available": True,
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
