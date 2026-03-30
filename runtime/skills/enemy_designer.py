"""
enemy-designer skill — Creates enemy and boss catalogs with stats, attacks,
behavior AI, loot tables, and boss phase transitions.  Saves individual
enemies and maintains an index at state/gamedev/enemies/_index.json.
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
ENEMIES_DIR = GAMEDEV_DIR / "enemies"
INDEX_FILE = ENEMIES_DIR / "_index.json"
GDD_FILE = GAMEDEV_DIR / "gdd.json"

VALID_ENEMY_TYPES = {"minion", "elite", "boss", "mini-boss"}
VALID_DIFFICULTIES = {"easy", "medium", "hard", "nightmare"}
BOSS_TYPES = {"boss", "mini-boss"}


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


def _load_index() -> dict:
    """Load the enemy index or return a fresh one."""
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load enemies _index.json: %s", e)
    return {"enemies": []}


def _save_index(index: dict) -> None:
    """Persist the enemy index."""
    ENEMIES_DIR.mkdir(parents=True, exist_ok=True)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)


def _save_enemy(enemy: dict) -> Path:
    """Persist a single enemy and return the file path."""
    ENEMIES_DIR.mkdir(parents=True, exist_ok=True)
    name = enemy.get("name", "unnamed_enemy")
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unnamed_enemy"
    path = ENEMIES_DIR / f"{slug}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(enemy, f, indent=2, ensure_ascii=False)
    return path


def _update_index(enemies: list[dict]) -> None:
    """Add enemies to the index, avoiding duplicates by name."""
    index = _load_index()
    existing_names = {e["name"].lower() for e in index["enemies"]}
    for enemy in enemies:
        enemy_name = enemy.get("name", "")
        if enemy_name.lower() not in existing_names:
            index["enemies"].append({
                "name": enemy_name,
                "enemy_type": enemy.get("enemy_type", "minion"),
                "difficulty": enemy.get("difficulty", "medium"),
            })
            existing_names.add(enemy_name.lower())
    _save_index(index)


def _parse_json_from_response(text: str):
    """Try to extract a JSON object or array from LLM output."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Code fence
    m = re.search(r"```(?:json)?\s*([\[\{].*?[\]\}])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Outermost braces/brackets
    for pattern in [r"\[.*\]", r"\{.*\}"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return None


def _difficulty_multiplier(difficulty: str) -> dict:
    """Stat multipliers by difficulty tier."""
    return {
        "easy":      {"hp": 0.7, "attack": 0.7, "defense": 0.7, "speed": 0.8},
        "medium":    {"hp": 1.0, "attack": 1.0, "defense": 1.0, "speed": 1.0},
        "hard":      {"hp": 1.5, "attack": 1.3, "defense": 1.3, "speed": 1.2},
        "nightmare": {"hp": 2.5, "attack": 1.8, "defense": 1.6, "speed": 1.4},
    }.get(difficulty, {"hp": 1.0, "attack": 1.0, "defense": 1.0, "speed": 1.0})


def _build_default_enemy(
    name: str, enemy_type: str, difficulty: str,
) -> dict:
    """Fallback enemy when LLM is unavailable."""
    mults = _difficulty_multiplier(difficulty)
    is_boss = enemy_type in BOSS_TYPES
    base_hp = 500 if is_boss else 80

    enemy = {
        "name": name or ("Unnamed Boss" if is_boss else "Unnamed Minion"),
        "enemy_type": enemy_type,
        "difficulty": difficulty,
        "stats": {
            "hp": int(base_hp * mults["hp"]),
            "attack": int(15 * mults["attack"]),
            "defense": int(10 * mults["defense"]),
            "speed": int(8 * mults["speed"]),
        },
        "behavior": {
            "ai_type": "aggressive" if is_boss else "patrol",
            "patrol_pattern": "none" if is_boss else "linear",
            "aggro_range": 10 if is_boss else 5,
        },
        "attacks": [
            {
                "name": "Basic Attack",
                "damage": int(15 * mults["attack"]),
                "type": "physical",
                "cooldown": 0,
                "description": "A standard melee strike.",
            }
        ],
        "phases": [],
        "loot_table": [
            {"item": "Gold Coins", "drop_chance": 1.0},
        ],
        "spawn_rules": {
            "location": "any",
            "max_count": 1 if is_boss else 5,
            "respawn_time": -1 if is_boss else 60,
        },
        "visual_description": "",
        "sprite_spec": {
            "style": "pixel-art",
            "palette": [],
            "pose": "idle",
            "size": "64x64" if is_boss else "32x32",
        },
        "backstory": "",
        "defeat_dialogue": "",
    }

    # Give bosses default phases
    if is_boss:
        enemy["phases"] = [
            {
                "trigger": "hp < 50%",
                "behavior_change": "enraged — faster attacks, higher damage",
                "new_attacks": [
                    {
                        "name": "Enraged Slam",
                        "damage": int(25 * mults["attack"]),
                        "type": "physical",
                        "cooldown": 3,
                        "description": "A powerful ground slam.",
                    }
                ],
                "dialogue": "You think you can defeat me?!",
            }
        ]

    return enemy


def _ensure_enemy_schema(
    enemy: dict, enemy_type: str, difficulty: str,
) -> dict:
    """Fill in any missing keys with safe defaults."""
    is_boss = enemy_type in BOSS_TYPES
    mults = _difficulty_multiplier(difficulty)

    enemy.setdefault("name", "Unnamed Enemy")
    enemy.setdefault("enemy_type", enemy_type)
    enemy.setdefault("difficulty", difficulty)
    enemy.setdefault("stats", {
        "hp": int(100 * mults["hp"]),
        "attack": int(15 * mults["attack"]),
        "defense": int(10 * mults["defense"]),
        "speed": int(8 * mults["speed"]),
    })
    enemy.setdefault("behavior", {
        "ai_type": "aggressive" if is_boss else "patrol",
        "patrol_pattern": "none" if is_boss else "linear",
        "aggro_range": 10 if is_boss else 5,
    })
    enemy.setdefault("attacks", [])
    enemy.setdefault("phases", [])
    enemy.setdefault("loot_table", [])
    enemy.setdefault("spawn_rules", {
        "location": "any",
        "max_count": 1 if is_boss else 5,
        "respawn_time": -1 if is_boss else 60,
    })
    enemy.setdefault("visual_description", "")
    enemy.setdefault("sprite_spec", {
        "style": "pixel-art",
        "palette": [],
        "pose": "idle",
        "size": "64x64" if is_boss else "32x32",
    })
    enemy.setdefault("backstory", "")
    enemy.setdefault("defeat_dialogue", "")
    return enemy


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(**kwargs) -> dict:
    """
    Design enemies and bosses for the game.

    kwargs:
        name (str):        Enemy name.  If omitted, LLM picks one.
        enemy_type (str):  minion | elite | boss | mini-boss.
        difficulty (str):  easy | medium | hard | nightmare.
        prompt (str):      Extra creative direction for the LLM.
    """
    ENEMIES_DIR.mkdir(parents=True, exist_ok=True)

    name = kwargs.get("name", "")
    enemy_type = kwargs.get("enemy_type", "minion")
    difficulty = kwargs.get("difficulty", "medium")
    prompt = kwargs.get("prompt", "")

    if enemy_type not in VALID_ENEMY_TYPES:
        enemy_type = "minion"
    if difficulty not in VALID_DIFFICULTIES:
        difficulty = "medium"

    is_boss = enemy_type in BOSS_TYPES
    gdd = _load_gdd()
    genre = gdd.get("genre", "fantasy RPG")
    art_style = gdd.get("art_style", "pixel-art")

    # ── Ollama unavailable — graceful degradation ──────────────────────
    if not is_available(MODEL, host=OLLAMA_HOST):
        enemy = _build_default_enemy(name, enemy_type, difficulty)
        path = _save_enemy(enemy)
        _update_index([enemy])
        return {
            "status": "degraded",
            "summary": (
                f"Enemy '{enemy['name']}' created with defaults — "
                "LLM unavailable, stats are placeholder."
            ),
            "metrics": {
                "enemy_name": enemy["name"],
                "enemy_type": enemy_type,
                "difficulty": difficulty,
                "is_boss": is_boss,
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": ["Re-run when Ollama is back for richer output."],
        }

    # ── Build prompts ──────────────────────────────────────────────────
    phase_instruction = ""
    if is_boss:
        phase_instruction = (
            "This is a boss-type enemy. Include 2-3 phases in the 'phases' array. "
            "Each phase must have: trigger (HP percentage threshold like 'hp < 75%'), "
            "behavior_change (description of how the boss changes), "
            "new_attacks (array of attack objects unlocked in this phase), "
            "and dialogue (what the boss says when entering this phase). "
            "Make each phase feel like a meaningful escalation."
        )
    else:
        phase_instruction = (
            "This is a regular enemy. The 'phases' array should be empty."
        )

    system_prompt = (
        "You are ARDENT, the Enemy Design lead for Z_Claw. "
        "You create detailed enemy data as pure JSON. "
        "Output ONLY a single valid JSON object — no markdown, no commentary. "
        "The JSON must contain exactly these keys: "
        "name, enemy_type, difficulty, "
        "stats (object with hp/attack/defense/speed), "
        "behavior (object with ai_type/patrol_pattern/aggro_range), "
        "attacks (array of objects each with name/damage/type/cooldown/description), "
        "phases (array — see instructions), "
        "loot_table (array of objects with item/drop_chance), "
        "spawn_rules (object with location/max_count/respawn_time), "
        "visual_description, sprite_spec (object with style/palette/pose/size), "
        "backstory, defeat_dialogue. "
        f"{phase_instruction}"
    )

    user_parts = [
        f"Game genre: {genre}",
        f"Art style: {art_style}",
        f"Enemy type: {enemy_type}",
        f"Difficulty: {difficulty}",
    ]
    if name:
        user_parts.append(f"Enemy name: {name}")
    else:
        user_parts.append(
            "Generate an original enemy name that fits the game's genre and "
            "this enemy type."
        )
    if prompt:
        user_parts.append(f"Creative direction: {prompt}")

    user_parts.append(
        "Include 3-5 attacks with varied damage types (physical, magical, etc). "
        "The loot table should have 2-4 items with realistic drop chances (0.0-1.0). "
        "Make the visual description vivid enough for a sprite artist. "
        "Backstory should be 2-3 sentences."
    )

    if is_boss:
        user_parts.append(
            "As a boss, give it a memorable defeat_dialogue (1-2 lines the boss "
            "says when defeated). Scale HP and damage to feel like a boss fight."
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
            temperature=0.55,
            max_tokens=1500,
            task_type="enemy-designer",
        )
    except Exception as e:
        log.error("enemy-designer LLM call failed: %s", e)
        enemy = _build_default_enemy(name, enemy_type, difficulty)
        path = _save_enemy(enemy)
        _update_index([enemy])
        return {
            "status": "failed",
            "summary": f"LLM call failed ({e}); saved placeholder enemy.",
            "metrics": {
                "enemy_name": enemy["name"],
                "enemy_type": enemy_type,
                "difficulty": difficulty,
                "is_boss": is_boss,
                "model_available": True,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": ["Investigate Ollama error and retry."],
        }

    # ── Parse response ────────────────────────────────────────────────
    parsed = _parse_json_from_response(response)

    if parsed is None:
        log.warning("enemy-designer: could not parse JSON, using fallback")
        enemy = _build_default_enemy(name, enemy_type, difficulty)
        enemy["_raw_llm_output"] = response[:2000]
        enemies = [enemy]
    elif isinstance(parsed, list):
        # LLM returned an array of enemies (unexpected but handle it)
        enemies = [
            _ensure_enemy_schema(e, enemy_type, difficulty)
            for e in parsed if isinstance(e, dict)
        ]
    elif isinstance(parsed, dict):
        # Could be {"enemies": [...]} wrapper or a single enemy
        if "enemies" in parsed and isinstance(parsed["enemies"], list):
            enemies = [
                _ensure_enemy_schema(e, enemy_type, difficulty)
                for e in parsed["enemies"] if isinstance(e, dict)
            ]
        else:
            enemies = [_ensure_enemy_schema(parsed, enemy_type, difficulty)]
    else:
        enemy = _build_default_enemy(name, enemy_type, difficulty)
        enemies = [enemy]

    if not enemies:
        enemy = _build_default_enemy(name, enemy_type, difficulty)
        enemies = [enemy]

    # ── Validate boss phases ──────────────────────────────────────────
    for enemy in enemies:
        etype = enemy.get("enemy_type", enemy_type)
        if etype in BOSS_TYPES and not enemy.get("phases"):
            # Ensure bosses always have at least one phase
            mults = _difficulty_multiplier(enemy.get("difficulty", difficulty))
            enemy["phases"] = [
                {
                    "trigger": "hp < 50%",
                    "behavior_change": "enraged — faster attacks, higher damage",
                    "new_attacks": [
                        {
                            "name": "Desperate Strike",
                            "damage": int(30 * mults["attack"]),
                            "type": "physical",
                            "cooldown": 2,
                            "description": "A frenzied last-resort attack.",
                        }
                    ],
                    "dialogue": "This isn't over!",
                }
            ]

    # ── Save each enemy + update index ────────────────────────────────
    saved_paths = []
    for enemy in enemies:
        path = _save_enemy(enemy)
        saved_paths.append(path)

    _update_index(enemies)

    names = [e.get("name", "?") for e in enemies]
    names_str = ", ".join(names)
    total_phases = sum(len(e.get("phases", [])) for e in enemies)
    total_attacks = sum(len(e.get("attacks", [])) for e in enemies)

    return {
        "status": "success",
        "summary": (
            f"Designed {len(enemies)} enemy/enemies: {names_str}. "
            f"{total_attacks} attacks, {total_phases} boss phases. "
            f"Index updated ({len(_load_index()['enemies'])} total enemies)."
        ),
        "metrics": {
            "enemies_created": len(enemies),
            "enemy_names": names,
            "enemy_type": enemy_type,
            "difficulty": difficulty,
            "is_boss": is_boss,
            "total_attacks": total_attacks,
            "total_phases": total_phases,
            "index_total": len(_load_index()["enemies"]),
            "output_length": len(response),
            "model_available": True,
            "files": [str(p) for p in saved_paths],
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
