"""
Art Director — Tier 1 LLM skill that generates creative briefs for the
production pipeline.  Sits ABOVE generation skills: reads the storyboard
queue and asset catalog, then produces scene direction, composition notes,
color mood, lighting, reference style, and commander aesthetic guidance.

Falls back to template-based briefs when Ollama is unavailable.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import SKILL_MODELS, STATE_DIR, BASE_DIR
from runtime.ollama_client import chat_json, is_available

log = logging.getLogger(__name__)
MODEL = SKILL_MODELS["art-director"]

STORYBOARD_FILE = BASE_DIR / "divisions" / "production" / "packets" / "storyboard.json"
CATALOG_FILE    = STATE_DIR / "asset-catalog.json"

# ── Commander aesthetic palettes ──────────────────────────────────────────────
_COMMANDER_AESTHETICS = {
    "vael":    {"palette": "amber, brown, deep forest green",
                "mood": "scout vigilance, quiet intensity",
                "motifs": "bow, dark hood, woodland shadows"},
    "seren":   {"palette": "silver, cyan, pale moonlight",
                "mood": "mystical foresight, ethereal calm",
                "motifs": "peaked hat, oracle staff, star charts"},
    "kaelen":  {"palette": "purple, gunmetal, spark orange",
                "mood": "industrial precision, relentless invention",
                "motifs": "mech eye, iron armor, blueprints"},
    "lyrin":   {"palette": "green, white, soft gold",
                "mood": "gentle healing, quiet resilience",
                "motifs": "leaf crown, healer robes, glowing orb"},
    "zeth":    {"palette": "deep shadow, crimson, obsidian",
                "mood": "stealth menace, controlled danger",
                "motifs": "hood, twin blades, red glowing eyes"},
    "lyke":    {"palette": "deep orange, iron, blueprint blue",
                "mood": "forge mastery, creative fire",
                "motifs": "architect armor, scroll, anvil sparks"},
    "generic": {"palette": "royal purple, gold, fantasy tones",
                "mood": "heroic adventure, high fantasy",
                "motifs": "crest, ornate armor, magical aura"},
}

# ── Focus area templates (fallback when no LLM) ──────────────────────────────
_FOCUS_TEMPLATES = {
    "general": {
        "composition": "Rule of thirds, subject centered with breathing room",
        "lighting": "Three-point lighting, warm key with cool fill",
        "color_mood": "Balanced saturation, fantasy-warm midtones",
        "reference_style": "Fire Emblem Engage key art, official character art",
    },
    "battle": {
        "composition": "Dynamic diagonal lines, low camera angle for power",
        "lighting": "Dramatic rim lighting with particle effects",
        "color_mood": "High contrast, desaturated background with vivid focal point",
        "reference_style": "Fire Emblem battle cut-in, Persona 5 all-out attack",
    },
    "portrait": {
        "composition": "Tight bust frame, shallow depth of field, bokeh background",
        "lighting": "Soft front fill, hair light from above, subtle eye catch light",
        "color_mood": "Warm skin tones, complementary background gradient",
        "reference_style": "Fire Emblem Engage character select portraits",
    },
    "ui": {
        "composition": "Centered symmetrical layout, clear negative space for text",
        "lighting": "Flat lighting, golden shimmer accents on borders",
        "color_mood": "Rich jewel tones, high legibility contrast",
        "reference_style": "Genshin Impact achievement cards, FE menu UI",
    },
    "sprite": {
        "composition": "Full body centered, white or transparent background",
        "lighting": "Even flat lighting, no harsh shadows for sprite clarity",
        "color_mood": "Saturated and clean, game-sprite readable at small sizes",
        "reference_style": "Fire Emblem Heroes chibi sprites, SD proportions",
    },
}


def _load_storyboard() -> list:
    """Load pending shots from the storyboard queue."""
    if not STORYBOARD_FILE.exists():
        return []
    try:
        with open(STORYBOARD_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("shots", [])
    except Exception:
        return []


def _load_catalog_summary() -> dict:
    """Load a lightweight summary of the asset catalog."""
    if not CATALOG_FILE.exists():
        return {"total": 0, "by_type": {}, "by_commander": {}}
    try:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            catalog = json.load(f)
    except Exception:
        return {"total": 0, "by_type": {}, "by_commander": {}}

    by_type = {}
    by_commander = {}
    for entry in catalog:
        t = entry.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
        c = entry.get("commander", "generic")
        by_commander[c] = by_commander.get(c, 0) + 1

    return {
        "total": len(catalog),
        "by_type": by_type,
        "by_commander": by_commander,
    }


def _template_brief(shot: dict, commander: str, focus_area: str) -> dict:
    """Build a creative brief from templates (no LLM)."""
    aesthetic = _COMMANDER_AESTHETICS.get(commander.lower(),
                                         _COMMANDER_AESTHETICS["generic"])
    focus = _FOCUS_TEMPLATES.get(focus_area, _FOCUS_TEMPLATES["general"])
    shot_data = shot.get("shot", {})

    return {
        "scene_description": shot_data.get("description", f"{commander} scene — {focus_area}"),
        "composition_notes": focus["composition"],
        "color_mood": f"{aesthetic['palette']} — {focus['color_mood']}",
        "lighting_direction": f"{shot_data.get('lighting', focus['lighting'])}",
        "reference_style": focus["reference_style"],
        "commander_aesthetic": aesthetic,
        "shot_type": shot_data.get("shot_type", "portrait_bust"),
        "mood": shot_data.get("mood", aesthetic["mood"]),
        "source": "template",
    }


def _llm_briefs(shots: list, commander: str, focus_area: str,
                catalog_summary: dict) -> list:
    """Use local Ollama to generate enriched creative briefs."""
    aesthetic = _COMMANDER_AESTHETICS.get(commander.lower(),
                                         _COMMANDER_AESTHETICS["generic"])

    # Build a compact context for the LLM
    shot_descriptions = []
    for i, shot in enumerate(shots[:5]):  # cap at 5 to keep prompt short
        sd = shot.get("shot", {})
        shot_descriptions.append(
            f"{i+1}. [{sd.get('shot_type','scene')}] {sd.get('description','untitled')} "
            f"(mood: {sd.get('mood','unspecified')})"
        )

    shots_text = "\n".join(shot_descriptions) if shot_descriptions else "No pending shots."
    catalog_text = (
        f"Catalog: {catalog_summary['total']} assets. "
        f"Types: {json.dumps(catalog_summary['by_type'])}. "
        f"Commanders: {json.dumps(catalog_summary['by_commander'])}."
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are LYKE's Art Director for a Fire Emblem-inspired game. "
                "Given storyboard shots and an asset catalog summary, produce creative briefs. "
                "Return JSON: {\"briefs\": [{\"scene_description\": str, "
                "\"composition_notes\": str, \"color_mood\": str, "
                "\"lighting_direction\": str, \"reference_style\": str, "
                "\"mood\": str, \"shot_type\": str}]}. "
                "Keep each field to one sentence. Max 5 briefs."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Focus: {focus_area}. Commander: {commander}.\n"
                f"Aesthetic — palette: {aesthetic['palette']}, "
                f"mood: {aesthetic['mood']}, motifs: {aesthetic['motifs']}.\n"
                f"Shots:\n{shots_text}\n{catalog_text}"
            ),
        },
    ]

    raw = chat_json(MODEL, messages, temperature=0.3, max_tokens=400, task_type="art-director")

    # Normalise response — may be {"briefs": [...]} or just a list
    if isinstance(raw, dict):
        briefs_raw = raw.get("briefs", [raw])
    elif isinstance(raw, list):
        briefs_raw = raw
    else:
        briefs_raw = []

    briefs = []
    for b in briefs_raw:
        if not isinstance(b, dict):
            continue
        b["commander_aesthetic"] = aesthetic
        b["source"] = "llm"
        briefs.append(b)

    return briefs


def run(focus_area: str = "general", commander: str = "generic") -> dict:
    """Art Director skill entry point."""
    try:
        shots = _load_storyboard()
        catalog_summary = _load_catalog_summary()

        briefs = []
        used_llm = False

        if shots and is_available(MODEL):
            # LLM-enriched briefs from storyboard shots
            try:
                briefs = _llm_briefs(shots, commander, focus_area, catalog_summary)
                used_llm = True
            except Exception as e:
                log.warning("art-director LLM call failed, falling back to templates: %s", e)

        # Fallback: template-based briefs
        if not briefs:
            if shots:
                briefs = [_template_brief(s, commander, focus_area) for s in shots[:5]]
            else:
                # No storyboard shots — generate a single general brief
                dummy_shot = {"shot": {
                    "description": f"{commander} — {focus_area} creative direction",
                    "shot_type": "portrait_bust" if focus_area == "portrait" else "battle_scene",
                    "mood": _COMMANDER_AESTHETICS.get(
                        commander.lower(), _COMMANDER_AESTHETICS["generic"])["mood"],
                    "lighting": _FOCUS_TEMPLATES.get(
                        focus_area, _FOCUS_TEMPLATES["general"])["lighting"],
                }}
                briefs = [_template_brief(dummy_shot, commander, focus_area)]

        # Count style notes across all briefs
        style_notes_count = sum(
            1 for b in briefs
            for key in ("composition_notes", "color_mood", "lighting_direction", "reference_style")
            if b.get(key)
        )

        summary = (
            f"Art Director produced {len(briefs)} creative brief(s) for "
            f"focus={focus_area}, commander={commander}. "
            f"{'LLM-enriched' if used_llm else 'Template-based'}. "
            f"{len(shots)} storyboard shot(s) reviewed, "
            f"{catalog_summary['total']} assets in catalog."
        )

        log.info("art_director: %d briefs, llm=%s, focus=%s, commander=%s",
                 len(briefs), used_llm, focus_area, commander)

        return {
            "status":       "success",
            "summary":      summary,
            "briefs":       briefs,
            "metrics": {
                "briefs_generated":  len(briefs),
                "style_notes_count": style_notes_count,
                "focus_area":        focus_area,
                "commander":         commander,
                "llm_used":          used_llm,
                "storyboard_shots":  len(shots),
                "catalog_total":     catalog_summary["total"],
            },
            "action_items": [],
            "escalate":     False,
        }

    except Exception as e:
        log.error("art_director failed: %s", e)
        return {
            "status":       "failed",
            "summary":      f"Art Director failed: {e}",
            "briefs":       [],
            "metrics":      {},
            "action_items": [{
                "priority": "normal",
                "description": str(e),
                "requires_matthew": False,
            }],
            "escalate":     False,
        }
