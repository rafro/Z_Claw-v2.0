"""
Realm Keeper XP and rank math - pure Python, no LLM.
Primary writer for Python-driven progression state and game events.

All world data (rank tables, XP values, thresholds) comes from
runtime.realm.config - the single source of truth.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from runtime import packet as packet_io
from runtime.config import STATE_DIR
from runtime.realm import chronicle, events as realm_events, story as realm_story
from runtime.tools.atomic_write import atomic_write_json
from runtime.realm.config import (
    ACHIEVEMENTS,
    BASE_RANKS,
    DIVISIONS,
    RANK_UP_BASE_XP,
    XP_PER_LEVEL,
    get_all_skill_xp,
    rank_title_for_xp,
    tier_for_xp,
)

try:
    from runtime.tools import anim_queue as _aq
except Exception:
    _aq = None

log = logging.getLogger(__name__)

STATS_FILE = STATE_DIR / "jclaw-stats.json"
XP_HISTORY_FILE = STATE_DIR / "xp-history.jsonl"

_PACKET_LOOKUP_OVERRIDES = {
    "hard-filter": ("opportunity", "job-intake"),
    "sentinel-health": ("sentinel", "provider-health"),
}
_PACKET_DIVISION_ALIASES = {
    "dev_automation": "dev-automation",
    "op_sec": "op-sec",
}

_SKILL_XP = get_all_skill_xp()


def _xp_for_next_level(level: int) -> int:
    if level < len(XP_PER_LEVEL):
        return XP_PER_LEVEL[level]
    return round(2100 * (1.3 ** (level - 9)))


def _level_from_xp(base_xp: int) -> int:
    level, remaining = 1, base_xp
    while remaining >= _xp_for_next_level(level):
        remaining -= _xp_for_next_level(level)
        level += 1
    return level


def _base_progress(base_xp: int) -> tuple[int, int, int, int]:
    total_xp = max(0, int(base_xp or 0))
    level = 1
    xp_into_level = total_xp
    xp_for_next_level = _xp_for_next_level(level)

    while xp_into_level >= xp_for_next_level:
        xp_into_level -= xp_for_next_level
        level += 1
        xp_for_next_level = _xp_for_next_level(level)

    xp_to_next_level = max(0, xp_for_next_level - xp_into_level)
    return level, xp_into_level, xp_for_next_level, xp_to_next_level


def _base_rank(level: int) -> str:
    for entry in BASE_RANKS:
        if level >= entry["min_level"]:
            return entry["title"]
    return "Apprentice of the Realm"


def _streak_entry() -> dict:
    return {
        "current": 0,
        "longest": 0,
        "last_date": None,
        "shield_this_week": False,
        "week": None,
    }


def _empty_stats() -> dict:
    divisions = {k: {"xp": 0, "rank": d["ranks"][0]} for k, d in DIVISIONS.items()}
    streaks = {k: _streak_entry() for k in DIVISIONS}
    return {
        "base_xp": 0,
        "xp_into_level": 0,
        "xp_for_next_level": 100,
        "level": 1,
        "rank": "Apprentice of the Realm",
        "xp_to_next_level": 100,
        "total_xp_earned": 0,
        "total_rewards_from_ruler": 0,
        "divisions": divisions,
        "streaks": streaks,
        "achievements": [],
        "prestige": 0,
        "prestige_multiplier": 1.0,
        "last_updated": None,
    }


def _refresh_base_progress(stats: dict) -> dict:
    stats["base_xp"] = max(0, int(stats.get("base_xp", 0) or 0))
    level, xp_into_level, xp_for_next_level, xp_to_next_level = _base_progress(stats["base_xp"])
    stats["level"] = level
    stats["rank"] = _base_rank(level)
    stats["xp_into_level"] = xp_into_level
    stats["xp_for_next_level"] = xp_for_next_level
    stats["xp_to_next_level"] = xp_to_next_level
    return stats


def _hydrate_stats(stats: dict) -> dict:
    defaults = _empty_stats()
    for key, value in defaults.items():
        stats.setdefault(key, value)

    stats["divisions"] = stats.get("divisions") or {}
    stats["streaks"] = stats.get("streaks") or {}
    for div_key, div_default in defaults["divisions"].items():
        stats["divisions"].setdefault(div_key, div_default.copy())
        stats["streaks"].setdefault(div_key, _streak_entry())

    if not isinstance(stats.get("prestige_multiplier"), (int, float)):
        stats["prestige_multiplier"] = 1.0
    return _refresh_base_progress(stats)


def _load_stats() -> dict:
    if not STATS_FILE.exists():
        return _empty_stats()
    try:
        with open(STATS_FILE, encoding="utf-8-sig") as f:
            return _hydrate_stats(json.load(f))
    except Exception as e:
        log.error("Failed to load jclaw-stats.json: %s", e)
        return _empty_stats()


def _save_stats(stats: dict) -> None:
    _refresh_base_progress(stats)
    stats["last_updated"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(STATS_FILE, stats)


def _append_xp_history(entry: dict) -> None:
    try:
        XP_HISTORY_FILE.parent.mkdir(exist_ok=True)
        with open(XP_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                **entry,
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("xp history write failed (non-fatal): %s", e)


def _streak_multiplier(stats: dict, division: str) -> float:
    streak = stats.get("streaks", {}).get(division, {}).get("current", 0)
    return min(1.5, 1.0 + (streak // 7) * 0.1)


def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def _update_streak(stats: dict, division: str) -> int | bool:
    entry = stats.setdefault("streaks", {}).setdefault(division, _streak_entry())
    now = datetime.now(timezone.utc)
    today_date = now.date()
    today = today_date.isoformat()
    week = _week_key(now)

    if entry.get("week") != week:
        entry["shield_this_week"] = False
        entry["week"] = week

    if entry.get("last_date") == today:
        return False

    current = max(0, int(entry.get("current", 0) or 0))
    last_date_raw = entry.get("last_date")
    if not last_date_raw:
        entry["current"] = 1
    else:
        try:
            last_date = datetime.fromisoformat(last_date_raw).date()
        except ValueError:
            entry["current"] = 1
        else:
            gap_days = (today_date - last_date).days
            if gap_days == 1:
                entry["current"] = current + 1 if current else 1
            elif gap_days == 2 and not entry.get("shield_this_week"):
                entry["shield_this_week"] = True
                entry["current"] = current + 1 if current else 1
            else:
                entry["current"] = 1

    entry["longest"] = max(entry.get("longest", 0), entry["current"])
    entry["last_date"] = today
    return entry["current"] if entry["current"] > 0 and entry["current"] % 7 == 0 else False


def _check_achievements(stats: dict) -> list:
    earned = set(stats.get("achievements", []))
    new_ones = []
    divisions = stats.get("divisions", {})
    streaks = stats.get("streaks", {})
    level = stats.get("level", 1)

    for ach in ACHIEVEMENTS:
        aid = ach["id"]
        cond = ach.get("condition", {})
        if aid in earned or cond.get("type") == "manual":
            continue

        unlocked = False
        ctype = cond.get("type")
        if ctype == "division_xp_gt":
            unlocked = divisions.get(cond["division"], {}).get("xp", 0) > cond["value"]
        elif ctype == "any_division_xp_gte":
            unlocked = any(d.get("xp", 0) >= cond["value"] for d in divisions.values())
        elif ctype == "all_divisions_xp_gt":
            unlocked = all(d.get("xp", 0) > cond["value"] for d in divisions.values()) and len(divisions) >= len(DIVISIONS)
        elif ctype == "base_level_gte":
            unlocked = level >= cond["value"]
        elif ctype == "any_streak_gte":
            unlocked = any(s.get("longest", 0) >= cond["value"] for s in streaks.values())
        elif ctype == "prestige_gte":
            unlocked = stats.get("prestige", 0) >= cond["value"]
        elif ctype == "divisions_xp_gte_count":
            threshold = cond.get("value", 0)
            count_needed = cond.get("count", 1)
            unlocked = sum(1 for d in divisions.values() if d.get("xp", 0) >= threshold) >= count_needed

        if unlocked:
            new_ones.append(aid)
            earned.add(aid)

    if new_ones:
        stats["achievements"] = list(earned)
    return new_ones


def _check_auto_prestige(stats: dict) -> dict | None:
    core_divisions = list(DIVISIONS.keys())
    if not core_divisions:
        return None
    if not all(stats.get("divisions", {}).get(d, {}).get("xp", 0) >= 500 for d in core_divisions):
        return None

    for div_key in core_divisions:
        stats["divisions"][div_key]["xp"] = 0
        stats["divisions"][div_key]["rank"] = DIVISIONS[div_key]["ranks"][0]

    stats["prestige"] = stats.get("prestige", 0) + 1
    stats["prestige_multiplier"] = round((1.0 + stats["prestige"] * 0.05) * 1000) / 1000
    return {
        "prestige": stats["prestige"],
        "multiplier": stats["prestige_multiplier"],
        "auto": True,
    }


def _packet_lookup(skill_name: str, division: str) -> tuple[str, str]:
    if skill_name in _PACKET_LOOKUP_OVERRIDES:
        return _PACKET_LOOKUP_OVERRIDES[skill_name]
    return (_PACKET_DIVISION_ALIASES.get(division, division), skill_name)


def _load_packet_context(skill_name: str, division: str) -> dict:
    packet_division, packet_skill = _packet_lookup(skill_name, division)
    try:
        return packet_io.read(packet_division, packet_skill) or {}
    except Exception:
        return {}


def _achievement_data(achievement_id: str) -> dict:
    return next((a for a in ACHIEVEMENTS if a["id"] == achievement_id), {})


def _read_commander_stance(division: str) -> str:
    """Read current commander stance from story state (pure file read, no LLM)."""
    try:
        import json as _json
        _sf = STATE_DIR / "story-state.json"
        if _sf.exists():
            _s = _json.loads(_sf.read_text(encoding="utf-8-sig"))
            return _s.get("relationships", {}).get(division, {}).get("stance", "watchful")
    except Exception:
        pass
    return "watchful"


def _emit_progression_side_effects(
    *,
    division: str,
    skill_name: str,
    xp_granted: int,
    multiplier: float,
    rank_up: bool,
    old_div_rank: str,
    new_div_rank: str,
    old_tier: int,
    new_tier: int,
    base_xp_bonus: int,
    streak_milestone: int | bool,
    streak_mult: float,
    current_streak: int,
    new_achievements: list[str],
    prestige_event: dict | None,
    stats: dict,
    packet_status: str,
    packet_escalate: bool,
    packet_ctx: dict,
    defeat_penalty: bool = False,
) -> None:
    if _aq:
        try:
            _aq.push_skill_complete(
                division=division,
                skill_name=skill_name,
                xp_granted=xp_granted,
                rank_up=rank_up,
                rank_up_msg=f"{old_div_rank} -> {new_div_rank}" if rank_up else "",
                new_rank=new_div_rank,
                multiplier=multiplier,
                status=packet_status,
                escalate=packet_escalate,
                escalation_reason=packet_ctx.get("escalation_reason", ""),
                summary=packet_ctx.get("summary", ""),
                urgency=packet_ctx.get("urgency", "normal"),
                provider_used=packet_ctx.get("provider_used", ""),
                defeat_penalty=defeat_penalty,
                commander_stance=_read_commander_stance(division),
            )
            if new_tier > old_tier:
                _aq.push_rank_up(
                    division=division,
                    old_rank=old_div_rank,
                    new_rank=new_div_rank,
                    tier=new_tier,
                    base_xp_bonus=base_xp_bonus,
                )
            for achievement_id in new_achievements:
                ach_data = _achievement_data(achievement_id)
                _aq.push_achievement(achievement_id, ach_data.get("name", achievement_id), division=division)
            if prestige_event:
                _aq.push_prestige(prestige_event["prestige"], prestige_event["multiplier"])
        except Exception as e:
            log.warning("anim_queue push failed (non-fatal): %s", e)

    if new_tier > old_tier:
        chronicle.log_rank_up(division, stats["divisions"][division]["xp"], old_tier, new_tier)
        realm_events.emit(
            "rank_up",
            division=division,
            old_rank=old_div_rank,
            new_rank=new_div_rank,
            tier=new_tier,
            base_xp_bonus=base_xp_bonus,
            level=stats.get("level", 1),
            rank=stats.get("rank", ""),
        )
        realm_story.record_event(
            "rank_up",
            division=division,
            tier=new_tier,
            rank=new_div_rank,
        )

    if streak_milestone:
        chronicle.log_streak_milestone(division, streak_milestone)
        realm_events.emit(
            "streak_milestone",
            division=division,
            streak=streak_milestone,
            multiplier=round(streak_mult, 3),
        )
        realm_story.record_event(
            "streak_milestone",
            division=division,
            streak=streak_milestone,
        )

    if streak_mult > 1.0:
        realm_events.emit(
            "streak_multiplier_applied",
            division=division,
            multiplier=round(streak_mult, 3),
            streak_days=current_streak,
        )

    for achievement_id in new_achievements:
        ach_data = _achievement_data(achievement_id)
        chronicle.log_achievement(achievement_id, ach_data)
        realm_events.emit(
            "achievement_unlock",
            achievement=achievement_id,
            division=division,
        )

    if prestige_event:
        chronicle.log_prestige(prestige_event["prestige"], prestige_event["multiplier"])
        realm_events.emit(
            "prestige",
            prestige=prestige_event["prestige"],
            multiplier=prestige_event["multiplier"],
            auto=True,
        )
        realm_story.record_event(
            "prestige",
            prestige=prestige_event["prestige"],
            multiplier=prestige_event["multiplier"],
        )
        _append_xp_history({
            "event": "prestige",
            "prestige": prestige_event["prestige"],
            "multiplier": prestige_event["multiplier"],
            "auto": prestige_event.get("auto", True),
        })


def grant_skill_xp(skill_name: str) -> dict:
    skill_data = _SKILL_XP.get(skill_name)
    if not skill_data:
        return {"skill": skill_name, "xp_granted": 0, "rank_up": False}

    division = skill_data["division"]
    xp_amount = skill_data["xp"]
    stats = _load_stats()
    packet_ctx = _load_packet_context(skill_name, division)
    packet_status = packet_ctx.get("status", "success")
    packet_escalate = packet_ctx.get("escalate", False)
    defeat_penalty = packet_status == "failed"
    if defeat_penalty:
        xp_amount = max(1, round(xp_amount * 0.5))
    streak_milestone = _update_streak(stats, division)

    streak_mult = _streak_multiplier(stats, division)
    prestige_mult = stats.get("prestige_multiplier", 1.0)
    actual_multiplier = round(streak_mult * prestige_mult, 3)
    xp_actual = round(xp_amount * streak_mult * prestige_mult)

    div_stats = stats["divisions"].setdefault(division, {"xp": 0, "rank": ""})
    old_div_xp = div_stats.get("xp", 0)
    old_tier = tier_for_xp(old_div_xp)
    old_div_rank = div_stats.get("rank", "")

    div_stats["xp"] = old_div_xp + xp_actual
    earned_div_xp = div_stats["xp"]
    new_tier = tier_for_xp(earned_div_xp)
    new_div_rank = rank_title_for_xp(division, earned_div_xp)
    div_stats["rank"] = new_div_rank
    stats["divisions"][division] = div_stats

    rank_up = new_div_rank != old_div_rank and old_div_rank != ""
    base_xp_bonus = 0
    base_level_up = False
    crossed_tiers = []

    if new_tier > old_tier:
        for tier in range(old_tier + 1, new_tier + 1):
            bonus = RANK_UP_BASE_XP.get(tier, 0)
            base_xp_bonus += bonus
            crossed_tiers.append(tier)

        if base_xp_bonus > 0:
            stats["base_xp"] = stats.get("base_xp", 0) + base_xp_bonus
            stats["total_xp_earned"] = stats.get("total_xp_earned", 0) + base_xp_bonus
            old_level = stats.get("level", 1)
            _refresh_base_progress(stats)
            base_level_up = stats["level"] > old_level

    new_achievements = _check_achievements(stats)
    prestige_event = _check_auto_prestige(stats)
    _save_stats(stats)

    current_streak = stats.get("streaks", {}).get(division, {}).get("current", 0)

    realm_events.emit(
        "skill_complete",
        division=division,
        skill=skill_name,
        xp_granted=xp_actual,
        multiplier=actual_multiplier,
        division_xp=earned_div_xp,
        division_rank=new_div_rank,
        streak=current_streak,
        status=packet_status,
        escalate=packet_escalate,
        escalation_reason=packet_ctx.get("escalation_reason", ""),
        summary=packet_ctx.get("summary", ""),
        urgency=packet_ctx.get("urgency", "normal"),
        provider_used=packet_ctx.get("provider_used", ""),
        soldier=skill_data.get("soldier", ""),
        label=skill_data.get("label", skill_name),
        icon=skill_data.get("icon", ""),
        anim=skill_data.get("anim", "slash"),
        base_xp_bonus=base_xp_bonus,
        base_level=stats.get("level", 1),
        base_rank=stats.get("rank", ""),
    )
    realm_story.record_event(
        "skill_complete",
        division=division,
        skill=skill_name,
        status=packet_status,
        escalate=packet_escalate,
        summary=packet_ctx.get("summary", ""),
    )
    _append_xp_history({
        "event": "skill_complete",
        "skill": skill_name,
        "div": division,
        "xp": xp_actual,
        "multiplier": actual_multiplier,
        "streak": current_streak,
        "status": packet_status,
        "escalate": packet_escalate,
    })

    _emit_progression_side_effects(
        division=division,
        skill_name=skill_name,
        xp_granted=xp_actual,
        multiplier=actual_multiplier,
        rank_up=rank_up,
        old_div_rank=old_div_rank,
        new_div_rank=new_div_rank,
        old_tier=old_tier,
        new_tier=new_tier,
        base_xp_bonus=base_xp_bonus,
        streak_milestone=streak_milestone,
        streak_mult=streak_mult,
        current_streak=current_streak,
        new_achievements=new_achievements,
        prestige_event=prestige_event,
        stats=stats,
        packet_status=packet_status,
        packet_escalate=packet_escalate,
        packet_ctx=packet_ctx,
        defeat_penalty=defeat_penalty,
    )

    log.info(
        "XP granted: %s +%d div XP (x%.1f streak, x%.2f prestige) (%s -> %s)%s",
        skill_name,
        xp_actual,
        streak_mult,
        prestige_mult,
        old_div_rank,
        new_div_rank,
        f" | +{base_xp_bonus} base XP (tier {crossed_tiers})" if base_xp_bonus else "",
    )

    return {
        "skill": skill_name,
        "division": division,
        "xp_granted": xp_actual,
        "multiplier": actual_multiplier,
        "division_xp": earned_div_xp,
        "division_rank": new_div_rank,
        "rank_up": rank_up,
        "rank_up_msg": f"{old_div_rank} -> {new_div_rank}" if rank_up else "",
        "base_xp_bonus": base_xp_bonus,
        "base_level_up": base_level_up,
        "crossed_tiers": crossed_tiers,
        "new_achievements": new_achievements,
    }


def grant_division_xp(division: str, amount: int, skill_name: str = "manual-bestow", reason: str = "") -> dict:
    if amount <= 0:
        raise ValueError("amount must be positive")
    if division not in DIVISIONS:
        raise ValueError(f"Unknown division: {division}")

    stats = _load_stats()
    streak_milestone = _update_streak(stats, division)
    streak_mult = _streak_multiplier(stats, division)
    prestige_mult = stats.get("prestige_multiplier", 1.0)
    actual_multiplier = round(streak_mult * prestige_mult, 3)
    xp_actual = round(amount * streak_mult * prestige_mult)

    div_stats = stats["divisions"].setdefault(division, {"xp": 0, "rank": DIVISIONS[division]["ranks"][0]})
    old_div_xp = div_stats.get("xp", 0)
    old_tier = tier_for_xp(old_div_xp)
    old_div_rank = div_stats.get("rank", DIVISIONS[division]["ranks"][0])

    div_stats["xp"] = old_div_xp + xp_actual
    earned_div_xp = div_stats["xp"]
    new_tier = tier_for_xp(earned_div_xp)
    new_div_rank = rank_title_for_xp(division, earned_div_xp)
    div_stats["rank"] = new_div_rank
    stats["divisions"][division] = div_stats

    rank_up = new_div_rank != old_div_rank and old_div_rank != ""
    base_xp_bonus = 0
    base_level_up = False
    crossed_tiers = []

    if new_tier > old_tier:
        for tier in range(old_tier + 1, new_tier + 1):
            bonus = RANK_UP_BASE_XP.get(tier, 0)
            base_xp_bonus += bonus
            crossed_tiers.append(tier)

        if base_xp_bonus > 0:
            stats["base_xp"] = stats.get("base_xp", 0) + base_xp_bonus
            stats["total_xp_earned"] = stats.get("total_xp_earned", 0) + base_xp_bonus
            old_level = stats.get("level", 1)
            _refresh_base_progress(stats)
            base_level_up = stats["level"] > old_level

    new_achievements = _check_achievements(stats)
    prestige_event = _check_auto_prestige(stats)
    _save_stats(stats)

    packet_ctx = {
        "status": "success",
        "escalate": False,
        "summary": reason or f"Manual XP bestow for {division}",
        "urgency": "normal",
        "provider_used": "manual",
    }
    current_streak = stats.get("streaks", {}).get(division, {}).get("current", 0)

    realm_events.emit(
        "skill_complete",
        division=division,
        skill=skill_name,
        xp_granted=xp_actual,
        multiplier=actual_multiplier,
        division_xp=earned_div_xp,
        division_rank=new_div_rank,
        streak=current_streak,
        status="success",
        escalate=False,
        escalation_reason="",
        summary=packet_ctx["summary"],
        urgency="normal",
        provider_used="manual",
        soldier=DIVISIONS[division].get("commander", ""),
        label=skill_name,
        icon="",
        anim="slash",
        base_xp_bonus=base_xp_bonus,
        base_level=stats.get("level", 1),
        base_rank=stats.get("rank", ""),
        reason=reason,
        grant_type="manual_division_xp",
    )
    realm_story.record_event(
        "skill_complete",
        division=division,
        skill=skill_name,
        status="success",
        escalate=False,
        summary=packet_ctx["summary"],
    )
    _append_xp_history({
        "event": "division_xp_grant",
        "skill": skill_name,
        "div": division,
        "xp": xp_actual,
        "multiplier": actual_multiplier,
        "streak": current_streak,
        "reason": reason,
    })

    _emit_progression_side_effects(
        division=division,
        skill_name=skill_name,
        xp_granted=xp_actual,
        multiplier=actual_multiplier,
        rank_up=rank_up,
        old_div_rank=old_div_rank,
        new_div_rank=new_div_rank,
        old_tier=old_tier,
        new_tier=new_tier,
        base_xp_bonus=base_xp_bonus,
        streak_milestone=streak_milestone,
        streak_mult=streak_mult,
        current_streak=current_streak,
        new_achievements=new_achievements,
        prestige_event=prestige_event,
        stats=stats,
        packet_status="success",
        packet_escalate=False,
        packet_ctx=packet_ctx,
    )

    return {
        "division": division,
        "skill": skill_name,
        "xp_granted": xp_actual,
        "multiplier": actual_multiplier,
        "division_xp": earned_div_xp,
        "division_rank": new_div_rank,
        "rank_up": rank_up,
        "rank_up_msg": f"{old_div_rank} -> {new_div_rank}" if rank_up else "",
        "base_xp_bonus": base_xp_bonus,
        "base_level_up": base_level_up,
        "crossed_tiers": crossed_tiers,
        "new_achievements": new_achievements,
        "reason": reason,
    }


def grant_base_xp(amount: int, reason: str = "") -> dict:
    stats = _load_stats()
    old_level = stats.get("level", 1)
    old_rank = stats.get("rank", "")

    stats["base_xp"] = stats.get("base_xp", 0) + amount
    stats["total_xp_earned"] = stats.get("total_xp_earned", 0) + amount
    stats["total_rewards_from_ruler"] = stats.get("total_rewards_from_ruler", 0) + 1
    stats.setdefault("achievements", [])

    manual_achievement = None
    if "rulers_blessing" not in stats["achievements"]:
        stats["achievements"].append("rulers_blessing")
        manual_achievement = "rulers_blessing"

    _refresh_base_progress(stats)
    new_level = stats["level"]
    new_rank = stats["rank"]

    new_achievements = _check_achievements(stats)
    if manual_achievement:
        new_achievements = [manual_achievement, *[a for a in new_achievements if a != manual_achievement]]

    rank_up = new_rank != old_rank
    level_up = new_level > old_level
    _save_stats(stats)

    chronicle.log_ruler_reward(amount, reason)
    realm_events.emit(
        "xp_grant",
        source="ruler",
        amount=amount,
        reason=reason,
        level=new_level,
        rank=new_rank,
        rank_up=rank_up,
        old_rank=old_rank,
        base_xp=stats["base_xp"],
        xp_to_next_level=stats["xp_to_next_level"],
    )
    realm_story.record_event(
        "xp_grant",
        level=new_level,
        rank=new_rank,
        amount=amount,
        reason=reason,
    )
    _append_xp_history({
        "event": "ruler_bestow",
        "amount": amount,
        "reason": reason,
        "level": new_level,
        "rank": new_rank,
    })

    if rank_up:
        realm_events.emit(
            "rank_up",
            division="base",
            old_rank=old_rank,
            new_rank=new_rank,
            level=new_level,
            rank=new_rank,
            source="ruler",
        )

    for achievement_id in new_achievements:
        ach_data = _achievement_data(achievement_id)
        chronicle.log_achievement(achievement_id, ach_data)
        realm_events.emit(
            "achievement_unlock",
            achievement=achievement_id,
            division="base",
        )

    log.info("Base XP granted: +%d (now %d) | Lvl %d | %s", amount, stats["base_xp"], new_level, new_rank)

    return {
        "xp_granted": amount,
        "reason": reason,
        "base_xp": stats["base_xp"],
        "xp_into_level": stats["xp_into_level"],
        "xp_for_next_level": stats["xp_for_next_level"],
        "xp_to_next_level": stats["xp_to_next_level"],
        "level": new_level,
        "rank": new_rank,
        "level_up": level_up,
        "rank_up": rank_up,
        "rank_up_msg": f"{old_rank} -> {new_rank}" if rank_up else "",
        "new_achievements": new_achievements,
    }


def force_prestige() -> dict:
    stats = _load_stats()
    core_divisions = list(DIVISIONS.keys())
    not_ready = [d for d in core_divisions if stats.get("divisions", {}).get(d, {}).get("xp", 0) < 500]
    if not_ready:
        raise ValueError(f"Not eligible - divisions below 500 XP: {', '.join(not_ready)}")

    for div_key in core_divisions:
        stats["divisions"][div_key]["xp"] = 0
        stats["divisions"][div_key]["rank"] = DIVISIONS[div_key]["ranks"][0]

    stats["prestige"] = stats.get("prestige", 0) + 1
    stats["prestige_multiplier"] = round((1.0 + stats["prestige"] * 0.05) * 1000) / 1000
    _save_stats(stats)

    if _aq:
        try:
            _aq.push_prestige(stats["prestige"], stats["prestige_multiplier"])
        except Exception as e:
            log.warning("anim_queue push failed (non-fatal): %s", e)

    chronicle.log_prestige(stats["prestige"], stats["prestige_multiplier"])
    realm_events.emit(
        "prestige",
        prestige=stats["prestige"],
        multiplier=stats["prestige_multiplier"],
        auto=False,
    )
    realm_story.record_event(
        "prestige",
        prestige=stats["prestige"],
        multiplier=stats["prestige_multiplier"],
    )
    _append_xp_history({
        "event": "prestige",
        "prestige": stats["prestige"],
        "multiplier": stats["prestige_multiplier"],
        "auto": False,
    })

    return {
        "ok": True,
        "prestige": stats["prestige"],
        "prestige_multiplier": stats["prestige_multiplier"],
        "message": f"Prestige {stats['prestige']} achieved - permanent x{stats['prestige_multiplier']} XP multiplier",
    }


def current_stats() -> dict:
    return _load_stats()
