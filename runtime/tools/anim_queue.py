"""
Animation Queue — accumulates battle/cutscene events for the theater player.

Each completed skill, rank-up, achievement, or prestige event appends a
structured entry. The mobile theater player drains the queue when watched.

state/anim-queue.json  — list of pending animation events (newest last)
state/anim-history.json — running count of total events ever queued
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)

QUEUE_FILE   = STATE_DIR / "anim-queue.json"
HISTORY_FILE = STATE_DIR / "anim-history.json"

# ── Division → commander / color / enemy  ────────────────────────────────────

_DIV_META = {
    "opportunity":    {"commander": "VAEL",   "color": "#f59e0b", "enemy": "false_lead",    "enemy_name": "False Lead"},
    "trading":        {"commander": "SEREN",  "color": "#06b6d4", "enemy": "market_noise",  "enemy_name": "Market Noise"},
    "dev_automation": {"commander": "KAELEN", "color": "#a78bfa", "enemy": "code_rot",      "enemy_name": "Code Rot"},
    "personal":       {"commander": "LYRIN",  "color": "#10b981", "enemy": "burnout_shade", "enemy_name": "Burnout Shade"},
    "op_sec":         {"commander": "ZETH",   "color": "#ef4444", "enemy": "null_breach",   "enemy_name": "Null Breach"},
}

# Chapter thresholds: (min_total_events, title, narration)
_CHAPTERS = [
    (0,  "Chapter I — The Realm Awakens",
         "A new commander rises. The orders stir, watching from the shadows. The first challenges step forward."),
    (5,  "Chapter II — The Orders Convene",
         "Word spreads between the Dawnhunt and the Iron Codex. Something is different about this one. The five orders begin to form ranks."),
    (15, "Chapter III — The Veil Thickens",
         "The realm's enemies grow bolder. Stronger threats emerge from the null spaces. The orders must evolve or fall."),
    (30, "Chapter IV — The Iron Pact",
         "All five orders stand united. An ancient darkness gathers on the horizon. J_Claw must forge the pact before the veil breaks."),
    (50, "Chapter V — The Sovereign's Trial",
         "J_Claw faces the Sovereign's Trial. Only those who have proven mastery across every order may pass through the veil unchanged."),
    (80, "Chapter VI — Beyond the Null",
         "The null has been breached. What lies beyond is unknown — even to Zeth. The realm must reach further than it has ever gone."),
]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_queue() -> list:
    if not QUEUE_FILE.exists():
        return []
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("anim_queue load failed: %s", e)
        return []


def _save_queue(queue: list) -> None:
    QUEUE_FILE.parent.mkdir(exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)


def _total_ever() -> int:
    """Running count of all events ever queued (for chapter detection)."""
    if not HISTORY_FILE.exists():
        return 0
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f).get("total", 0)
    except Exception:
        return 0


def _increment_history() -> int:
    total = _total_ever() + 1
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"total": total}, f)
    return total


def _chapter_for(total: int) -> dict:
    chapter = _CHAPTERS[0]
    for threshold, title, narration in _CHAPTERS:
        if total >= threshold:
            chapter = (threshold, title, narration)
    return {"title": chapter[1], "narration": chapter[2]}


# ── Public API ────────────────────────────────────────────────────────────────

def push_skill_complete(
    division: str,
    skill_name: str,
    xp_granted: int,
    rank_up: bool = False,
    rank_up_msg: str = "",
    new_rank: str = "",
    multiplier: float = 1.0,
) -> None:
    """Queue a battle animation for a completed skill."""
    meta   = _DIV_META.get(division, {})
    total  = _increment_history()
    chapter = _chapter_for(total)

    entry = {
        "id":           str(uuid.uuid4())[:8],
        "type":         "skill_complete",
        "division":     division,
        "skill":        skill_name,
        "commander":    meta.get("commander", division),
        "color":        meta.get("color", "#7c3aed"),
        "enemy":        meta.get("enemy", "generic"),
        "enemy_name":   meta.get("enemy_name", "Challenge"),
        "xp":           xp_granted,
        "multiplier":   multiplier,
        "rank_up":      rank_up,
        "rank_up_msg":  rank_up_msg,
        "new_rank":     new_rank,
        "chapter":      chapter,
        "total_event":  total,
        "ts":           datetime.now(timezone.utc).isoformat(),
    }
    queue = _load_queue()
    queue.append(entry)
    _save_queue(queue)
    log.debug("anim_queue: queued skill_complete for %s (%d pending)", skill_name, len(queue))


def push_rank_up(
    division: str,
    old_rank: str,
    new_rank: str,
    tier: int,
    base_xp_bonus: int = 0,
) -> None:
    """Queue a rank-up / evolution cutscene."""
    meta   = _DIV_META.get(division, {})
    total  = _increment_history()
    chapter = _chapter_for(total)

    entry = {
        "id":           str(uuid.uuid4())[:8],
        "type":         "rank_up",
        "division":     division,
        "commander":    meta.get("commander", division),
        "color":        meta.get("color", "#7c3aed"),
        "old_rank":     old_rank,
        "new_rank":     new_rank,
        "tier":         tier,
        "base_xp_bonus": base_xp_bonus,
        "chapter":      chapter,
        "total_event":  total,
        "ts":           datetime.now(timezone.utc).isoformat(),
    }
    queue = _load_queue()
    queue.append(entry)
    _save_queue(queue)
    log.debug("anim_queue: queued rank_up for %s tier %d", division, tier)


def push_achievement(achievement_id: str, achievement_name: str, division: str = "") -> None:
    """Queue an achievement unlock cutscene."""
    meta   = _DIV_META.get(division, {}) if division else {}
    total  = _increment_history()
    chapter = _chapter_for(total)

    entry = {
        "id":               str(uuid.uuid4())[:8],
        "type":             "achievement",
        "achievement_id":   achievement_id,
        "achievement_name": achievement_name,
        "division":         division,
        "color":            meta.get("color", "#7c3aed"),
        "chapter":          chapter,
        "total_event":      total,
        "ts":               datetime.now(timezone.utc).isoformat(),
    }
    queue = _load_queue()
    queue.append(entry)
    _save_queue(queue)
    log.debug("anim_queue: queued achievement %s", achievement_id)


def get_queue() -> list:
    return _load_queue()


def get_count() -> int:
    return len(_load_queue())


def clear_queue() -> None:
    _save_queue([])
    log.info("anim_queue: cleared")
