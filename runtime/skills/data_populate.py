"""
data-populate skill — Generates balance data files from game content designs.
Reads character, enemy, item, and skill-tree designs, then creates
damage-tables.json, economy.json, and progression-curves.json for balance-audit.
Tier 0 (pure Python — deterministic computation from design data).
"""

import json
import logging
import math
from pathlib import Path

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"

# Design source directories
CHARACTERS_DIR = GAMEDEV_DIR / "characters"
ENEMIES_DIR = GAMEDEV_DIR / "enemies"
ITEMS_DIR = GAMEDEV_DIR / "items"
SKILL_TREES_DIR = GAMEDEV_DIR / "skill-trees"
QUESTS_DIR = GAMEDEV_DIR / "quests"

# Output files
DAMAGE_TABLES_FILE = GAMEDEV_DIR / "damage-tables.json"
ECONOMY_FILE = GAMEDEV_DIR / "economy.json"
PROGRESSION_FILE = GAMEDEV_DIR / "progression-curves.json"

# Balance constants (sensible defaults)
DEFAULT_ATTACK_SPEED = 1.0  # attacks per second
DEFAULT_MAX_LEVEL = 20
BASE_XP_PER_LEVEL = 100
XP_GROWTH_FACTOR = 1.5


def _load_json_dir(directory: Path) -> list[tuple[str, dict]]:
    """Load all JSON files from a directory. Returns list of (stem, data)."""
    results = []
    if not directory.exists():
        return results
    for fpath in sorted(directory.glob("*.json")):
        # Skip index files
        if fpath.stem.startswith("_"):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                results.append((fpath.stem, data))
        except Exception as e:
            log.warning("Failed to load %s: %s", fpath, e)
    return results


