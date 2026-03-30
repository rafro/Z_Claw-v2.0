"""
quest-writer skill — Builds quest chains with branching narratives,
objectives, rewards, and NPC dialogue.
Reads existing story and character data for narrative consistency.
Saves individual quests + maintains a quest index.
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
QUESTS_DIR = GAMEDEV_DIR / "quests"
INDEX_FILE = QUESTS_DIR / "_index.json"
STORY_DIR = GAMEDEV_DIR / "story"
CHARACTERS_DIR = GAMEDEV_DIR / "characters"

VALID_QUEST_TYPES = ("main", "side", "daily", "event")


# ── State helpers ────────────────────────────────────────────────────────────

def _load_index() -> dict:
    """Load the quest index, or return an empty scaffold."""
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load quest index: %s", e)
    return {"quests": []}


def _save_index(index: dict) -> None:
    """Persist the quest index."""
    QUESTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)


def _load_quest(quest_name: str) -> dict | None:
    """Load a single quest file by name, or None if not found."""
    fpath = QUESTS_DIR / f"{quest_name}.json"
    if fpath.exists():
        try:
            with open(fpath, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load quest '%s': %s", quest_name, e)
    return None


def _save_quest(quest: dict) -> None:
    """Persist a single quest to its own file."""
    QUESTS_DIR.mkdir(parents=True, exist_ok=True)
    name = quest.get("name", "unnamed_quest")
    safe_name = name.lower().replace(" ", "_").replace("/", "_")
    fpath = QUESTS_DIR / f"{safe_name}.json"
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(quest, f, indent=2, ensure_ascii=False)


def _update_index(quest: dict) -> None:
    """Add or update a quest entry in the index."""
    index = _load_index()
    quest_name = quest.get("name", "")
    quest_type = quest.get("quest_type", "side")
    chapter = quest.get("chapter", 1)

    # Remove existing entry with the same name if present
    index["quests"] = [
        q for q in index["quests"]
        if q.get("name") != quest_name
    ]

    index["quests"].append({
        "name": quest_name,
        "quest_type": quest_type,
        "chapter": chapter,
        "difficulty": quest.get("difficulty", "medium"),
        "giver_npc": quest.get("giver_npc", ""),
    })

    _save_index(index)


# ── Context gathering ────────────────────────────────────────────────────────

def _gather_story_context() -> str:
    """Read existing story data from state/gamedev/story/ for LLM context."""
    parts = []

    # Story bible
    bible_path = STORY_DIR / "story-bible.json"
    if bible_path.exists():
        try:
            with open(bible_path, encoding="utf-8") as f:
                bible = json.load(f)
            if bible.get("title"):
                parts.append(f"Game title: {bible['title']}")
            if bible.get("setting"):
                parts.append(f"Setting: {bible['setting']}")
            if bible.get("themes"):
                parts.append(f"Themes: {', '.join(bible['themes'])}")
            if bible.get("acts"):
                act_summaries = []
                for act in bible["acts"][:5]:
                    act_summaries.append(
                        f"  Act {act.get('number', '?')}: {act.get('title', '')} — {act.get('summary', '')}"
                    )
                parts.append("Story acts:\n" + "\n".join(act_summaries))
            if bible.get("factions"):
                faction_names = [f.get("name", "?") if isinstance(f, dict) else str(f) for f in bible["factions"][:10]]
                parts.append(f"Factions: {', '.join(faction_names)}")
        except Exception as e:
            log.warning("Failed to read story bible for context: %s", e)

    return "\n".join(parts) if parts else ""


def _gather_character_context() -> str:
    """Read character sheets from state/gamedev/characters/ for LLM context."""
    parts = []
    if CHARACTERS_DIR.exists():
        try:
            for fpath in sorted(CHARACTERS_DIR.glob("*.json"))[:10]:
                with open(fpath, encoding="utf-8") as f:
                    char = json.load(f)
                name = char.get("name", fpath.stem)
                role = char.get("role", "unknown")
                parts.append(f"  {name} ({role})")
        except Exception as e:
            log.warning("Failed to read character data: %s", e)

    if parts:
        return "Known characters:\n" + "\n".join(parts)
    return ""


def _gather_existing_quests_context() -> str:
    """Summarise existing quests so the LLM avoids duplication."""
    index = _load_index()
    quests = index.get("quests", [])
    if not quests:
        return ""
    lines = []
    for q in quests[:15]:
        lines.append(f"  [{q.get('quest_type', '?')}] Ch{q.get('chapter', '?')}: {q.get('name', '?')}")
    return "Existing quests:\n" + "\n".join(lines)


# ── Quest scaffold (fallback) ───────────────────────────────────────────────

def _scaffold_quest(quest_name: str, quest_type: str, chapter: int, prompt: str) -> dict:
    """Return a minimal quest scaffold when LLM is unavailable."""
    return {
        "name": quest_name,
        "quest_type": quest_type,
        "chapter": chapter,
        "description": f"Auto-scaffolded quest: {prompt}" if prompt else f"Placeholder quest for chapter {chapter}.",
        "giver_npc": "",
        "prerequisites": [],
        "objectives": [
            {
                "description": "Complete the main objective",
                "type": "interact",
                "target": "unknown",
                "count": 1,
                "optional": False,
            },
        ],
        "rewards": {
            "xp": 100 * chapter,
            "gold": 50 * chapter,
            "items": [],
            "unlocks": [],
        },
        "dialogue": {
            "intro": "",
            "progress": "",
            "completion": "",
            "failure": "",
        },
        "branching": [],
        "estimated_time": "10 minutes",
        "difficulty": "medium",
    }


# ── LLM quest generation ────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a Quest Designer for Z_Claw, a fantasy-RPG game.
Given context about the game world, characters, and existing quests, design a single quest.

Return ONLY valid JSON with this exact structure:
{
  "name": "quest name",
  "quest_type": "main|side|daily|event",
  "chapter": 1,
  "description": "2-3 sentence quest description",
  "giver_npc": "NPC name who gives the quest",
  "prerequisites": [
    {"type": "quest_complete|level|item|faction_rep", "value": "prerequisite identifier or number"}
  ],
  "objectives": [
    {
      "description": "objective text",
      "type": "kill|collect|interact|escort|defend|explore|craft",
      "target": "target name or item",
      "count": 1,
      "optional": false
    }
  ],
  "rewards": {
    "xp": 500,
    "gold": 200,
    "items": ["item name"],
    "unlocks": ["unlock description"]
  },
  "dialogue": {
    "intro": "NPC dialogue when accepting the quest",
    "progress": "NPC dialogue while quest is in progress",
    "completion": "NPC dialogue on quest completion",
    "failure": "NPC dialogue on quest failure or abandonment"
  },
  "branching": [
    {
      "choice": "player choice description",
      "consequence": "what happens as a result",
      "leads_to": "follow-up quest name or empty string"
    }
  ],
  "estimated_time": "15 minutes",
  "difficulty": "easy|medium|hard|legendary"
}

No markdown. No explanation outside the JSON."""


