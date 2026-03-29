"""
Narrative Bridge — Tier 1 LLM skill that translates story/realm events
into production-ready creative briefs.

Reads story state (chapter, arc, relationships), chronicle entries (rank-ups,
achievements, prestige), and recent game events, then generates scene
descriptions, dialogue snippets, and visual prompts rich enough for
prompt_craft to build a generation prompt from.

Falls back to template-based scenes if Ollama is unavailable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from runtime.config import STATE_DIR, MODEL_7B
from runtime.realm import story, chronicle
from runtime.realm.config import DIVISIONS
from runtime.realm.events import recent as recent_game_events
from runtime.ollama_client import chat_json, is_available

log = logging.getLogger(__name__)

MODEL = MODEL_7B
GAME_EVENTS_LIMIT = 20
CHRONICLE_LIMIT = 15

# ── Emotion map — derive dominant emotion from story signals ─────────────────

_STANCE_EMOTIONS = {
    "loyal":    "pride",
    "aligned":  "resolve",
    "watchful": "tension",
    "strained": "unease",
}

_EVENT_EMOTIONS = {
    "rank_up":           "triumph",
    "prestige":          "awe",
    "streak_milestone":  "determination",
    "achievement":       "elation",
    "skill_complete":    "focus",
    "ruler_reward":      "gratitude",
}

_ARC_MOODS = {
    "balanced": "measured and deliberate",
    "aggressive": "urgent and relentless",
    "patient": "contemplative and watchful",
}

# ── Template fallbacks ───────────────────────────────────────────────────────

_SCENE_TEMPLATES = {
    "rank_up": {
        "type": "portrait_bust",
        "description": "{commander} ascends to new authority within {order}. The court witnesses the transformation.",
        "visual_prompt": "fantasy commander portrait, rank-up aura, glowing insignia, dramatic backlight, {style}",
        "dialogue": "{commander}: 'The order answers. We climb.'",
        "emotion": "triumph",
    },
    "prestige": {
        "type": "battle_scene",
        "description": "The realm resets but remembers. Prestige reshapes the court's gravity.",
        "visual_prompt": "fantasy throne room, cosmic reset, swirling energy, prestige ascension, epic wide shot",
        "dialogue": "J_Claw: 'Everything earned remains. Everything ahead is harder.'",
        "emotion": "awe",
    },
    "achievement": {
        "type": "ui_element",
        "description": "A new mark is etched into the chronicle. The realm takes note.",
        "visual_prompt": "fantasy achievement badge, golden glow, ornate frame, celebratory particles",
        "dialogue": "",
        "emotion": "elation",
    },
    "streak_milestone": {
        "type": "portrait_bust",
        "description": "{commander} holds the line. {order} marks another unbroken stretch.",
        "visual_prompt": "fantasy commander portrait, streak fire aura, determined expression, warm side-light, {style}",
        "dialogue": "{commander}: 'Consistency is the blade that never dulls.'",
        "emotion": "determination",
    },
    "chapter_advance": {
        "type": "battle_scene",
        "description": "The realm crosses into {chapter_title}. {chapter_summary}",
        "visual_prompt": "fantasy landscape, chapter transition, new dawn, epic wide shot, dramatic clouds, {arc_mood}",
        "dialogue": "",
        "emotion": "anticipation",
    },
    "generic": {
        "type": "battle_scene",
        "description": "The realm continues its advance. Events unfold across the court.",
        "visual_prompt": "fantasy court scene, multiple commanders, strategic table, ambient light",
        "dialogue": "",
        "emotion": "resolve",
    },
}


# ── Data gathering ───────────────────────────────────────────────────────────

def _gather_context() -> dict:
    """Collect story state, chronicle, and game events into a unified context."""
    state = story.current_state()
    chronicle_entries = chronicle.get_recent(limit=CHRONICLE_LIMIT)
    game_events = recent_game_events(limit=GAME_EVENTS_LIMIT)

    # Distill relationships into a compact form
    relationships = {}
    for div_key, rel in state.get("relationships", {}).items():
        relationships[div_key] = {
            "commander": rel.get("commander", div_key),
            "trust": rel.get("trust", 50),
            "tension": rel.get("tension", 15),
            "stance": rel.get("stance", "watchful"),
        }

    # Determine dominant emotion from highest-tension or most-recent chronicle
    dominant_emotion = "resolve"
    if chronicle_entries:
        cat = chronicle_entries[0].get("category", "")
        dominant_emotion = _EVENT_EMOTIONS.get(cat, "resolve")

    # Check for strained commanders
    strained = [r["commander"] for r in relationships.values() if r["stance"] == "strained"]
    if strained:
        dominant_emotion = "unease"

    arc_id = state.get("active_arc", {}).get("id", "balanced")

    return {
        "chapter": {
            "id": state.get("chapter", 0),
            "key": state.get("chapter_key", "prologue"),
            "title": state.get("chapter_title", "The Awakening"),
            "summary": state.get("chapter_summary", ""),
        },
        "arc": {
            "id": arc_id,
            "name": state.get("active_arc", {}).get("name", "The Measured Ascent"),
            "mood": _ARC_MOODS.get(arc_id, "measured and deliberate"),
        },
        "doctrine": state.get("doctrine", {}),
        "relationships": relationships,
        "recent_scenes": state.get("recent_scenes", [])[-5:],
        "chronicle": chronicle_entries[:10],
        "game_events": game_events[:10],
        "progress": state.get("progress", {}),
        "dominant_emotion": dominant_emotion,
        "strained_commanders": strained if strained else [],
    }


# ── LLM scene generation ────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are the Narrative Bridge for Z_Claw, a fantasy-RPG command realm.
Given story context, generate 1-3 scenes as a JSON object.

Each scene must have:
- type: one of "portrait_bust", "battle_scene", "ui_element", "chibi_sprite"
- description: 1-2 sentence scene description for production direction
- visual_prompt: comma-separated visual tags for image generation (style, mood, lighting, composition)
- dialogue: a short in-character line (empty string if none fits)
- commander: which commander features (or "J_Claw" / "" if realm-wide)
- emotion: single word emotion (triumph, awe, unease, resolve, tension, pride, determination, elation, anticipation, gratitude)

Return ONLY valid JSON: {"scenes": [...]}
Keep it concise. No markdown. No explanation outside the JSON."""