def _load_quest_index() -> list[dict]:
    """Load quests from _index.json if it exists."""
    index_file = QUESTS_DIR / "_index.json"
    if not index_file.exists():
        return []
    try:
        with open(index_file, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("quests", [])
    except Exception as e:
        log.warning("Failed to load quests/_index.json: %s", e)
    return []


def _safe_num(value, default=0) -> float:
    """Safely extract a numeric value."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    return float(default)


def _build_damage_tables(
    characters: list[tuple[str, dict]],
    enemies: list[tuple[str, dict]],
    items: list[tuple[str, dict]],
) -> dict:
    """Generate damage-tables.json data."""
    player_damage = []
    enemy_hp = []
    ttk_analysis = []

    # Extract player damage from characters
    for stem, char in characters:
        base_attack = _safe_num(char.get("base_attack") or char.get("attack") or char.get("stats", {}).get("attack", 0))
        weapon_bonus = 0
        char_class = char.get("class", char.get("name", stem))

        # Check if character has a default weapon referenced
        default_weapon = char.get("default_weapon") or char.get("starting_weapon", "")
        if default_weapon:
            # Try to find the weapon in items
            for item_stem, item in items:
                if item_stem == default_weapon or item.get("name", "") == default_weapon:
                    weapon_bonus = _safe_num(item.get("attack_bonus") or item.get("damage") or item.get("stats", {}).get("attack", 0))
                    break

        total = base_attack + weapon_bonus
        if base_attack > 0 or weapon_bonus > 0:
            player_damage.append({
                "source": f"character/{stem}",
                "class": char_class,
                "base_attack": base_attack,
                "weapon_bonus": weapon_bonus,
                "total": total,
            })

    # Extract enemy HP
    for stem, enemy in enemies:
        hp = _safe_num(enemy.get("hp") or enemy.get("health") or enemy.get("stats", {}).get("hp", 0))
        name = enemy.get("name", stem)
        if hp > 0:
            # Calculate hits-to-kill using average player damage
            avg_player_dmg = 0
            if player_damage:
                avg_player_dmg = sum(pd["total"] for pd in player_damage) / len(player_damage)
            hits_to_kill = round(hp / avg_player_dmg, 1) if avg_player_dmg > 0 else 0

            enemy_hp.append({
                "name": name,
                "source": f"enemy/{stem}",
                "hp": hp,
                "hits_to_kill": hits_to_kill,
            })

    # Build time-to-kill analysis (each attacker vs each enemy)
    for pd in player_damage:
        attack_speed = DEFAULT_ATTACK_SPEED
        for eh in enemy_hp:
            if pd["total"] > 0:
                hits_needed = math.ceil(eh["hp"] / pd["total"])
                ttk_seconds = round(hits_needed / attack_speed, 1)
                ttk_analysis.append({
                    "attacker": pd.get("class", pd["source"]),
                    "target": eh["name"],
                    "hits_needed": hits_needed,
                    "time_to_kill_seconds": ttk_seconds,
                })

    return {
        "player_damage": player_damage,
        "enemy_hp": enemy_hp,
        "ttk_analysis": ttk_analysis,
    }


def _build_economy(
    items: list[tuple[str, dict]],
    quests: list[dict],
    enemies: list[tuple[str, dict]],
) -> dict:
    """Generate economy.json data."""
    gold_sources = []
    gold_sinks = []
    crafting_costs = []

    # Quest rewards as gold sources
    for quest in quests:
        quest_name = quest.get("name") or quest.get("id", "unknown")
        gold_reward = _safe_num(quest.get("gold_reward") or quest.get("reward_gold") or quest.get("rewards", {}).get("gold", 0))
        if gold_reward > 0:
            gold_sources.append({
                "source": f"quest/{quest_name}",
                "amount": gold_reward,
                "type": "quest_reward",
            })

    # Enemy loot as gold sources
    for stem, enemy in enemies:
        name = enemy.get("name", stem)
        loot_gold = _safe_num(enemy.get("gold_drop") or enemy.get("loot", {}).get("gold", 0))
        if loot_gold > 0:
            gold_sources.append({
                "source": f"enemy/{stem}",
                "amount": loot_gold,
                "type": "enemy_loot",
            })

    # Item costs as gold sinks
    for stem, item in items:
        name = item.get("name", stem)
        cost = _safe_num(item.get("cost") or item.get("price") or item.get("buy_price", 0))
        if cost > 0:
            gold_sinks.append({
                "item": name,
                "source": f"item/{stem}",
                "cost": cost,
            })

        # Crafting costs
        craft = item.get("crafting") or item.get("recipe", {})
        if isinstance(craft, dict) and craft:
            materials = craft.get("materials") or craft.get("ingredients", [])
            craft_gold = _safe_num(craft.get("gold_cost", 0))
            if materials or craft_gold > 0:
                crafting_costs.append({
                    "item": name,
                    "source": f"item/{stem}",
                    "gold_cost": craft_gold,
                    "materials": materials if isinstance(materials, list) else [],
                })

    # Compute balance ratio: total gold in vs total gold out
    total_sources = sum(gs["amount"] for gs in gold_sources)
    total_sinks = sum(gs["cost"] for gs in gold_sinks)
    balance_ratio = round(total_sources / total_sinks, 2) if total_sinks > 0 else 0.0

    # Estimate inflation rate (sources vs sinks)
    # ratio > 1 means more gold entering than leaving
    inflation_rate = round(max(0, balance_ratio - 1.0), 3) if balance_ratio > 0 else 0.0

    return {
        "gold_sources": gold_sources,
        "gold_sinks": gold_sinks,
        "crafting_costs": crafting_costs,
        "balance_ratio": balance_ratio,
        "inflation_rate": inflation_rate,
        "total_gold_available": total_sources,
        "total_gold_required": total_sinks,
    }


def _build_progression(
    characters: list[tuple[str, dict]],
    quests: list[dict],
    enemies: list[tuple[str, dict]],
    skill_trees: list[tuple[str, dict]],
) -> dict:
    """Generate progression-curves.json data."""
    # Generate XP curve
    xp_per_level = [0]
    for level in range(1, DEFAULT_MAX_LEVEL + 1):
        xp_needed = int(BASE_XP_PER_LEVEL * (XP_GROWTH_FACTOR ** (level - 1)))
        xp_per_level.append(xp_needed)

    # Stat growth per class
    stat_growth = []
    for stem, char in characters:
        char_class = char.get("class", char.get("name", stem))
        hp_per_level = _safe_num(char.get("hp_per_level") or char.get("stats", {}).get("hp_per_level", 0))
        attack_per_level = _safe_num(char.get("attack_per_level") or char.get("stats", {}).get("attack_per_level", 0))

        # If per-level growth is not specified, estimate from base stats
        if hp_per_level == 0:
            base_hp = _safe_num(char.get("hp") or char.get("health") or char.get("stats", {}).get("hp", 0))
            if base_hp > 0:
                hp_per_level = round(base_hp * 0.1, 1)  # 10% of base per level
        if attack_per_level == 0:
            base_atk = _safe_num(char.get("base_attack") or char.get("attack") or char.get("stats", {}).get("attack", 0))
            if base_atk > 0:
                attack_per_level = round(base_atk * 0.08, 1)  # 8% of base per level

        if hp_per_level > 0 or attack_per_level > 0:
            stat_growth.append({
                "class": char_class,
                "source": f"character/{stem}",
                "hp_per_level": hp_per_level,
                "attack_per_level": attack_per_level,
            })

    # Quest XP rewards
    quest_xp_rewards = []
    for quest in quests:
        quest_name = quest.get("name") or quest.get("id", "unknown")
        xp_reward = _safe_num(quest.get("xp_reward") or quest.get("reward_xp") or quest.get("rewards", {}).get("xp", 0))
        if xp_reward > 0:
            quest_xp_rewards.append({
                "source": f"quest/{quest_name}",
                "xp": xp_reward,
            })

    # Enemy XP rewards
    enemy_xp_rewards = []
    for stem, enemy in enemies:
        name = enemy.get("name", stem)
        xp = _safe_num(enemy.get("xp_reward") or enemy.get("xp") or enemy.get("rewards", {}).get("xp", 0))
        if xp > 0:
            enemy_xp_rewards.append({
                "source": f"enemy/{stem}",
                "name": name,
                "xp": xp,
            })

    # Estimate hours to max level
    total_xp_needed = sum(xp_per_level)
    xp_per_hour = 0
    if enemy_xp_rewards:
        # Assume ~60 kills per hour at average XP
        avg_enemy_xp = sum(e["xp"] for e in enemy_xp_rewards) / len(enemy_xp_rewards)
        xp_per_hour += avg_enemy_xp * 60
    if quest_xp_rewards:
        # Assume ~2 quests per hour at average XP
        avg_quest_xp = sum(q["xp"] for q in quest_xp_rewards) / len(quest_xp_rewards)
        xp_per_hour += avg_quest_xp * 2

    estimated_hours = round(total_xp_needed / xp_per_hour, 1) if xp_per_hour > 0 else 0

    # Skill tree effect values
    skill_tree_data = []
    for stem, tree in skill_trees:
        tree_name = tree.get("name", stem)
        nodes = tree.get("nodes") or tree.get("skills", [])
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, dict):
                    effect = node.get("effect") or node.get("bonus", "")
                    node_name = node.get("name", "unknown")
                    skill_tree_data.append({
                        "tree": tree_name,
                        "node": node_name,
                        "effect": effect if isinstance(effect, str) else json.dumps(effect),
                    })

    return {
        "xp_per_level": xp_per_level,
        "max_level": DEFAULT_MAX_LEVEL,
        "stat_growth": stat_growth,
        "quest_xp_rewards": quest_xp_rewards,
        "enemy_xp_rewards": enemy_xp_rewards,
        "skill_tree_effects": skill_tree_data,
        "estimated_hours_to_max": estimated_hours,
        "total_xp_to_max": total_xp_needed,
    }


def _save_json(path: Path, data: dict) -> None:
    """Write a JSON file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def run(**kwargs) -> dict:
    """
    Generate balance data files from game content designs.

    kwargs:
        force_regenerate (bool): Regenerate even if files already exist. Default False.
    """
    GAMEDEV_DIR.mkdir(parents=True, exist_ok=True)

    force = kwargs.get("force_regenerate", False)

    # Check if output files already exist and skip unless forced
    if not force:
        all_exist = (
            DAMAGE_TABLES_FILE.exists()
            and ECONOMY_FILE.exists()
            and PROGRESSION_FILE.exists()
        )
        if all_exist:
            return {
                "status": "success",
                "summary": (
                    "Balance data files already exist. "
                    "Use force_regenerate=True to rebuild from designs."
                ),
                "metrics": {
                    "characters_processed": 0,
                    "enemies_processed": 0,
                    "items_processed": 0,
                    "files_generated": 0,
                    "skipped": True,
                },
                "escalate": False,
                "escalation_reason": "",
                "action_items": [],
            }

    # Load all design data
    characters = _load_json_dir(CHARACTERS_DIR)
    enemies = _load_json_dir(ENEMIES_DIR)
    items = _load_json_dir(ITEMS_DIR)
    skill_trees = _load_json_dir(SKILL_TREES_DIR)
    quests = _load_quest_index()

    total_designs = len(characters) + len(enemies) + len(items) + len(skill_trees) + len(quests)

    if total_designs == 0:
        return {
            "status": "partial",
            "summary": (
                "No design data found. Add JSON design files to "
                "state/gamedev/characters/, enemies/, items/, skill-trees/, quests/."
            ),
            "metrics": {
                "characters_processed": 0,
                "enemies_processed": 0,
                "items_processed": 0,
                "files_generated": 0,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Build the three balance data files
    damage_tables = _build_damage_tables(characters, enemies, items)
    economy = _build_economy(items, quests, enemies)
    progression = _build_progression(characters, quests, enemies, skill_trees)

    # Save output files
    files_generated = 0

    _save_json(DAMAGE_TABLES_FILE, damage_tables)
    files_generated += 1
    log.info("Wrote %s", DAMAGE_TABLES_FILE)

    _save_json(ECONOMY_FILE, economy)
    files_generated += 1
    log.info("Wrote %s", ECONOMY_FILE)

    _save_json(PROGRESSION_FILE, progression)
    files_generated += 1
    log.info("Wrote %s", PROGRESSION_FILE)

    # Build summary
    summary_parts = [
        f"Data populate: generated {files_generated} balance file(s) from {total_designs} design(s).",
    ]
    if characters:
        summary_parts.append(f"{len(characters)} character(s).")
    if enemies:
        summary_parts.append(f"{len(enemies)} enemy(ies).")
    if items:
        summary_parts.append(f"{len(items)} item(s).")
    if not characters and not enemies:
        summary_parts.append("Note: no character or enemy data — damage tables will be sparse.")

    return {
        "status": "success",
        "summary": " ".join(summary_parts),
        "metrics": {
            "characters_processed": len(characters),
            "enemies_processed": len(enemies),
            "items_processed": len(items),
            "skill_trees_processed": len(skill_trees),
            "quests_processed": len(quests),
            "files_generated": files_generated,
            "damage_entries": len(damage_tables.get("player_damage", [])),
            "economy_sources": len(economy.get("gold_sources", [])),
            "economy_sinks": len(economy.get("gold_sinks", [])),
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
