"""
Realm story engine.

Maintains a persistent story state driven by progression events and player
choices. This is the canonical narrative layer for chapters, doctrine, and
commander relationships.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from runtime.config import STATE_DIR
from runtime.realm.config import DIVISIONS
from runtime.tools.atomic_write import atomic_write_json

try:
    from runtime.tools import anim_queue as _aq
except Exception:
    _aq = None

log = logging.getLogger(__name__)

STORY_FILE = STATE_DIR / "story-state.json"
SCENE_LIMIT = 18
CHOICE_LIMIT = 40
CORE_DIVISIONS = ["opportunity", "trading", "dev_automation", "personal", "op_sec"]

ARC_DEFS = {
    "balanced": {
        "id": "balanced",
        "label": "Balanced Doctrine",
        "name": "The Measured Ascent",
        "summary": (
            "J_Claw advances without overreach. Each commander gets room to work, "
            "and the realm grows with discipline instead of panic."
        ),
    },
    "aggressive": {
        "id": "aggressive",
        "label": "Aggressive Doctrine",
        "name": "The Relentless Campaign",
        "summary": (
            "Momentum rules the court. Success is pressed hard and quickly, "
            "even when the commanders begin to warn about strain at the edges."
        ),
    },
    "patient": {
        "id": "patient",
        "label": "Patient Doctrine",
        "name": "The Long Game",
        "summary": (
            "The sovereign waits, studies, and commits only when the field is ready. "
            "The realm moves slower, but every move lands with intent."
        ),
    },
}

CHAPTERS = [
    {
        "id": 0,
        "key": "prologue",
        "label": "Prologue",
        "title": "The Awakening",
        "summary": "The realm stirs. The commanders are watching to learn what kind of sovereign J_Claw will become.",
    },
    {
        "id": 1,
        "key": "orders_convene",
        "label": "Arc I",
        "title": "The Orders Convene",
        "summary": "The first commanders are no longer acting alone. The realm is beginning to behave like a court, not a camp.",
        "unlock": lambda state: len(_active_divisions(state)) >= 2,
    },
    {
        "id": 2,
        "key": "doctrine_formed",
        "label": "Arc II",
        "title": "Doctrine of the Realm",
        "summary": "J_Claw's decisions are no longer isolated. A governing doctrine is emerging, and the commanders are starting to adapt to it.",
        "unlock": lambda state: len(state.get("choices", [])) >= 3,
    },
    {
        "id": 3,
        "key": "fracture_lines",
        "label": "Arc III",
        "title": "Fracture Lines",
        "summary": "Partial victories, escalations, and strain inside the court reveal that growth has a cost. The commanders begin to diverge.",
        "unlock": lambda state: state.get("progress", {}).get("crisis_events", 0) >= 1 or any(
            rel.get("tension", 0) >= 55 for rel in state.get("relationships", {}).values()
        ),
    },
    {
        "id": 4,
        "key": "iron_pact",
        "label": "Arc IV",
        "title": "The Iron Pact",
        "summary": "Every order is now active. J_Claw is no longer assembling a realm, but governing one. Coordination becomes the real trial.",
        "unlock": lambda state: len(_active_divisions(state)) >= len(CORE_DIVISIONS),
    },
    {
        "id": 5,
        "key": "sovereigns_trial",
        "label": "Arc V",
        "title": "The Sovereign's Trial",
        "summary": "Prestige, high command, and legendary ranks transform the realm into a proving ground. The next failures will matter more than the first ones ever did.",
        "unlock": lambda state: state.get("progress", {}).get("prestige", 0) >= 1 or state.get("progress", {}).get("level", 1) >= 10,
    },
]

FIRST_ACTIVATION_SCENES = {
    "opportunity": (
        "VAEL Takes the Field",
        "Vael opens the Dawnhunt ledger and marks the first quarry. The court realizes the realm can now look outward, not only inward.",
    ),
    "trading": (
        "SEREN Speaks First",
        "Seren breaks the silence with the first resolved signal. The Auric Veil is no longer prophecy alone; it has become action.",
    ),
    "dev_automation": (
        "KAELEN Lights the Forge",
        "Kaelen sets the first structure into place. The Iron Codex begins to look less like a workshop and more like an engine.",
    ),
    "personal": (
        "LYRIN Tends the Flame",
        "Lyrin establishes the sovereign's rhythm. The court understands that endurance is now part of strategy, not an afterthought.",
    ),
    "op_sec": (
        "ZETH Raises the Ward",
        "Zeth seals the first unseen seam. The realm learns that what does not break in public can still fail in shadow.",
    ),
    "production": (
        "LYKE Opens the Forge",
        "Lyke brings the first artifact into being. The realm now has a maker, and ideas can start becoming visible assets.",
    ),
}

CHOICE_EFFECTS = {
    "opportunity": {
        "aggressive": {"trust": 3, "tension": 1},
        "patient": {"trust": 1, "tension": -1},
        "balanced": {"trust": 2, "tension": 0},
    },
    "trading": {
        "aggressive": {"trust": 2, "tension": 1},
        "patient": {"trust": 3, "tension": -1},
        "balanced": {"trust": 2, "tension": 0},
    },
    "dev_automation": {
        "aggressive": {"trust": 1, "tension": 1},
        "patient": {"trust": 2, "tension": -1},
        "balanced": {"trust": 3, "tension": -1},
    },
    "personal": {
        "aggressive": {"trust": -3, "tension": 5},
        "patient": {"trust": 3, "tension": -2},
        "balanced": {"trust": 2, "tension": -1},
    },
    "op_sec": {
        "aggressive": {"trust": -1, "tension": 4},
        "patient": {"trust": 3, "tension": -1},
        "balanced": {"trust": 2, "tension": 0},
    },
    "production": {
        "aggressive": {"trust": 2, "tension": 1},
        "patient": {"trust": 1, "tension": -1},
        "balanced": {"trust": 3, "tension": -1},
    },
}

CHOICE_REACTIONS = {
    "aggressive": "The order feels the pace increase. Momentum rises, but so does strain.",
    "patient": "The court accepts restraint. The realm slows enough for intent to become visible.",
    "balanced": "The sovereign keeps pressure and discipline in equilibrium. The commanders can work without losing the line.",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relationship_template(division: str) -> dict:
    div = DIVISIONS.get(division, {})
    return {
        "division": division,
        "commander": div.get("commander", division),
        "order": div.get("order", division),
        "trust": 50,
        "tension": 15,
        "stance": "watchful",
        "last_event": "",
    }


def _default_state() -> dict:
    chapter = CHAPTERS[0]
    return {
        "version": 2,
        "chapter": chapter["id"],
        "chapter_key": chapter["key"],
        "chapter_label": chapter["label"],
        "chapter_title": chapter["title"],
        "chapter_summary": chapter["summary"],
        "active_arc": ARC_DEFS["balanced"].copy(),
        "relationships": {division: _relationship_template(division) for division in CORE_DIVISIONS},
        "doctrine": {"aggressive": 0, "patient": 0, "balanced": 0, "dominant": "balanced"},
        "choices": [],
        "recent_scenes": [],
        "chapter_history": [
            {
                "chapter": chapter["id"],
                "chapter_key": chapter["key"],
                "title": chapter["title"],
                "summary": chapter["summary"],
                "ts": _now(),
            }
        ],
        "flags": {
            "active_divisions": [],
            "first_activation": {},
            "chapters_unlocked": [chapter["key"]],
            "scene_flags": {},
        },
        "progress": {
            "battles": 0,
            "crisis_events": 0,
            "prestige": 0,
            "level": 1,
            "last_division": "",
        },
        "last_choice": None,
        "pending_choice": None,
        "last_updated": _now(),
    }


def _load_state() -> dict:
    if not STORY_FILE.exists():
        return _default_state()
    try:
        with open(STORY_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return _default_state()

    state = _default_state()
    state.update({k: v for k, v in data.items() if k not in {"relationships", "doctrine", "flags", "progress"}})
    state["relationships"].update(data.get("relationships", {}))
    state["doctrine"].update(data.get("doctrine", {}))
    state["flags"].update(data.get("flags", {}))
    state["progress"].update(data.get("progress", {}))
    state["choices"] = data.get("choices", state["choices"])
    state["recent_scenes"] = data.get("recent_scenes", state["recent_scenes"])
    state["chapter_history"] = data.get("chapter_history", state["chapter_history"])
    for division in CORE_DIVISIONS:
        state["relationships"].setdefault(division, _relationship_template(division))
    return state


def _save_state(state: dict) -> dict:
    state["last_updated"] = _now()
    atomic_write_json(STORY_FILE, state, ensure_ascii=False)
    return state


def _active_divisions(state: dict) -> list[str]:
    return list(state.get("flags", {}).get("active_divisions", []))


def _clamp(value: int, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, value))


def _relationship_stance(rel: dict) -> str:
    trust = rel.get("trust", 50)
    tension = rel.get("tension", 15)
    if trust >= 72 and tension <= 28:
        return "loyal"
    if tension >= 60:
        return "strained"
    if trust >= 58:
        return "aligned"
    return "watchful"


def _apply_relationship_delta(state: dict, division: str, trust_delta: int = 0, tension_delta: int = 0, event: str = "") -> None:
    rel = state["relationships"].setdefault(division, _relationship_template(division))
    rel["trust"] = _clamp(rel.get("trust", 50) + trust_delta)
    rel["tension"] = _clamp(rel.get("tension", 15) + tension_delta)
    rel["stance"] = _relationship_stance(rel)
    if event:
        rel["last_event"] = event


def _scene_seen(state: dict, key: str) -> bool:
    return bool(state.get("flags", {}).get("scene_flags", {}).get(key))


def _mark_scene_seen(state: dict, key: str) -> None:
    state.setdefault("flags", {}).setdefault("scene_flags", {})[key] = True


def _current_chapter_meta(state: dict) -> dict:
    return {
        "id": state.get("chapter", 0),
        "key": state.get("chapter_key", "prologue"),
        "label": state.get("chapter_label", "Prologue"),
        "title": state.get("chapter_title", "The Awakening"),
        "summary": state.get("chapter_summary", ""),
    }


def _push_scene(
    state: dict,
    *,
    scene_key: str,
    title: str,
    narrative: str,
    division: str = "",
    icon: str = "⚔",
    color: str = "",
    commander: str = "",
    order: str = "",
    scene_type: str = "story_scene",
) -> dict | None:
    if scene_key and _scene_seen(state, scene_key):
        return None
    if scene_key:
        _mark_scene_seen(state, scene_key)

    div_meta = DIVISIONS.get(division, {})
    scene = {
        "id": f"scene-{len(state.get('recent_scenes', [])) + 1}",
        "scene_key": scene_key,
        "type": scene_type,
        "title": title,
        "narrative": narrative,
        "division": division,
        "commander": commander or div_meta.get("commander", ""),
        "order": order or div_meta.get("order", ""),
        "icon": icon,
        "color": color or div_meta.get("color", "#7c3aed"),
        "chapter": _current_chapter_meta(state),
        "ts": _now(),
    }
    scenes = state.setdefault("recent_scenes", [])
    scenes.append(scene)
    state["recent_scenes"] = scenes[-SCENE_LIMIT:]

    if _aq:
        try:
            _aq.push_story_scene(
                title=scene["title"],
                narration=scene["narrative"],
                division=scene["division"],
                commander=scene["commander"],
                color=scene["color"],
                icon=scene["icon"],
                chapter=scene["chapter"],
                story_key=scene_key,
            )
        except Exception as e:
            log.warning("story scene queue failed (non-fatal): %s", e)
    return scene


def _update_active_arc(state: dict) -> str:
    doctrine = state.setdefault("doctrine", {"aggressive": 0, "patient": 0, "balanced": 0, "dominant": "balanced"})
    if len(state.get("choices", [])) < 3:
        previous = doctrine.get("dominant", "balanced")
        doctrine["dominant"] = "balanced"
        state["active_arc"] = ARC_DEFS["balanced"].copy()
        return previous
    dominant = max(
        ("aggressive", "patient", "balanced"),
        key=lambda key: (doctrine.get(key, 0), 1 if key == "balanced" else 0),
    )
    previous = doctrine.get("dominant", "balanced")
    doctrine["dominant"] = dominant
    state["active_arc"] = ARC_DEFS[dominant].copy()
    return previous


def _unlock_chapters(state: dict) -> list[dict]:
    unlocked = []
    chapter_keys = set(state.get("flags", {}).get("chapters_unlocked", []))
    current_id = state.get("chapter", 0)
    for chapter in CHAPTERS[1:]:
        if chapter["key"] in chapter_keys:
            continue
        if chapter["unlock"](state):
            chapter_keys.add(chapter["key"])
            state["flags"]["chapters_unlocked"] = list(chapter_keys)
            if chapter["id"] > current_id:
                state["chapter"] = chapter["id"]
                state["chapter_key"] = chapter["key"]
                state["chapter_label"] = chapter["label"]
                state["chapter_title"] = chapter["title"]
                state["chapter_summary"] = chapter["summary"]
                current_id = chapter["id"]
            entry = {
                "chapter": chapter["id"],
                "chapter_key": chapter["key"],
                "title": chapter["title"],
                "summary": chapter["summary"],
                "ts": _now(),
            }
            state.setdefault("chapter_history", []).append(entry)
            unlocked.append(entry)
            _push_scene(
                state,
                scene_key=f"chapter:{chapter['key']}",
                title=chapter["title"],
                narrative=chapter["summary"],
                icon="✦",
                scene_type="chapter_unlock",
            )
    state["chapter_history"] = state.get("chapter_history", [])[-12:]
    return unlocked


def record_event(event: str, **data) -> dict:
    state = _load_state()
    progress = state.setdefault("progress", {})
    flags = state.setdefault("flags", {})
    flags.setdefault("active_divisions", [])
    flags.setdefault("first_activation", {})
    progress.setdefault("battles", 0)
    progress.setdefault("crisis_events", 0)

    division = data.get("division", "")
    if event == "skill_complete":
        progress["battles"] += 1
        progress["last_division"] = division
        status = data.get("status", "success")
        escalate = bool(data.get("escalate", False))
        if division in CORE_DIVISIONS and division not in flags["active_divisions"]:
            flags["active_divisions"].append(division)
        if division in CORE_DIVISIONS and not flags["first_activation"].get(division):
            flags["first_activation"][division] = True
            scene = FIRST_ACTIVATION_SCENES.get(division)
            if scene:
                _push_scene(
                    state,
                    scene_key=f"first_activation:{division}",
                    title=scene[0],
                    narrative=scene[1],
                    division=division,
                    icon="⚔",
                )
        trust_delta = 1 if status == "success" else -1 if status == "failed" else 0
        tension_delta = 4 if status == "failed" else 2 if status == "partial" else 0
        if escalate:
            tension_delta += 2
            progress["crisis_events"] += 1
            _push_scene(
                state,
                scene_key=f"crisis:{division}:{progress['crisis_events']}",
                title=f"{DIVISIONS.get(division, {}).get('commander', division)} Requests Judgment",
                narrative=(
                    data.get("summary")
                    or f"The {DIVISIONS.get(division, {}).get('order', division)} reports strain in the field. "
                       "The realm can keep advancing, but the court will remember the cost."
                ),
                division=division,
                icon="⚠",
            )
        _apply_relationship_delta(state, division, trust_delta=trust_delta, tension_delta=tension_delta, event=event)
        if len(flags["active_divisions"]) == len(CORE_DIVISIONS):
            _push_scene(
                state,
                scene_key="milestone:all_orders_active",
                title="The Court Stands Complete",
                narrative="Every order now speaks into the same realm. Coordination, not activation, becomes the next real challenge.",
                icon="♜",
            )

    elif event == "rank_up" and division in CORE_DIVISIONS:
        tier = int(data.get("tier", 0) or 0)
        _apply_relationship_delta(state, division, trust_delta=3, tension_delta=-1, event=event)
        if tier >= 4:
            _push_scene(
                state,
                scene_key=f"legendary:{division}",
                title=f"{DIVISIONS.get(division, {}).get('commander', division)} Reaches Legendary Command",
                narrative=(
                    f"The {DIVISIONS.get(division, {}).get('order', division)} has crossed into its highest form. "
                    "The rest of the court now has to adjust to a commander operating at legendary weight."
                ),
                division=division,
                icon="👑",
            )

    elif event == "streak_milestone" and division in CORE_DIVISIONS:
        _apply_relationship_delta(state, division, trust_delta=2, tension_delta=-1, event=event)

    elif event == "prestige":
        progress["prestige"] = int(data.get("prestige", progress.get("prestige", 0) or 0))
        for div in CORE_DIVISIONS:
            _apply_relationship_delta(state, div, trust_delta=2, tension_delta=-2, event=event)
        _push_scene(
            state,
            scene_key=f"prestige:{progress['prestige']}",
            title=f"Prestige {progress['prestige']} - The Cycle Breaks",
            narrative="The court watches the realm reset without forgetting what was learned. Prestige changes the stakes of every future order.",
            icon="✦",
        )

    elif event == "xp_grant":
        progress["level"] = max(progress.get("level", 1), int(data.get("level", progress.get("level", 1))))

    previous_arc = _update_active_arc(state)
    _unlock_chapters(state)
    _save_state(state)
    return {
        "state": state,
        "previous_arc": previous_arc,
        "active_arc": state.get("active_arc", {}).get("id", "balanced"),
    }


def apply_choice(division: str, choice_id: str, choice_text: str = "") -> dict:
    state = _load_state()
    doctrine = state.setdefault("doctrine", {"aggressive": 0, "patient": 0, "balanced": 0, "dominant": "balanced"})
    if choice_id not in ("aggressive", "patient", "balanced"):
        raise ValueError(f"Unsupported choice_id: {choice_id}")

    entry = {
        "division": division,
        "choice_id": choice_id,
        "choice_text": choice_text,
        "chapter": state.get("chapter", 0),
        "chapter_title": state.get("chapter_title", ""),
        "speaker": DIVISIONS.get(division, {}).get("commander", division),
        "ts": _now(),
    }
    state.setdefault("choices", []).append(entry)
    state["choices"] = state["choices"][-CHOICE_LIMIT:]
    state["last_choice"] = entry

    doctrine[choice_id] = doctrine.get(choice_id, 0) + 1
    previous_arc = _update_active_arc(state)
    effects = CHOICE_EFFECTS.get(division, {}).get(choice_id, {"trust": 1, "tension": 0})
    if division in CORE_DIVISIONS:
        _apply_relationship_delta(
            state,
            division,
            trust_delta=effects.get("trust", 0),
            tension_delta=effects.get("tension", 0),
            event=f"choice:{choice_id}",
        )

    if previous_arc != state["active_arc"]["id"] and len(state.get("choices", [])) >= 3:
        _push_scene(
            state,
            scene_key=f"arc_shift:{state['active_arc']['id']}",
            title=state["active_arc"]["name"],
            narrative=state["active_arc"]["summary"],
            icon="◆",
        )

    _push_scene(
        state,
        scene_key=f"choice:{division}:{len(state['choices'])}",
        title=f"{DIVISIONS.get(division, {}).get('commander', division)} Takes the Order",
        narrative=CHOICE_REACTIONS.get(choice_id, "The court adjusts to the sovereign's choice."),
        division=division,
        icon="☍",
    )

    _unlock_chapters(state)
    _save_state(state)
    return state


def current_state() -> dict:
    state = _load_state()
    _update_active_arc(state)
    _unlock_chapters(state)
    return _save_state(state)