def _build_user_prompt(ctx: dict, event_type: str, commander: str) -> str:
    """Build a compact LLM prompt from gathered context."""
    parts = [
        f"Chapter: {ctx['chapter']['title']} — {ctx['chapter']['summary']}",
        f"Arc: {ctx['arc']['name']} (mood: {ctx['arc']['mood']})",
        f"Dominant emotion: {ctx['dominant_emotion']}",
    ]

    if ctx["strained_commanders"]:
        parts.append(f"Strained commanders: {', '.join(ctx['strained_commanders'])}")

    # Include top relationships
    rel_lines = []
    for div_key, r in list(ctx["relationships"].items())[:5]:
        rel_lines.append(f"  {r['commander']}: trust={r['trust']} tension={r['tension']} stance={r['stance']}")
    if rel_lines:
        parts.append("Relationships:\n" + "\n".join(rel_lines))

    # Recent chronicle highlights
    if ctx["chronicle"]:
        chron_lines = []
        for e in ctx["chronicle"][:5]:
            chron_lines.append(f"  [{e.get('category', '?')}] {e.get('title', '')}")
        parts.append("Recent chronicle:\n" + "\n".join(chron_lines))

    # Recent game events
    if ctx["game_events"]:
        ev_lines = []
        for e in ctx["game_events"][:5]:
            ev_lines.append(f"  [{e.get('event', '?')}] {e.get('division', '?')} — {e.get('status', '')}")
        parts.append("Recent game events:\n" + "\n".join(ev_lines))

    if event_type != "auto":
        parts.append(f"Focus event type: {event_type}")
    if commander != "generic":
        parts.append(f"Focus commander: {commander}")

    parts.append("Generate 1-3 scenes that capture the current narrative moment.")
    return "\n".join(parts)


