"""
character-designer skill — Creates RPG character sheets with stats, abilities,
backstory, and sprite specs.  Uses LLM to generate rich character data and
saves each character as JSON in state/gamedev/characters/.
Tier 1 (7B local).
"""

import json
import logging
import re
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_7B, STATE_DIR
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
MODEL = MODEL_7B
GAMEDEV_DIR = STATE_DIR / "gamedev"
CHARACTERS_DIR = GAMEDEV_DIR / "characters"
GDD_FILE = GAMEDEV_DIR / "gdd.json"

VALID_ROLES = {"hero", "npc", "villain"}
VALID_CLASSES = {
    "warrior", "mage", "rogue", "ranger", "cleric",
    "paladin", "necromancer", "bard", "monk", "assassin",
}


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_gdd() -> dict:
    """Load the current GDD for genre/art_style context."""
    if GDD_FILE.exists():
        try:
            with open(GDD_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load gdd.json: %s", e)
    return {}


def _save_character(character: dict) -> Path:
    """Persist a character sheet and return the file path."""
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    name = character.get("name", "unnamed")
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unnamed"
    path = CHARACTERS_DIR / f"{slug}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(character, f, indent=2, ensure_ascii=False)
    return path


def _parse_json_from_response(text: str) -> dict | None:
    """Try to extract a JSON object from LLM output."""
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extracting from markdown code fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try finding the outermost braces
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


def _build_default_character(name: str, role: str, class_type: str) -> dict:
    """Fallback character sheet when LLM is unavailable."""
    return {
        "name": name or "Unknown Hero",
        "role": role or "hero",
        "class_type": class_type or "warrior",
        "stats": {
            "hp": 100, "mp": 50, "attack": 12, "defense": 10,
            "speed": 8, "luck": 5,
        },
        "abilities": [
            {
                "name": "Basic Strike",
                "type": "physical",
                "description": "A standard melee attack.",
                "mp_cost": 0,
                "cooldown": 0,
                "damage_formula": "attack * 1.0",
            }
        ],
        "backstory": "",
        "personality": "",
        "motivation": "",
        "visual_description": "",
        "sprite_spec": {
            "style": "pixel-art",
            "palette": [],
            "pose": "idle",
            "size": "32x32",
        },
        "dialogue_style": "",
        "voice_tone": "",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(**kwargs) -> dict:
    """
    Generate an RPG character sheet.

    kwargs:
        name (str):       Character name.  If omitted, LLM picks one.
        role (str):       hero | npc | villain.
        class_type (str): warrior | mage | rogue | etc.
        prompt (str):     Extra creative direction for the LLM.
    """
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)

    name = kwargs.get("name", "")
    role = kwargs.get("role", "hero")
    class_type = kwargs.get("class_type", "warrior")
    prompt = kwargs.get("prompt", "")

    # Normalise enums
    if role not in VALID_ROLES:
        role = "hero"
    if class_type not in VALID_CLASSES:
        class_type = "warrior"

    gdd = _load_gdd()
    genre = gdd.get("genre", "fantasy RPG")
    art_style = gdd.get("art_style", "pixel-art")

    # ── Ollama unavailable — graceful degradation ──────────────────────
    if not is_available(MODEL, host=OLLAMA_HOST):
        character = _build_default_character(name, role, class_type)
        path = _save_character(character)
        return {
            "status": "degraded",
            "summary": (
                f"Character '{character['name']}' created with defaults — "
                "LLM unavailable, stats are placeholder."
            ),
            "metrics": {
                "character_name": character["name"],
                "role": role,
                "class_type": class_type,
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": ["Re-run when Ollama is back for richer output."],
        }

    # ── Build prompts ──────────────────────────────────────────────────
    system_prompt = (
        "You are ARDENT, the Character Design lead for Z_Claw. "
        "You create detailed RPG character sheets as pure JSON. "
        "Output ONLY a single valid JSON object — no markdown, no commentary. "
        "The JSON must contain exactly these keys: "
        "name, role, class_type, stats (object with hp/mp/attack/defense/speed/luck), "
        "abilities (array of objects each with name/type/description/mp_cost/cooldown/damage_formula), "
        "backstory, personality, motivation, visual_description, "
        "sprite_spec (object with style/palette/pose/size), dialogue_style, voice_tone."
    )

    user_parts = [f"Game genre: {genre}", f"Art style: {art_style}"]
    if name:
        user_parts.append(f"Character name: {name}")
    else:
        user_parts.append(
            "Generate an original character name that fits the game's genre."
        )
    user_parts.append(f"Role: {role}")
    user_parts.append(f"Class: {class_type}")
    if prompt:
        user_parts.append(f"Creative direction: {prompt}")
    user_parts.append(
        "Provide 3-5 unique abilities with creative damage formulas. "
        "Make the backstory 2-3 sentences and the visual description vivid enough "
        "for an artist to work from."
    )

    user_prompt = "\n".join(user_parts)

    # ── LLM call ──────────────────────────────────────────────────────
    try:
        response = chat(
            MODEL,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.6,
            max_tokens=1200,
            task_type="character-designer",
        )
    except Exception as e:
        log.error("character-designer LLM call failed: %s", e)
        character = _build_default_character(name, role, class_type)
        path = _save_character(character)
        return {
            "status": "failed",
            "summary": f"LLM call failed ({e}); saved placeholder character.",
            "metrics": {
                "character_name": character["name"],
                "role": role,
                "class_type": class_type,
                "model_available": True,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": ["Investigate Ollama error and retry."],
        }

    # ── Parse response ────────────────────────────────────────────────
    character = _parse_json_from_response(response)
    if character is None:
        log.warning("character-designer: could not parse JSON, using fallback")
        character = _build_default_character(name, role, class_type)
        character["_raw_llm_output"] = response[:2000]
    else:
        # Ensure required keys exist with sane defaults
        character.setdefault("name", name or "Unknown Hero")
        character.setdefault("role", role)
        character.setdefault("class_type", class_type)
        character.setdefault("stats", {
            "hp": 100, "mp": 50, "attack": 12, "defense": 10,
            "speed": 8, "luck": 5,
        })
        character.setdefault("abilities", [])
        character.setdefault("backstory", "")
        character.setdefault("personality", "")
        character.setdefault("motivation", "")
        character.setdefault("visual_description", "")
        character.setdefault("sprite_spec", {
            "style": art_style, "palette": [], "pose": "idle", "size": "32x32",
        })
        character.setdefault("dialogue_style", "")
        character.setdefault("voice_tone", "")

    path = _save_character(character)
    char_name = character.get("name", "Unknown")
    abilities_count = len(character.get("abilities", []))

    return {
        "status": "success",
        "summary": (
            f"Character '{char_name}' ({role} {class_type}) created with "
            f"{abilities_count} abilities. Saved to {path.name}."
        ),
        "metrics": {
            "character_name": char_name,
            "role": role,
            "class_type": class_type,
            "abilities_count": abilities_count,
            "output_length": len(response),
            "model_available": True,
            "file": str(path),
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
