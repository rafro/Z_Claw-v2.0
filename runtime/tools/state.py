"""
State file read/write helpers — pure I/O, no LLM.
All state files live in OpenClaw-Orchestrator/state/.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)


def _load(path: Path, default: Any) -> Any:
    if not path.exists():
        log.warning("State file missing, using default: %s", path)
        return default
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with open(path, encoding=enc) as f:
                return json.load(f)
        except (UnicodeDecodeError, UnicodeError):
            continue
        except json.JSONDecodeError as e:
            log.error("JSON parse error in %s: %s", path, e)
            return default
    log.error("Could not decode %s with any known encoding", path)
    return default


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── jobs-seen ─────────────────────────────────────────────────────────────────

def load_jobs_seen() -> dict:
    return _load(
        STATE_DIR / "jobs-seen.json",
        {"jobs": [], "last_run": None, "total_seen": 0}
    )


def save_jobs_seen(state: dict) -> None:
    _save(STATE_DIR / "jobs-seen.json", state)


def get_seen_ids(state: dict) -> set:
    return {j["id"] for j in state.get("jobs", []) if j and j.get("id")}


def append_new_jobs(state: dict, new_jobs: list) -> dict:
    state["jobs"].extend(new_jobs)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["total_seen"] = len(state["jobs"])
    return state


# ── applications ──────────────────────────────────────────────────────────────

def load_applications() -> dict:
    return _load(
        STATE_DIR / "applications.json",
        {"pipeline": [], "stats": {"pending_review": 0, "applied": 0, "rejected": 0}}
    )


def save_applications(state: dict) -> None:
    _save(STATE_DIR / "applications.json", state)


def add_to_pipeline(state: dict, jobs: list) -> dict:
    existing_ids = {j["id"] for j in state["pipeline"]}
    new = [j for j in jobs if j["id"] not in existing_ids]
    state["pipeline"].extend(new)
    state["stats"]["pending_review"] = sum(
        1 for j in state["pipeline"] if j.get("tier") in ("A", "B", "C")
    )
    return state


# ── health-log ────────────────────────────────────────────────────────────────

def load_health_log() -> dict:
    return _load(
        STATE_DIR / "health-log.json",
        {"entries": [], "last_logged": None}
    )


def save_health_log(state: dict) -> None:
    _save(STATE_DIR / "health-log.json", state)


def append_health_entry(state: dict, entry: dict) -> dict:
    state["entries"].append(entry)
    state["last_logged"] = entry["logged_at"]
    return state


def recent_health_entries(state: dict, days: int = 14) -> list:
    """Return the last N days of health entries."""
    return state["entries"][-days:]


# ── trade-log ─────────────────────────────────────────────────────────────────

def load_trade_log() -> dict:
    return _load(
        STATE_DIR / "trade-log.json",
        {"trades": [], "last_updated": None}
    )


# ── jclaw-stats (read-only — Realm Keeper owns writes) ───────────────────────

def read_jclaw_stats() -> dict:
    return _load(
        STATE_DIR / "jclaw-stats.json",
        {"level": 1, "base_xp": 0, "rank": "Apprentice of the Realm",
         "divisions": {}}
    )


# ── intake-temp (handoff between job-intake tool and hard-filter skill) ───────

def save_intake_temp(jobs: list) -> None:
    _save(STATE_DIR / "intake-temp.json", jobs)


def load_intake_temp() -> list:
    return _load(STATE_DIR / "intake-temp.json", [])