def _generate_scenes_llm(ctx: dict, event_type: str, commander: str) -> list[dict]:
    """Use Ollama to generate scenes from narrative context."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(ctx, event_type, commander)},
    ]
    result = chat_json(MODEL, messages, temperature=0.4, max_tokens=500, task_type="narrative-craft")
    scenes = result.get("scenes", [])

    # Validate and normalize each scene
    valid = []
    for s in scenes:
        if not isinstance(s, dict):
            continue
        scene = {
            "type": s.get("type", "battle_scene"),
            "description": s.get("description", ""),
            "visual_prompt": s.get("visual_prompt", ""),
            "dialogue": s.get("dialogue", ""),
            "commander": s.get("commander", commander),
            "emotion": s.get("emotion", ctx["dominant_emotion"]),
        }
        if scene["description"]:
            valid.append(scene)
    return valid


# ── Template fallback ────────────────────────────────────────────────────────

def _generate_scenes_template(ctx: dict, event_type: str, commander: str) -> list[dict]:
    """Generate scenes from templates when Ollama is unavailable."""
    scenes = []

    # Determine which template categories to use
    categories = set()
    if event_type != "auto":
        categories.add(event_type)
    else:
        # Infer from recent chronicle
        for entry in ctx.get("chronicle", [])[:5]:
            cat = entry.get("category", "")
            if cat in _SCENE_TEMPLATES:
                categories.add(cat)
        # Check for chapter advances via recent scenes
        for sc in ctx.get("recent_scenes", []):
            if sc.get("type") == "chapter_unlock":
                categories.add("chapter_advance")

    if not categories:
        categories.add("generic")

    # Build one scene per category (max 3)
    for cat in list(categories)[:3]:
        tpl = _SCENE_TEMPLATES.get(cat, _SCENE_TEMPLATES["generic"]).copy()

        # Resolve commander for the scene
        scene_cmdr = commander
        if scene_cmdr == "generic" and ctx.get("chronicle"):
            for entry in ctx["chronicle"]:
                if entry.get("commander"):
                    scene_cmdr = entry["commander"]
                    break

        div_key = ""
        for dk, r in ctx.get("relationships", {}).items():
            if r.get("commander", "").upper() == scene_cmdr.upper():
                div_key = dk
                break

        div_meta = DIVISIONS.get(div_key, {})
        order = div_meta.get("order", "the realm")
        style = f"fire emblem, fantasy, {div_meta.get('color', '#7c3aed')} tones"

        scene = {
            "type": tpl["type"],
            "description": tpl["description"].format(
                commander=scene_cmdr,
                order=order,
                chapter_title=ctx["chapter"]["title"],
                chapter_summary=ctx["chapter"]["summary"],
                arc_mood=ctx["arc"]["mood"],
                style=style,
            ),
            "visual_prompt": tpl["visual_prompt"].format(
                commander=scene_cmdr,
                order=order,
                style=style,
                arc_mood=ctx["arc"]["mood"],
            ),
            "dialogue": tpl["dialogue"].format(
                commander=scene_cmdr,
                order=order,
            ),
            "commander": scene_cmdr,
            "emotion": tpl.get("emotion", ctx["dominant_emotion"]),
        }
        scenes.append(scene)

    return scenes


# ── Public entry point ───────────────────────────────────────────────────────

def run(event_type: str = "auto", commander: str = "generic") -> dict:
    """
    Narrative Bridge skill entry point.

    Args:
        event_type: Focus event type, or "auto" to infer from recent activity.
        commander:  Focus commander name, or "generic" for realm-wide scenes.

    Returns:
        Standard skill result dict with status, scenes, summary, metrics.
    """
    try:
        ctx = _gather_context()
        llm_used = False

        if is_available(MODEL):
            try:
                scenes = _generate_scenes_llm(ctx, event_type, commander)
                llm_used = True
            except Exception as e:
                log.warning("narrative-craft LLM generation failed, falling back to templates: %s", e)
                scenes = _generate_scenes_template(ctx, event_type, commander)
        else:
            log.info("narrative-craft: Ollama unavailable, using template fallback")
            scenes = _generate_scenes_template(ctx, event_type, commander)

        if not scenes:
            scenes = _generate_scenes_template(ctx, event_type, commander)

        summary = (
            f"Narrative bridge generated {len(scenes)} scene(s) "
            f"for chapter '{ctx['chapter']['title']}' "
            f"(arc: {ctx['arc']['name']}, mood: {ctx['dominant_emotion']}). "
            f"{'LLM' if llm_used else 'Template'}-driven."
        )

        log.info("narrative-craft: %d scenes, llm=%s", len(scenes), llm_used)

        return {
            "status": "success",
            "summary": summary,
            "scenes": scenes,
            "metrics": {
                "scenes_generated": len(scenes),
                "events_processed": len(ctx.get("game_events", [])) + len(ctx.get("chronicle", [])),
                "llm_used": llm_used,
                "chapter": ctx["chapter"]["title"],
                "arc": ctx["arc"]["id"],
                "dominant_emotion": ctx["dominant_emotion"],
            },
            "action_items": [],
            "escalate": False,
        }

    except Exception as e:
        log.error("narrative-craft failed: %s", e)
        return {
            "status": "failed",
            "summary": f"Narrative bridge failed: {e}",
            "scenes": [],
            "metrics": {"scenes_generated": 0, "events_processed": 0},
            "action_items": [{"priority": "normal", "description": str(e), "requires_matthew": False}],
            "escalate": False,
        }