def _build_user_prompt(quest_name: str, quest_type: str, chapter: int, prompt: str,
                       story_ctx: str, char_ctx: str, quest_ctx: str) -> str:
    """Build the user prompt from all available context."""
    parts = []

    if story_ctx:
        parts.append(f"World context:\n{story_ctx}")
    if char_ctx:
        parts.append(char_ctx)
    if quest_ctx:
        parts.append(quest_ctx)

    parts.append(f"Quest name: {quest_name}")
    parts.append(f"Quest type: {quest_type}")
    parts.append(f"Chapter: {chapter}")

    if prompt:
        parts.append(f"Design direction: {prompt}")

    # Type-specific guidance
    if quest_type == "main":
        parts.append("This is a MAIN quest — it should advance the central story arc and feel significant.")
    elif quest_type == "daily":
        parts.append("This is a DAILY quest — keep it short (5-10 min), repeatable, with modest rewards.")
    elif quest_type == "event":
        parts.append("This is an EVENT quest — it should feel time-limited and special, with unique rewards.")
    else:
        parts.append("This is a SIDE quest — it should enrich the world with lore or character development.")

    parts.append("Design one compelling quest. Keep dialogue in-character and concise.")
    return "\n\n".join(parts)


def _validate_quest(quest: dict) -> list[str]:
    """Validate quest structure and return a list of issues (empty = valid)."""
    issues = []

    if not quest.get("name"):
        issues.append("Missing quest name")
    if quest.get("quest_type") not in VALID_QUEST_TYPES:
        issues.append(f"Invalid quest_type: {quest.get('quest_type')}")
    if not isinstance(quest.get("chapter"), int) or quest["chapter"] < 1:
        issues.append(f"Invalid chapter: {quest.get('chapter')}")
    if not quest.get("description"):
        issues.append("Missing description")

    # Objectives
    objectives = quest.get("objectives", [])
    if not isinstance(objectives, list) or len(objectives) == 0:
        issues.append("Quest has no objectives")
    else:
        for i, obj in enumerate(objectives):
            if not isinstance(obj, dict):
                issues.append(f"Objective {i} is not a dict")
            elif not obj.get("description"):
                issues.append(f"Objective {i} missing description")

    # Rewards
    rewards = quest.get("rewards")
    if not isinstance(rewards, dict):
        issues.append("Missing or invalid rewards")

    # Dialogue
    dialogue = quest.get("dialogue")
    if not isinstance(dialogue, dict):
        issues.append("Missing or invalid dialogue")

    return issues


