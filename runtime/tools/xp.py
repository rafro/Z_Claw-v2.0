"""
Realm Keeper XP and rank math — pure Python, no LLM.
Sole writer of jclaw-stats.json. J_Claw and division orchestrators read-only.

All world data (rank tables, XP values, thresholds) imported from
runtime.realm.config — the single source of truth.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import STATE_DIR
from runtime.realm.config import (
    DIVISIONS, BASE_RANKS, XP_PER_LEVEL, DIV_XP_THRESHOLDS,
    RANK_UP_BASE_XP, ACHIEVEMENTS,
    get_all_skill_xp, rank_title_for_xp, tier_for_xp,
)
try:
    from runtime.tools import anim_queue as _aq
except Exception:
    _aq = None

log = logging.getLogger(__name__)

STATS_FILE = STATE_DIR / "jclaw-stats.json"

# Flat skill→{division, xp, soldier, label} from config
_SKILL_XP = get_all_skill_xp()


# ── XP / level helpers ────────────────────────────────────────────────────────

def _xp_for_next_level(level: int) -> int:
    """XP to advance FROM this level."""
    if level < len(XP_PER_LEVEL):
        return XP_PER_LEVEL[level]
    return round(2100 * (1.3 ** (level - 9)))


def _level_from_xp(base_xp: int) -> int:
    """Derive level from accumulated base_xp."""
    level, remaining = 1, base_xp
    while remaining >= _xp_for_next_level(level):
        remaining -= _xp_for_next_level(level)
        level += 1
    return level


def _base_rank(level: int) -> str:
    for entry in BASE_RANKS:
        if level >= entry["min_level"]:
            return entry["title"]
    return "Apprentice of the Realm"


# ── State read/write ──────────────────────────────────────────────────────────

def _streak_entry() -> dict:
    return {"current": 0, "longest": 0, "last_date": None, "shield_this_week": False, "week": None}


def _empty_stats() -> dict:
    divs    = {k: {"xp": 0, "rank": d["ranks"][0]} for k, d in DIVISIONS.items()}
    streaks = {k: _streak_entry() for k in DIVISIONS}
    return {
        "base_xp":               0,
        "level":                 1,
        "rank":                  "Apprentice of the Realm",
        "xp_to_next_level":      100,
        "total_xp_earned":       0,
        "total_rewards_from_ruler": 0,
        "divisions":             divs,
        "streaks":               streaks,
        "achievements":          [],
        "last_updated":          None,
    }


def _load_stats() -> dict:
    if not STATS_FILE.exists():
        return _empty_stats()
    try:
        with open(STATS_FILE, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        log.error("Failed to load jclaw-stats.json: %s", e)
        return _empty_stats()


def _save_stats(stats: dict) -> None:
    stats["last_updated"] = datetime.now(timezone.utc).isoformat()
    STATS_FILE.parent.mkdir(exist_ok=True)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)


# ── Multipliers ───────────────────────────────────────────────────────────────

def _streak_multiplier(stats: dict, division: str) -> float:
    """Streak XP multiplier: +10% per 7-day milestone, capped at ×1.5."""
    streak = stats.get("streaks", {}).get(division, {}).get("current", 0)
    return min(1.5, 1.0 + (streak // 7) * 0.1)


# ── Achievement check ─────────────────────────────────────────────────────────

def _check_achievements(stats: dict) -> list:
    """Return list of newly unlocked achievement IDs."""
    earned   = set(stats.get("achievements", []))
    new_ones = []
    divs     = stats.get("divisions", {})
    streaks  = stats.get("streaks", {})
    level    = stats.get("level", 1)

    for ach in ACHIEVEMENTS:
        aid  = ach["id"]
        cond = ach.get("condition", {})
        if aid in earned or cond.get("type") == "manual":
            continue

        unlocked = False
        ctype    = cond.get("type")

        if ctype == "division_xp_gt":
            unlocked = (divs.get(cond["division"], {}).get("xp", 0) > cond["value"])
        elif ctype == "any_division_xp_gte":
            unlocked = any(d.get("xp", 0) >= cond["value"] for d in divs.values())
        elif ctype == "all_divisions_xp_gt":
            unlocked = all(d.get("xp", 0) > cond["value"] for d in divs.values()) and len(divs) >= 5
        elif ctype == "base_level_gte":
            unlocked = level >= cond["value"]
        elif ctype == "any_streak_gte":
            unlocked = any(s.get("longest", 0) >= cond["value"] for s in streaks.values())

        if unlocked:
            new_ones.append(aid)
            earned.add(aid)

    if new_ones:
        stats["achievements"] = list(earned)
    return new_ones


# ── Public API ────────────────────────────────────────────────────────────────

def grant_skill_xp(skill_name: str) -> dict:
    """
    Grant division XP for a completed skill.
    Returns progression_packet dict.
    Division XP NEVER converts to base XP automatically —
    except on rank-up: each tier crossed auto-grants a small base XP bonus.
    """
    skill_data = _SKILL_XP.get(skill_name)
    if not skill_data:
        return {"skill": skill_name, "xp_granted": 0, "rank_up": False}

    division   = skill_data["division"]
    xp_amount  = skill_data["xp"]
    stats      = _load_stats()

    # Apply multipliers
    streak_mult   = _streak_multiplier(stats, division)
    prestige_mult = stats.get("prestige_multiplier", 1.0)
    xp_actual     = round(xp_amount * streak_mult * prestige_mult)

    # Update division XP and rank
    div_stats    = stats["divisions"].setdefault(division, {"xp": 0, "rank": ""})
    old_div_xp   = div_stats.get("xp", 0)
    old_tier     = tier_for_xp(old_div_xp)
    old_div_rank = div_stats.get("rank", "")

    div_stats["xp"]  = old_div_xp + xp_actual
    new_tier         = tier_for_xp(div_stats["xp"])
    new_div_rank     = rank_title_for_xp(division, div_stats["xp"])
    div_stats["rank"] = new_div_rank
    stats["divisions"][division] = div_stats

    # Rank-up detected — auto-grant base XP for each tier crossed
    rank_up        = new_div_rank != old_div_rank and old_div_rank != ""
    base_xp_bonus  = 0
    base_level_up  = False
    crossed_tiers  = []

    if new_tier > old_tier:
        for t in range(old_tier + 1, new_tier + 1):
            bonus = RANK_UP_BASE_XP.get(t, 0)
            base_xp_bonus += bonus
            crossed_tiers.append(t)

        if base_xp_bonus > 0:
            stats["base_xp"]         = stats.get("base_xp", 0) + base_xp_bonus
            stats["total_xp_earned"] = stats.get("total_xp_earned", 0) + base_xp_bonus
            old_level  = stats.get("level", 1)
            new_level  = _level_from_xp(stats["base_xp"])
            stats["level"]           = new_level
            stats["rank"]            = _base_rank(new_level)
            stats["xp_to_next_level"]= _xp_for_next_level(new_level)
            base_level_up = new_level > old_level

    # Check achievements
    new_achievements = _check_achievements(stats)

    _save_stats(stats)

    # ── Queue animation events ─────────────────────────────────────────────
    if _aq:
        try:
            _aq.push_skill_complete(
                division=division,
                skill_name=skill_name,
                xp_granted=xp_actual,
                rank_up=rank_up,
                rank_up_msg=f"{old_div_rank} → {new_div_rank}" if rank_up else "",
                new_rank=new_div_rank,
                multiplier=round(streak_mult * prestige_mult, 3),
            )
            if new_tier > old_tier:
                _aq.push_rank_up(
                    division=division,
                    old_rank=old_div_rank,
                    new_rank=new_div_rank,
                    tier=new_tier,
                    base_xp_bonus=base_xp_bonus,
                )
            from runtime.realm.config import ACHIEVEMENTS as _ACH_LIST
            for aid in new_achievements:
                ach_data = next((a for a in _ACH_LIST if a["id"] == aid), {})
                _aq.push_achievement(aid, ach_data.get("name", aid), division=division)
        except Exception as _e:
            log.warning("anim_queue push failed (non-fatal): %s", _e)

    log.info(
        "XP granted: %s +%d div XP (×%.1f streak, ×%.2f prestige) (%s → %s)%s",
        skill_name, xp_actual, streak_mult, prestige_mult,
        old_div_rank, new_div_rank,
        f" | +{base_xp_bonus} base XP (tier {crossed_tiers})" if base_xp_bonus else "",
    )

    return {
        "skill":            skill_name,
        "division":         division,
        "xp_granted":       xp_actual,
        "multiplier":       round(streak_mult * prestige_mult, 3),
        "division_xp":      div_stats["xp"],
        "division_rank":    new_div_rank,
        "rank_up":          rank_up,
        "rank_up_msg":      f"{old_div_rank} → {new_div_rank}" if rank_up else "",
        "base_xp_bonus":    base_xp_bonus,
        "base_level_up":    base_level_up,
        "crossed_tiers":    crossed_tiers,
        "new_achievements": new_achievements,
    }


def grant_base_xp(amount: int, reason: str = "") -> dict:
    """
    Grant base XP. ONLY called directly by /reward from Matthew.
    This is what drives level and base rank beyond division rank-up bonuses.
    """
    stats     = _load_stats()
    old_level = stats.get("level", 1)
    old_rank  = stats.get("rank", "")

    stats["base_xp"]         = stats.get("base_xp", 0) + amount
    stats["total_xp_earned"] = stats.get("total_xp_earned", 0) + amount
    stats["total_rewards_from_ruler"] = stats.get("total_rewards_from_ruler", 0) + 1

    new_level = _level_from_xp(stats["base_xp"])
    new_rank  = _base_rank(new_level)
    stats["level"]            = new_level
    stats["rank"]             = new_rank
    stats["xp_to_next_level"] = _xp_for_next_level(new_level)

    new_achievements = _check_achievements(stats)
    rank_up  = new_rank  != old_rank
    level_up = new_level > old_level
    _save_stats(stats)

    log.info("Base XP granted: +%d (now %d) | Lvl %d | %s", amount, stats["base_xp"], new_level, new_rank)

    return {
        "xp_granted":       amount,
        "reason":           reason,
        "base_xp":          stats["base_xp"],
        "level":            new_level,
        "rank":             new_rank,
        "level_up":         level_up,
        "rank_up":          rank_up,
        "rank_up_msg":      f"{old_rank} → {new_rank}" if rank_up else "",
        "new_achievements": new_achievements,
    }


def current_stats() -> dict:
    return _load_stats()
