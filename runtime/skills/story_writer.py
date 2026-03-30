"""
story-writer skill — Maintains the story bible for Z_Claw's narrative arc.
Handles overview, acts, cutscenes, lore entries, codex entries, factions, and world rules.
Loads existing bible and merges new content — never overwrites.
Reads GDD, characters, and quests for cross-domain consistency.
Tier 1 (7B local).
"""

import json
import logging
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_7B, STATE_DIR
from runtime.ollama_client import chat_json, is_available

log = logging.getLogger(__name__)
MODEL = MODEL_7B
GAMEDEV_DIR = STATE_DIR / "gamedev"
STORY_DIR = GAMEDEV_DIR / "story"
BIBLE_FILE = STORY_DIR / "story-bible.json"
GDD_FILE = GAMEDEV_DIR / "gdd.json"
CHARACTERS_DIR = GAMEDEV_DIR / "characters"
QUESTS_DIR = GAMEDEV_DIR / "quests"

VALID_SECTIONS = ("overview", "act", "cutscene", "lore", "codex")


# ── State helpers ────────────────────────────────────────────────────────────

def _load_bible() -> dict:
    """Load the story bible, or return an empty scaffold."""
    if BIBLE_FILE.exists():
        try:
            with open(BIBLE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load story-bible.json: %s", e)
    return {
        "title": "",
        "setting": "",
        "themes": [],
        "acts": [],
        "cutscenes": [],
        "lore_entries": [],
        "world_rules": [],
        "factions": [],
    }


def _save_bible(bible: dict) -> None:
    """Persist the story bible."""
    STORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(BIBLE_FILE, "w", encoding="utf-8") as f:
        json.dump(bible, f, indent=2, ensure_ascii=False)


# ── Context gathering ────────────────────────────────────────────────────────

def _load_gdd_context() -> str:
    """Read GDD for genre/setting context."""
    if not GDD_FILE.exists():
        return ""
    try:
        with open(GDD_FILE, encoding="utf-8") as f:
            gdd = json.load(f)
        parts = []
        if gdd.get("title"):
            parts.append(f"Game title: {gdd['title']}")
        if gdd.get("genre"):
            parts.append(f"Genre: {gdd['genre']}")
        if gdd.get("narrative_hook"):
            parts.append(f"Narrative hook: {gdd['narrative_hook']}")
        if gdd.get("art_style"):
            parts.append(f"Art style: {gdd['art_style']}")
        if gdd.get("core_loop"):
            parts.append(f"Core loop: {gdd['core_loop']}")
        return "\n".join(parts) if parts else ""
    except Exception as e:
        log.warning("Failed to read GDD for story context: %s", e)
        return ""


def _load_character_names() -> list[str]:
    """Return a list of known character names."""
    names = []
    if CHARACTERS_DIR.exists():
        try:
            for fpath in sorted(CHARACTERS_DIR.glob("*.json"))[:15]:
                with open(fpath, encoding="utf-8") as f:
                    char = json.load(f)
                name = char.get("name", fpath.stem)
                role = char.get("role", "")
                names.append(f"{name} ({role})" if role else name)
        except Exception as e:
            log.warning("Failed to read characters: %s", e)
    return names


def _load_quest_names() -> list[str]:
    """Return names of existing quests for consistency."""
    index_path = QUESTS_DIR / "_index.json"
    if not index_path.exists():
        return []
    try:
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
        return [q.get("name", "?") for q in index.get("quests", [])[:20]]
    except Exception:
        return []


def _bible_summary(bible: dict) -> str:
    """Compact summary of the current bible state for LLM context."""
    parts = []
    if bible.get("title"):
        parts.append(f"Title: {bible['title']}")
    if bible.get("setting"):
        setting = bible["setting"]
        if len(setting) > 300:
            setting = setting[:300] + "..."
        parts.append(f"Setting: {setting}")
    if bible.get("themes"):
        parts.append(f"Themes: {', '.join(bible['themes'][:10])}")
    if bible.get("acts"):
        act_lines = []
        for act in bible["acts"]:
            act_lines.append(f"  Act {act.get('number', '?')}: {act.get('title', '')} — {act.get('summary', '')[:100]}")
        parts.append("Acts:\n" + "\n".join(act_lines))
    if bible.get("factions"):
        faction_names = []
        for f in bible["factions"][:10]:
            faction_names.append(f.get("name", "?") if isinstance(f, dict) else str(f))
        parts.append(f"Factions: {', '.join(faction_names)}")
    if bible.get("world_rules"):
        parts.append(f"World rules defined: {len(bible['world_rules'])}")
    if bible.get("lore_entries"):
        parts.append(f"Lore entries: {len(bible['lore_entries'])}")
    if bible.get("cutscenes"):
        parts.append(f"Cutscenes: {len(bible['cutscenes'])}")
    return "\n".join(parts) if parts else "Story bible is empty — starting fresh."


# ── Section-specific prompts ────────────────────────────────────────────────

def _system_prompt_for_section(section: str) -> str:
    """Return the system prompt tailored to the requested section."""
    base = (
        "You are the Lead Narrative Designer for Z_Claw, a fantasy-RPG. "
        "You write rich, consistent story content for the game's story bible. "
        "Stay consistent with existing world details. Be specific and evocative. "
        "Return ONLY valid JSON — no markdown, no explanation outside the JSON.\n\n"
    )

    if section == "overview":
        return base + (
            "Generate or update the story overview. Return JSON:\n"
            '{"title": "game title", "setting": "2-4 sentence world description", '
            '"themes": ["theme1", "theme2", ...], '
            '"world_rules": ["rule1", "rule2", ...], '
            '"factions": [{"name": "faction name", "description": "1-2 sentences", "alignment": "good|neutral|evil|ambiguous"}]}'
        )
    elif section == "act":
        return base + (
            "Generate or update a story act. Return JSON:\n"
            '{"number": 1, "title": "act title", "summary": "3-5 sentence act summary", '
            '"key_events": ["event1", "event2", ...], '
            '"climax": "description of the act climax"}'
        )
    elif section == "cutscene":
        return base + (
            "Generate a cutscene for the story. Return JSON:\n"
            '{"id": "cutscene_unique_id", "act": 1, '
            '"trigger": "what triggers this cutscene (e.g., quest completion, area entry)", '
            '"script": "full cutscene script with stage directions in [brackets] and dialogue", '
            '"characters": ["character1", "character2"]}'
        )
    elif section == "lore":
        return base + (
            "Generate a lore entry for the world. Return JSON:\n"
            '{"title": "lore entry title", '
            '"category": "history|geography|culture|magic|technology|mythology|bestiary", '
            '"content": "2-5 paragraph lore text"}'
        )
    elif section == "codex":
        return base + (
            "Generate a codex entry (in-game collectible knowledge). Return JSON:\n"
            '{"title": "codex entry title", '
            '"category": "character|location|item|event|organization", '
            '"content": "1-3 paragraph in-world text written from an in-universe perspective"}'
        )
    else:
        return base + "Generate story content. Return valid JSON."


def _build_user_prompt(section: str, act_number: int | None, prompt: str,
                       bible: dict, gdd_ctx: str, char_names: list[str],
                       quest_names: list[str]) -> str:
    """Build the user prompt from all gathered context."""
    parts = []

    if gdd_ctx:
        parts.append(f"Game design context:\n{gdd_ctx}")

    bible_ctx = _bible_summary(bible)
    parts.append(f"Current story bible:\n{bible_ctx}")

    if char_names:
        parts.append(f"Known characters: {', '.join(char_names[:15])}")
    if quest_names:
        parts.append(f"Known quests: {', '.join(quest_names[:15])}")

    parts.append(f"Section to write: {section}")

    if section == "act" and act_number is not None:
        parts.append(f"Act number: {act_number}")
        # Check if this act already exists — include it for update context
        existing_act = None
        for act in bible.get("acts", []):
            if act.get("number") == act_number:
                existing_act = act
                break
        if existing_act:
            parts.append(f"Existing act {act_number} (update/expand):\n{json.dumps(existing_act, indent=2)}")
        else:
            parts.append(f"This is a NEW act (act {act_number}). Design it to fit the narrative arc.")

    if prompt:
        parts.append(f"Creative direction: {prompt}")

    return "\n\n".join(parts)


# ── Merge logic ──────────────────────────────────────────────────────────────

def _merge_overview(bible: dict, data: dict) -> dict:
    """Merge overview data into the bible without overwriting existing content."""
    if data.get("title"):
        bible["title"] = data["title"]
    if data.get("setting"):
        bible["setting"] = data["setting"]

    # Merge themes (deduplicate)
    new_themes = data.get("themes", [])
    if isinstance(new_themes, list):
        existing = set(bible.get("themes", []))
        for t in new_themes:
            if isinstance(t, str) and t not in existing:
                bible.setdefault("themes", []).append(t)
                existing.add(t)

    # Merge world rules (deduplicate)
    new_rules = data.get("world_rules", [])
    if isinstance(new_rules, list):
        existing_rules = set(bible.get("world_rules", []))
        for r in new_rules:
            if isinstance(r, str) and r not in existing_rules:
                bible.setdefault("world_rules", []).append(r)
                existing_rules.add(r)

    # Merge factions (by name)
    new_factions = data.get("factions", [])
    if isinstance(new_factions, list):
        existing_names = {f.get("name", "").lower() for f in bible.get("factions", []) if isinstance(f, dict)}
        for f in new_factions:
            if isinstance(f, dict) and f.get("name") and f["name"].lower() not in existing_names:
                bible.setdefault("factions", []).append(f)
                existing_names.add(f["name"].lower())

    return bible


def _merge_act(bible: dict, data: dict, act_number: int | None) -> dict:
    """Add or update a specific act in the bible."""
    num = data.get("number", act_number)
    if not isinstance(num, int) or num < 1:
        num = act_number if isinstance(act_number, int) and act_number >= 1 else len(bible.get("acts", [])) + 1

    act_entry = {
        "number": num,
        "title": data.get("title", f"Act {num}"),
        "summary": data.get("summary", ""),
        "key_events": data.get("key_events", []) if isinstance(data.get("key_events"), list) else [],
        "climax": data.get("climax", ""),
    }

    # Replace existing act with same number, or append
    acts = bible.get("acts", [])
    replaced = False
    for i, existing in enumerate(acts):
        if existing.get("number") == num:
            acts[i] = act_entry
            replaced = True
            break
    if not replaced:
        acts.append(act_entry)

    # Sort by act number
    acts.sort(key=lambda a: a.get("number", 0))
    bible["acts"] = acts
    return bible


def _merge_cutscene(bible: dict, data: dict) -> dict:
    """Add a cutscene to the bible (avoid duplicate IDs)."""
    cutscene = {
        "id": data.get("id", f"cs_{len(bible.get('cutscenes', [])) + 1:03d}"),
        "act": data.get("act", 1) if isinstance(data.get("act"), int) else 1,
        "trigger": data.get("trigger", ""),
        "script": data.get("script", ""),
        "characters": data.get("characters", []) if isinstance(data.get("characters"), list) else [],
    }

    # Check for duplicate ID
    existing_ids = {c.get("id") for c in bible.get("cutscenes", [])}
    if cutscene["id"] in existing_ids:
        # Update existing
        for i, c in enumerate(bible["cutscenes"]):
            if c.get("id") == cutscene["id"]:
                bible["cutscenes"][i] = cutscene
                break
    else:
        bible.setdefault("cutscenes", []).append(cutscene)

    return bible


def _merge_lore(bible: dict, data: dict) -> dict:
    """Add a lore entry to the bible."""
    entry = {
        "title": data.get("title", "Untitled Lore"),
        "category": data.get("category", "history"),
        "content": data.get("content", ""),
    }

    # Avoid exact title duplicates
    existing_titles = {e.get("title", "").lower() for e in bible.get("lore_entries", [])}
    if entry["title"].lower() in existing_titles:
        # Update existing
        for i, e in enumerate(bible["lore_entries"]):
            if e.get("title", "").lower() == entry["title"].lower():
                bible["lore_entries"][i] = entry
                break
    else:
        bible.setdefault("lore_entries", []).append(entry)

    return bible


def _merge_codex(bible: dict, data: dict) -> dict:
    """Add a codex entry — stored in lore_entries with a 'codex' source tag."""
    entry = {
        "title": data.get("title", "Untitled Codex"),
        "category": data.get("category", "character"),
        "content": data.get("content", ""),
        "source": "codex",
    }

    existing_titles = {
        e.get("title", "").lower()
        for e in bible.get("lore_entries", [])
        if e.get("source") == "codex"
    }
    if entry["title"].lower() in existing_titles:
        for i, e in enumerate(bible["lore_entries"]):
            if e.get("title", "").lower() == entry["title"].lower() and e.get("source") == "codex":
                bible["lore_entries"][i] = entry
                break
    else:
        bible.setdefault("lore_entries", []).append(entry)

    return bible


# ── Public entry point ───────────────────────────────────────────────────────

def run(**kwargs) -> dict:
    """
    Write or update a section of the story bible.

    kwargs:
        section (str):    One of overview, act, cutscene, lore, codex.
        act_number (int): Required when section="act". Which act to add/update.
        prompt (str):     Creative direction for the LLM.
    """
    STORY_DIR.mkdir(parents=True, exist_ok=True)

    section = kwargs.get("section", "overview")
    act_number = kwargs.get("act_number")
    prompt = kwargs.get("prompt", "")

    if section not in VALID_SECTIONS:
        section = "overview"
    if section == "act" and (not isinstance(act_number, int) or act_number < 1):
        act_number = 1

    bible = _load_bible()
    gdd_ctx = _load_gdd_context()
    char_names = _load_character_names()
    quest_names = _load_quest_names()

    # ── Check LLM availability ───────────────────────────────────────────
    if not is_available(MODEL, host=OLLAMA_HOST):
        summary = f"Story writer ({section}) — LLM unavailable, bible preserved."
        act_count = len(bible.get("acts", []))
        lore_count = len(bible.get("lore_entries", []))
        if bible.get("title"):
            summary += f" Current title: '{bible['title']}'. {act_count} act(s), {lore_count} lore entries."
        return {
            "status": "degraded",
            "summary": summary,
            "metrics": {
                "section": section,
                "acts_count": act_count,
                "lore_count": lore_count,
                "cutscenes_count": len(bible.get("cutscenes", [])),
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # ── LLM generation ───────────────────────────────────────────────────
    system_prompt = _system_prompt_for_section(section)
    user_prompt = _build_user_prompt(
        section, act_number, prompt,
        bible, gdd_ctx, char_names, quest_names,
    )

    try:
        raw = chat_json(MODEL, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.5, max_tokens=1200, task_type="story-writer")
    except Exception as e:
        log.error("story-writer LLM call failed: %s", e)
        return {
            "status": "failed",
            "summary": f"Story writer LLM call failed: {e}",
            "metrics": {
                "section": section,
                "acts_count": len(bible.get("acts", [])),
                "model_available": True,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    if not isinstance(raw, dict):
        log.warning("story-writer: LLM returned non-dict (%s), cannot merge", type(raw).__name__)
        return {
            "status": "failed",
            "summary": f"Story writer received invalid response type ({type(raw).__name__}), bible unchanged.",
            "metrics": {
                "section": section,
                "acts_count": len(bible.get("acts", [])),
                "model_available": True,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # ── Merge into bible ─────────────────────────────────────────────────
    section_detail = ""
    if section == "overview":
        bible = _merge_overview(bible, raw)
        section_detail = f"title='{bible.get('title', '')}', {len(bible.get('themes', []))} themes, {len(bible.get('factions', []))} factions"
    elif section == "act":
        bible = _merge_act(bible, raw, act_number)
        section_detail = f"act {act_number}: '{raw.get('title', '')}'"
    elif section == "cutscene":
        bible = _merge_cutscene(bible, raw)
        section_detail = f"cutscene '{raw.get('id', '?')}' for act {raw.get('act', '?')}"
    elif section == "lore":
        bible = _merge_lore(bible, raw)
        section_detail = f"lore entry '{raw.get('title', '')}' ({raw.get('category', '')})"
    elif section == "codex":
        bible = _merge_codex(bible, raw)
        section_detail = f"codex entry '{raw.get('title', '')}' ({raw.get('category', '')})"

    _save_bible(bible)

    summary = (
        f"Story bible updated ({section}): {section_detail}. "
        f"Bible now has {len(bible.get('acts', []))} act(s), "
        f"{len(bible.get('lore_entries', []))} lore entries, "
        f"{len(bible.get('cutscenes', []))} cutscenes, "
        f"{len(bible.get('factions', []))} factions."
    )

    return {
        "status": "success",
        "summary": summary,
        "section_data": raw,
        "metrics": {
            "section": section,
            "acts_count": len(bible.get("acts", [])),
            "lore_count": len(bible.get("lore_entries", [])),
            "cutscenes_count": len(bible.get("cutscenes", [])),
            "factions_count": len(bible.get("factions", [])),
            "themes_count": len(bible.get("themes", [])),
            "world_rules_count": len(bible.get("world_rules", [])),
            "model_available": True,
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