def _normalize_quest(raw: dict, quest_name: str, quest_type: str, chapter: int) -> dict:
    """Ensure all expected fields exist and have correct types."""
    quest = {
        "name": raw.get("name") or quest_name,
        "quest_type": raw.get("quest_type") if raw.get("quest_type") in VALID_QUEST_TYPES else quest_type,
        "chapter": raw.get("chapter") if isinstance(raw.get("chapter"), int) else chapter,
        "description": raw.get("description", ""),
        "giver_npc": raw.get("giver_npc", ""),
        "prerequisites": raw.get("prerequisites", []) if isinstance(raw.get("prerequisites"), list) else [],
        "objectives": [],
        "rewards": {
            "xp": 0,
            "gold": 0,
            "items": [],
            "unlocks": [],
        },
        "dialogue": {
            "intro": "",
            "progress": "",
            "completion": "",
            "failure": "",
        },
        "branching": [],
        "estimated_time": raw.get("estimated_time", "15 minutes"),
        "difficulty": raw.get("difficulty", "medium"),
    }

    # Normalize objectives
    for obj in (raw.get("objectives") or []):
        if isinstance(obj, dict) and obj.get("description"):
            quest["objectives"].append({
                "description": obj.get("description", ""),
                "type": obj.get("type", "interact"),
                "target": obj.get("target", ""),
                "count": obj.get("count", 1) if isinstance(obj.get("count"), int) else 1,
                "optional": bool(obj.get("optional", False)),
            })

    # Fallback if LLM returned no valid objectives
    if not quest["objectives"]:
        quest["objectives"].append({
            "description": "Complete the quest objective",
            "type": "interact",
            "target": "unknown",
            "count": 1,
            "optional": False,
        })

    # Normalize rewards
    raw_rewards = raw.get("rewards", {})
    if isinstance(raw_rewards, dict):
        quest["rewards"]["xp"] = raw_rewards.get("xp", 0) if isinstance(raw_rewards.get("xp"), (int, float)) else 0
        quest["rewards"]["gold"] = raw_rewards.get("gold", 0) if isinstance(raw_rewards.get("gold"), (int, float)) else 0
        quest["rewards"]["items"] = raw_rewards.get("items", []) if isinstance(raw_rewards.get("items"), list) else []
        quest["rewards"]["unlocks"] = raw_rewards.get("unlocks", []) if isinstance(raw_rewards.get("unlocks"), list) else []

    # Normalize dialogue
    raw_dialogue = raw.get("dialogue", {})
    if isinstance(raw_dialogue, dict):
        for key in ("intro", "progress", "completion", "failure"):
            val = raw_dialogue.get(key, "")
            quest["dialogue"][key] = val if isinstance(val, str) else ""

    # Normalize branching
    for branch in (raw.get("branching") or []):
        if isinstance(branch, dict) and branch.get("choice"):
            quest["branching"].append({
                "choice": branch.get("choice", ""),
                "consequence": branch.get("consequence", ""),
                "leads_to": branch.get("leads_to", ""),
            })

    return quest


# ── Public entry point ───────────────────────────────────────────────────────

