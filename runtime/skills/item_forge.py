"""
item-forge skill — Designs weapons, armor, consumables, materials, and
accessories.  Uses LLM to generate detailed item data with stats, effects,
crafting recipes, and lore.  Saves individual items and maintains an index
at state/gamedev/items/_index.json.
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
ITEMS_DIR = GAMEDEV_DIR / "items"
INDEX_FILE = ITEMS_DIR / "_index.json"
GDD_FILE = GAMEDEV_DIR / "gdd.json"

VALID_TYPES = {"weapon", "armor", "consumable", "material", "accessory"}
VALID_RARITIES = {"common", "uncommon", "rare", "epic", "legendary"}


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
    """Load the item index or return a fresh one."""
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load items _index.json: %s", e)
    return {"items": []}


def _save_index(index: dict) -> None:
    """Persist the item index."""
    ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)


def _save_item(item: dict) -> Path:
    """Persist a single item and return the file path."""
    ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    name = item.get("name", "unnamed_item")
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unnamed_item"
    path = ITEMS_DIR / f"{slug}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(item, f, indent=2, ensure_ascii=False)
    return path


def _update_index(items: list[dict]) -> None:
    """Add items to the index, avoiding duplicates by name."""
    index = _load_index()
    existing_names = {e["name"].lower() for e in index["items"]}
    for item in items:
        item_name = item.get("name", "")
        if item_name.lower() not in existing_names:
            index["items"].append({
                "name": item_name,
                "type": item.get("type", "unknown"),
                "rarity": item.get("rarity", "common"),
            })
            existing_names.add(item_name.lower())
    _save_index(index)


def _parse_json_from_response(text: str):
    """Try to extract a JSON object or array from LLM output."""
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try code fence
    m = re.search(r"```(?:json)?\s*([\[\{].*?[\]\}])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Outermost braces — try array first, then object
    for pattern in [r"\[.*\]", r"\{.*\}"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return None


def _build_default_item(
    item_name: str, item_type: str, rarity: str,
) -> dict:
    """Fallback item when LLM is unavailable."""
    return {
        "name": item_name or "Unnamed Item",
        "type": item_type,
        "rarity": rarity,
        "stats": {
            "damage": 0, "defense": 0, "speed_mod": 0, "crit_chance": 0.0,
        },
        "effects": [],
        "description": "",
        "lore": "",
        "crafting_recipe": {"materials": [], "gold_cost": 0},
        "requirements": {"level": 1, "class": "any", "stat_min": {}},
        "visual_description": "",
        "icon_spec": "",
    }


def _ensure_item_schema(item: dict, item_type: str, rarity: str) -> dict:
    """Fill in any missing keys with safe defaults."""
    item.setdefault("name", "Unnamed Item")
    item.setdefault("type", item_type)
    item.setdefault("rarity", rarity)
    item.setdefault("stats", {
        "damage": 0, "defense": 0, "speed_mod": 0, "crit_chance": 0.0,
    })
    item.setdefault("effects", [])
    item.setdefault("description", "")
    item.setdefault("lore", "")
    item.setdefault("crafting_recipe", {"materials": [], "gold_cost": 0})
    item.setdefault("requirements", {"level": 1, "class": "any", "stat_min": {}})
    item.setdefault("visual_description", "")
    item.setdefault("icon_spec", "")
    return item


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(**kwargs) -> dict:
    """
    Design game items — weapons, armor, consumables, materials, accessories.

    kwargs:
        item_name (str):  Name for the item.  If omitted, LLM generates 3-5.
        item_type (str):  weapon | armor | consumable | material | accessory.
        rarity (str):     common | uncommon | rare | epic | legendary.
        prompt (str):     Extra creative direction for the LLM.
    """
    ITEMS_DIR.mkdir(parents=True, exist_ok=True)

    item_name = kwargs.get("item_name", "")
    item_type = kwargs.get("item_type", "weapon")
    rarity = kwargs.get("rarity", "common")
    prompt = kwargs.get("prompt", "")

    if item_type not in VALID_TYPES:
        item_type = "weapon"
    if rarity not in VALID_RARITIES:
        rarity = "common"

    batch_mode = not item_name
    gdd = _load_gdd()
    genre = gdd.get("genre", "fantasy RPG")
    art_style = gdd.get("art_style", "pixel-art")

    # ── Ollama unavailable — graceful degradation ──────────────────────
    if not is_available(MODEL, host=OLLAMA_HOST):
        item = _build_default_item(item_name, item_type, rarity)
        path = _save_item(item)
        _update_index([item])
        return {
            "status": "degraded",
            "summary": (
                f"Item '{item['name']}' created with defaults — "
                "LLM unavailable, stats are placeholder."
            ),
            "metrics": {
                "items_created": 1,
                "item_type": item_type,
                "rarity": rarity,
                "batch_mode": batch_mode,
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": ["Re-run when Ollama is back for richer output."],
        }

    # ── Build prompts ──────────────────────────────────────────────────
    if batch_mode:
        output_instruction = (
            "Generate a JSON array of 3-5 items that would appear in this game. "
            "Include a mix of types and rarities unless told otherwise."
        )
    else:
        output_instruction = (
            "Generate a single JSON object for this item."
        )

    system_prompt = (
        "You are ARDENT, the Item Design lead for Z_Claw. "
        "You create detailed game items as pure JSON. "
        "Output ONLY valid JSON — no markdown, no commentary. "
        f"{output_instruction} "
        "Each item must have these keys: "
        "name, type (weapon|armor|consumable|material|accessory), "
        "rarity (common|uncommon|rare|epic|legendary), "
        "stats (object with damage/defense/speed_mod/crit_chance), "
        "effects (array of objects each with name/description/trigger/value), "
        "description, lore, "
        "crafting_recipe (object with materials array and gold_cost), "
        "requirements (object with level/class/stat_min), "
        "visual_description, icon_spec."
    )

    user_parts = [f"Game genre: {genre}", f"Art style: {art_style}"]
    if item_name:
        user_parts.append(f"Item name: {item_name}")
    user_parts.append(f"Item type: {item_type}")
    user_parts.append(f"Rarity: {rarity}")
    if prompt:
        user_parts.append(f"Creative direction: {prompt}")
    if batch_mode:
        user_parts.append(
            "Create 3-5 varied items that fit the game world. "
            "Give each a unique name, interesting lore, and balanced stats."
        )
    else:
        user_parts.append(
            "Make the lore 1-2 sentences, the description vivid, and "
            "include at least one special effect."
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
            temperature=0.5,
            max_tokens=1500,
            task_type="item-forge",
        )
    except Exception as e:
        log.error("item-forge LLM call failed: %s", e)
        item = _build_default_item(item_name, item_type, rarity)
        path = _save_item(item)
        _update_index([item])
        return {
            "status": "failed",
            "summary": f"LLM call failed ({e}); saved placeholder item.",
            "metrics": {
                "items_created": 1,
                "item_type": item_type,
                "rarity": rarity,
                "batch_mode": batch_mode,
                "model_available": True,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": ["Investigate Ollama error and retry."],
        }

    # ── Parse response ────────────────────────────────────────────────
    parsed = _parse_json_from_response(response)
    items: list[dict] = []

    if parsed is None:
        log.warning("item-forge: could not parse JSON, using fallback")
        item = _build_default_item(item_name, item_type, rarity)
        item["_raw_llm_output"] = response[:2000]
        items.append(item)
    elif isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, dict):
                items.append(_ensure_item_schema(entry, item_type, rarity))
    elif isinstance(parsed, dict):
        # Could be {"items": [...]} wrapper or a single item
        if "items" in parsed and isinstance(parsed["items"], list):
            for entry in parsed["items"]:
                if isinstance(entry, dict):
                    items.append(_ensure_item_schema(entry, item_type, rarity))
        else:
            items.append(_ensure_item_schema(parsed, item_type, rarity))

    if not items:
        item = _build_default_item(item_name, item_type, rarity)
        items.append(item)

    # ── Save each item + update index ─────────────────────────────────
    saved_paths = []
    for item in items:
        path = _save_item(item)
        saved_paths.append(path)

    _update_index(items)

    names = [i.get("name", "?") for i in items]
    names_str = ", ".join(names)

    return {
        "status": "success",
        "summary": (
            f"Forged {len(items)} item(s): {names_str}. "
            f"Index updated ({len(_load_index()['items'])} total items)."
        ),
        "metrics": {
            "items_created": len(items),
            "item_names": names,
            "item_type": item_type,
            "rarity": rarity,
            "batch_mode": batch_mode,
            "index_total": len(_load_index()["items"]),
            "output_length": len(response),
            "model_available": True,
            "files": [str(p) for p in saved_paths],
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
