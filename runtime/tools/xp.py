"""
Realm Keeper XP and rank math — pure Python, no LLM.
Sole writer of jclaw-stats.json. J_Claw and division orchestrators read-only.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)

STATS_FILE = STATE_DIR / "jclaw-stats.json"

# ── XP table per skill completion ─────────────────────────────────────────────

SKILL_XP = {
    # ── Opportunity ───────────────────────────────────────────────────────────
    "job-intake":           ("opportunity",    10),
    "hard-filter":          ("opportunity",     5),
    "funding-finder":       ("opportunity",     5),
    # ── Trading ───────────────────────────────────────────────────────────────
    "trading-report":       ("trading",        15),
    "market-scan":          ("trading",         5),
    "backtester":           ("trading",         5),
    # ── Dev Automation ────────────────────────────────────────────────────────
    "repo-monitor":         ("dev_automation", 10),
    "refactor-scan":        ("dev_automation",  5),
    "doc-update":           ("dev_automation",  5),
    "security-scan":        ("dev_automation",  5),
    "debug-agent":          ("dev_automation",  8),
    "artifact-manager":     ("dev_automation",  3),
    "dev-digest":           ("dev_automation",  5),
    # ── Personal ──────────────────────────────────────────────────────────────
    "health-logger":        ("personal",       15),
    "perf-correlation":     ("personal",       10),
    "burnout-monitor":      ("personal",        5),
    "personal-digest":      ("personal",        5),
    # ── OP-Sec ────────────────────────────────────────────────────────────────
    "device-posture":       ("op_sec",         10),
    "breach-check":         ("op_sec",         10),
    "threat-surface":       ("op_sec",          8),
    "cred-audit":           ("op_sec",          8),
    "privacy-scan":         ("op_sec",          5),
    "opsec-digest":         ("op_sec",          5),
    "mobile-audit-review":  ("op_sec",          5),
    # daily-briefing grants no XP (synthesis only)
}

# ── Base rank table ───────────────────────────────────────────────────────────

BASE_RANKS = [
    (50,  "The Eternal Orchestrator"),
    (35,  "Grand Sovereign"),
    (20,  "Warlord of Automation"),
    (10,  "Commander of the Realm"),
    (5,   "Keeper of Systems"),
    (1,   "Apprentice of the Realm"),
]

DIVISION_RANKS = {
    "opportunity": [
        (500, "Sovereign Headhunter"),
        (301, "Grand Headhunter"),
        (151, "Grand Hunter"),
        (51,  "Opportunity Adept"),
        (0,   "Hunter"),
    ],
    "trading": [
        (500, "Oracle of Markets"),
        (301, "Trading Master"),
        (151, "Market Expert"),
        (51,  "Market Adept"),
        (0,   "Market Scout"),
    ],
    "dev_automation": [
        (500, "Architect of the Realm"),
        (301, "Code Architect"),
        (151, "Code Expert"),
        (51,  "Code Adept"),
        (0,   "Code Ward"),
    ],
    "personal": [
        (500, "Eternal Guardian"),
        (301, "Guardian of the Flame"),
        (151, "Wellness Expert"),
        (51,  "Wellness Adept"),
        (0,   "Keeper"),
    ],
    "op_sec": [
        (500, "Sovereign Sentinel"),
        (301, "Grand Sentinel"),
        (151, "Security Expert"),
        (51,  "Security Adept"),
        (0,   "Watchman"),
    ],
}


def _base_rank(level: int) -> str:
    for threshold, title in BASE_RANKS:
        if level >= threshold:
            return title
    return "Apprentice of the Realm"


def _division_rank(division: str, xp: int) -> str:
    table = DIVISION_RANKS.get(division, [])
    for threshold, title in table:
        if xp >= threshold:
            return title
    return "—"


def _level_from_xp(base_xp: int) -> int:
    """Simple level formula: 1 level per 100 base XP, floor at 1."""
    return max(1, base_xp // 100 + 1)


# ── State read/write ──────────────────────────────────────────────────────────

def _load_stats() -> dict:
    if not STATS_FILE.exists():
        return _empty_stats()
    try:
        with open(STATS_FILE, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        log.error("Failed to load jclaw-stats.json: %s", e)
        return _empty_stats()


def _empty_stats() -> dict:
    return {
        "base_xp":  0,
        "level":    1,
        "rank":     "Apprentice of the Realm",
        "divisions": {
            "opportunity":    {"xp": 0, "rank": "Hunter"},
            "trading":        {"xp": 0, "rank": "Market Scout"},
            "dev_automation": {"xp": 0, "rank": "Code Ward"},
            "personal":       {"xp": 0, "rank": "Keeper"},
            "op_sec":         {"xp": 0, "rank": "Watchman"},
        },
        "achievements": [],
        "last_updated": None,
    }


def _save_stats(stats: dict) -> None:
    stats["last_updated"] = datetime.now(timezone.utc).isoformat()
    STATS_FILE.parent.mkdir(exist_ok=True)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def grant_skill_xp(skill_name: str) -> dict:
    """
    Grant division XP for a completed skill.
    Returns progression_packet dict.
    Division XP NEVER converts to base XP automatically.
    """
    if skill_name not in SKILL_XP:
        return {"skill": skill_name, "xp_granted": 0, "rank_up": False}

    division, xp_amount = SKILL_XP[skill_name]
    stats = _load_stats()

    div_stats = stats["divisions"].setdefault(division, {"xp": 0, "rank": ""})
    old_div_rank = div_stats.get("rank", "")
    div_stats["xp"] = div_stats.get("xp", 0) + xp_amount
    new_div_rank = _division_rank(division, div_stats["xp"])
    div_stats["rank"] = new_div_rank
    stats["divisions"][division] = div_stats

    rank_up = new_div_rank != old_div_rank and old_div_rank != ""
    _save_stats(stats)

    log.info("XP granted: %s +%d div XP (%s → %s)",
             skill_name, xp_amount, old_div_rank, new_div_rank)

    return {
        "skill":        skill_name,
        "division":     division,
        "xp_granted":   xp_amount,
        "division_xp":  div_stats["xp"],
        "division_rank": new_div_rank,
        "rank_up":      rank_up,
        "rank_up_msg":  f"{old_div_rank} → {new_div_rank}" if rank_up else "",
    }


def grant_base_xp(amount: int, reason: str = "") -> dict:
    """
    Grant base XP. ONLY called by /reward from Matthew.
    This is what drives level and base rank.
    """
    stats = _load_stats()
    old_level = stats.get("level", 1)
    old_rank  = stats.get("rank", "")

    stats["base_xp"] = stats.get("base_xp", 0) + amount
    new_level = _level_from_xp(stats["base_xp"])
    new_rank  = _base_rank(new_level)
    stats["level"] = new_level
    stats["rank"]  = new_rank

    rank_up = new_rank != old_rank
    level_up = new_level > old_level
    _save_stats(stats)

    log.info("Base XP granted: +%d (now %d) | Lvl %d | %s",
             amount, stats["base_xp"], new_level, new_rank)

    return {
        "xp_granted":  amount,
        "reason":      reason,
        "base_xp":     stats["base_xp"],
        "level":       new_level,
        "rank":        new_rank,
        "level_up":    level_up,
        "rank_up":     rank_up,
        "rank_up_msg": f"{old_rank} → {new_rank}" if rank_up else "",
    }


def current_stats() -> dict:
    return _load_stats()