def run(**kwargs) -> dict:
    """
    Generate a quest with objectives, rewards, dialogue, and branching.

    kwargs:
        quest_name (str):  Name/title of the quest.
        quest_type (str):  One of main, side, daily, event. Defaults to "side".
        chapter (int):     Chapter number this quest belongs to. Defaults to 1.
        prompt (str):      Additional design direction for the LLM.
    """
    QUESTS_DIR.mkdir(parents=True, exist_ok=True)

    quest_name = kwargs.get("quest_name", "Unnamed Quest")
    quest_type = kwargs.get("quest_type", "side")
    chapter = kwargs.get("chapter", 1)
    prompt = kwargs.get("prompt", "")

    if quest_type not in VALID_QUEST_TYPES:
        quest_type = "side"
    if not isinstance(chapter, int) or chapter < 1:
        chapter = 1

    # Gather context for the LLM
    story_ctx = _gather_story_context()
    char_ctx = _gather_character_context()
    quest_ctx = _gather_existing_quests_context()

    # ── Check LLM availability ───────────────────────────────────────────
    if not is_available(MODEL, host=OLLAMA_HOST):
        log.info("quest-writer: Ollama unavailable, generating scaffold")
        quest = _scaffold_quest(quest_name, quest_type, chapter, prompt)
        _save_quest(quest)
        _update_index(quest)

        return {
            "status": "degraded",
            "summary": f"Quest '{quest_name}' scaffolded (LLM unavailable). Fill in dialogue and details manually.",
            "quest": quest,
            "metrics": {
                "quest_name": quest_name,
                "quest_type": quest_type,
                "chapter": chapter,
                "objectives_count": len(quest["objectives"]),
                "branching_paths": len(quest["branching"]),
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [
                {"priority": "low", "description": f"Review scaffolded quest '{quest_name}' — needs dialogue and polish.", "requires_matthew": False},
            ],
        }

    # ── LLM generation ───────────────────────────────────────────────────
    user_prompt = _build_user_prompt(
        quest_name, quest_type, chapter, prompt,
        story_ctx, char_ctx, quest_ctx,
    )

    try:
        raw = chat_json(MODEL, [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ], temperature=0.5, max_tokens=1200, task_type="quest-writer")
    except Exception as e:
        log.error("quest-writer LLM call failed: %s", e)
        # Fall back to scaffold
        quest = _scaffold_quest(quest_name, quest_type, chapter, prompt)
        _save_quest(quest)
        _update_index(quest)
        return {
            "status": "failed",
            "summary": f"Quest LLM generation failed ({e}). Scaffold saved for '{quest_name}'.",
            "quest": quest,
            "metrics": {
                "quest_name": quest_name,
                "quest_type": quest_type,
                "chapter": chapter,
                "objectives_count": len(quest["objectives"]),
                "branching_paths": 0,
                "model_available": True,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # ── Parse and normalize ──────────────────────────────────────────────
    if not isinstance(raw, dict):
        log.warning("quest-writer: LLM returned non-dict (%s), falling back to scaffold", type(raw).__name__)
        quest = _scaffold_quest(quest_name, quest_type, chapter, prompt)
    else:
        quest = _normalize_quest(raw, quest_name, quest_type, chapter)

    # Validate
    issues = _validate_quest(quest)
    if issues:
        log.warning("quest-writer: validation issues after normalization: %s", issues)

    # Persist
    _save_quest(quest)
    _update_index(quest)

    index = _load_index()
    total_quests = len(index.get("quests", []))

    summary = (
        f"Quest '{quest['name']}' ({quest['quest_type']}, Ch{quest['chapter']}) generated — "
        f"{len(quest['objectives'])} objective(s), {len(quest['branching'])} branch(es), "
        f"difficulty: {quest['difficulty']}. "
        f"Total quests in index: {total_quests}."
    )

    return {
        "status": "success",
        "summary": summary,
        "quest": quest,
        "metrics": {
            "quest_name": quest["name"],
            "quest_type": quest["quest_type"],
            "chapter": quest["chapter"],
            "objectives_count": len(quest["objectives"]),
            "branching_paths": len(quest["branching"]),
            "rewards_xp": quest["rewards"]["xp"],
            "rewards_gold": quest["rewards"]["gold"],
            "total_quests_indexed": total_quests,
            "validation_issues": len(issues),
            "model_available": True,
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
