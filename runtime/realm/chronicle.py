"""
Realm Chronicle — persistent world event log.

Appends structured events to state/realm-chronicle.jsonl.
Readable via /mobile/api/realm/chronicle.

Event classes:
  micro — rank-ups, streaks, achievements, skill first-runs
  major — new divisions, tier-4 legendary, full-order activation, prestige

All narrative text is generated from templates in config.py.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import STATE_DIR
from runtime.realm.config import (
    DIVISIONS, CHRONICLE_TEMPLATES, DIV_XP_THRESHOLDS, RANK_UP_BASE_XP
)

log = logging.getLogger(__name__)

CHRONICLE_FILE = STATE_DIR / "realm-chronicle.jsonl"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fmt(template: str, **kwargs) -> str:
    try:
        return template.format(**kwargs)
    except KeyError:
        return template


def _append(entry: dict) -> None:
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat())
    try:
        with open(CHRONICLE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        log.debug("Chronicle: %s — %s", entry.get("event_class"), entry.get("title"))
    except Exception as e:
        log.error("Chronicle write failed: %s", e)


def _div(division_key: str) -> dict:
    return DIVISIONS.get(division_key, {})


# ── Public API ────────────────────────────────────────────────────────────────

def log_rank_up(division_key: str, new_xp: int, old_tier: int, new_tier: int) -> None:
    """Record a division tier advancement."""
    div        = _div(division_key)
    commander  = div.get("commander", division_key)
    order      = div.get("order", division_key)
    ranks      = div.get("ranks", [])
    new_rank   = ranks[new_tier] if new_tier < len(ranks) else "Unknown"
    base_xp    = RANK_UP_BASE_XP.get(new_tier, 0)
    tpl_key    = f"rank_up_tier_{new_tier}"
    tpl        = CHRONICLE_TEMPLATES.get(tpl_key, {})
    event_class = "major" if new_tier == 4 else "micro"

    _append({
        "event_class": event_class,
        "category":    "rank_up",
        "division":    division_key,
        "commander":   commander,
        "order":       order,
        "tier":        new_tier,
        "title":       _fmt(tpl.get("title", f"{order} advances"), commander=commander, order=order, rank=new_rank),
        "lore":        _fmt(tpl.get("lore",  ""), commander=commander, order=order, rank=new_rank, xp=new_xp),
        "operational": f"{order} crossed {DIV_XP_THRESHOLDS[new_tier]} XP — {new_rank} achieved",
        "impact":      _fmt(tpl.get("impact", ""), commander=commander, order=order, rank=new_rank) +
                       (f" +{base_xp} base XP to J_Claw." if base_xp else ""),
    })


def log_streak_milestone(division_key: str, streak_days: int) -> None:
    """Record a 7-day streak milestone."""
    div       = _div(division_key)
    commander = div.get("commander", division_key)
    order     = div.get("order", division_key)
    tpl_key   = f"streak_{streak_days}" if streak_days in (7, 14, 21) else "streak_7"
    tpl       = CHRONICLE_TEMPLATES.get(tpl_key, {})
    mult      = min(1.5, 1.0 + (streak_days // 7) * 0.1)

    _append({
        "event_class": "micro",
        "category":    "streak_milestone",
        "division":    division_key,
        "commander":   commander,
        "order":       order,
        "streak_days": streak_days,
        "title":       _fmt(tpl.get("title", f"{order} — {streak_days}-Day Streak"), commander=commander, order=order),
        "lore":        _fmt(tpl.get("lore",  ""), commander=commander, order=order),
        "operational": f"{order} streak: {streak_days} consecutive days",
        "impact":      _fmt(tpl.get("impact", ""), commander=commander, order=order, multiplier=f"×{mult:.1f}"),
    })


def log_achievement(achievement_id: str, achievement_data: dict) -> None:
    """Record an achievement unlock."""
    _append({
        "event_class": "micro",
        "category":    "achievement",
        "achievement": achievement_id,
        "title":       f"Achievement Unlocked: {achievement_data.get('name', achievement_id)}",
        "lore":        achievement_data.get("chronicle_lore", achievement_data.get("desc", "")),
        "operational": f"Achievement condition met: {achievement_data.get('desc', '')}",
        "impact":      achievement_data.get("name", ""),
    })


def log_prestige(prestige_level: int, multiplier: float) -> None:
    """Record a prestige ascension."""
    tpl = CHRONICLE_TEMPLATES.get("prestige", {})
    _append({
        "event_class": "major",
        "category":    "prestige",
        "prestige":    prestige_level,
        "multiplier":  multiplier,
        "title":       _fmt(tpl.get("title", f"Prestige {prestige_level}"), prestige=prestige_level, multiplier=multiplier),
        "lore":        _fmt(tpl.get("lore", ""), prestige=prestige_level, multiplier=multiplier),
        "operational": f"Prestige {prestige_level} achieved — permanent ×{multiplier:.2f} XP multiplier active",
        "impact":      _fmt(tpl.get("impact", ""), prestige=prestige_level, multiplier=multiplier),
    })


def log_ruler_reward(amount: int, reason: str) -> None:
    """Record a sovereign reward from Matthew."""
    tpl = CHRONICLE_TEMPLATES.get("ruler_reward", {})
    _append({
        "event_class": "micro",
        "category":    "ruler_reward",
        "amount":      amount,
        "reason":      reason,
        "title":       _fmt(tpl.get("title", f"Sovereign's Decree — {amount} XP"), amount=amount),
        "lore":        _fmt(tpl.get("lore",  ""), amount=amount, reason=reason or "excellence in the realm"),
        "operational": f"Matthew granted {amount} base XP. Reason: {reason}",
        "impact":      _fmt(tpl.get("impact", ""), amount=amount),
    })


def get_recent(limit: int = 25) -> list:
    """Return the most recent chronicle entries (newest first)."""
    if not CHRONICLE_FILE.exists():
        return []
    try:
        lines = CHRONICLE_FILE.read_text(encoding="utf-8").splitlines()
        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        return list(reversed(entries))[:limit]
    except Exception as e:
        log.error("Chronicle read failed: %s", e)
        return []


def migrate_from_history(xp_history_path: Path, stats_path: Path) -> int:
    """
    One-time retroactive migration from xp-history.jsonl.
    Reconstructs rank-up events, ruler rewards, and prestige events
    that already occurred. Skips if chronicle already has entries.
    Returns count of entries written.
    """
    if CHRONICLE_FILE.exists() and CHRONICLE_FILE.stat().st_size > 0:
        log.info("Chronicle already populated — skipping migration")
        return 0

    written = 0

    # Reconstruct division rank-ups from XP history
    try:
        if not xp_history_path.exists():
            return 0

        # Track cumulative XP per division to find when thresholds were crossed
        div_xp: dict[str, int] = {}
        div_tier: dict[str, int] = {}

        lines = xp_history_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue

            ts = e.get("ts", "")

            if e.get("event") == "skill_complete":
                div_key = e.get("div", "")
                xp_gain = e.get("xp", 0)
                if not div_key or not xp_gain:
                    continue

                old_xp   = div_xp.get(div_key, 0)
                new_xp   = old_xp + xp_gain
                old_tier = div_tier.get(div_key, 0)

                # Find new tier
                new_tier = 0
                for i in range(len(DIV_XP_THRESHOLDS) - 1, -1, -1):
                    if new_xp >= DIV_XP_THRESHOLDS[i]:
                        new_tier = i
                        break

                if new_tier > old_tier:
                    div       = _div(div_key)
                    commander = div.get("commander", div_key)
                    order     = div.get("order", div_key)
                    ranks     = div.get("ranks", [])
                    new_rank  = ranks[new_tier] if new_tier < len(ranks) else "Unknown"
                    base_xp   = RANK_UP_BASE_XP.get(new_tier, 0)
                    tpl_key   = f"rank_up_tier_{new_tier}"
                    tpl       = CHRONICLE_TEMPLATES.get(tpl_key, {})
                    event_class = "major" if new_tier == 4 else "micro"

                    entry = {
                        "ts":          ts,
                        "event_class": event_class,
                        "category":    "rank_up",
                        "division":    div_key,
                        "commander":   commander,
                        "order":       order,
                        "tier":        new_tier,
                        "title":       _fmt(tpl.get("title", f"{order} advances"), commander=commander, order=order, rank=new_rank),
                        "lore":        _fmt(tpl.get("lore",  ""), commander=commander, order=order, rank=new_rank, xp=new_xp),
                        "operational": f"{order} crossed {DIV_XP_THRESHOLDS[new_tier]} XP — {new_rank} achieved",
                        "impact":      _fmt(tpl.get("impact", ""), commander=commander, order=order, rank=new_rank) +
                                       (f" +{base_xp} base XP to J_Claw." if base_xp else ""),
                        "retroactive": True,
                    }
                    _append(entry)
                    written += 1
                    div_tier[div_key] = new_tier

                div_xp[div_key] = new_xp

            elif e.get("event") == "ruler_bestow":
                amount = e.get("amount", 0)
                reason = e.get("reason", "")
                tpl    = CHRONICLE_TEMPLATES.get("ruler_reward", {})
                entry  = {
                    "ts":          ts,
                    "event_class": "micro",
                    "category":    "ruler_reward",
                    "amount":      amount,
                    "reason":      reason,
                    "title":       _fmt(tpl.get("title", f"Sovereign's Decree — {amount} XP"), amount=amount),
                    "lore":        _fmt(tpl.get("lore", ""), amount=amount, reason=reason or "excellence in the realm"),
                    "operational": f"Matthew granted {amount} base XP",
                    "impact":      _fmt(tpl.get("impact", ""), amount=amount),
                    "retroactive": True,
                }
                _append(entry)
                written += 1

            elif e.get("event") in {"prestige", "auto_prestige"}:
                prestige = e.get("prestige", 0)
                multiplier = e.get("multiplier", 1.0)
                tpl = CHRONICLE_TEMPLATES.get("prestige", {})
                entry = {
                    "ts":          ts,
                    "event_class": "major",
                    "category":    "prestige",
                    "prestige":    prestige,
                    "multiplier":  multiplier,
                    "title":       _fmt(tpl.get("title", f"Prestige {prestige}"), prestige=prestige, multiplier=multiplier),
                    "lore":        _fmt(tpl.get("lore", ""), prestige=prestige, multiplier=multiplier),
                    "operational": f"Prestige {prestige} achieved — permanent ×{multiplier:.2f} XP multiplier active",
                    "impact":      _fmt(tpl.get("impact", ""), prestige=prestige, multiplier=multiplier),
                    "retroactive": True,
                    "auto":        e.get("auto", e.get("event") == "auto_prestige"),
                }
                _append(entry)
                written += 1

        log.info("Chronicle migration complete: %d entries written", written)
    except Exception as ex:
        log.error("Chronicle migration failed: %s", ex)

    return written
