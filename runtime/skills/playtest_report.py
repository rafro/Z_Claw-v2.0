"""
playtest-report skill — Analyzes playtest data from state/gamedev/playtest-data.jsonl.
Uses LLM to identify patterns, flag issues, and suggest improvements.
Returns a structured report.
Tier 1 (7B local).
"""

import json
import logging
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_7B, STATE_DIR
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
MODEL = MODEL_7B
GAMEDEV_DIR = STATE_DIR / "gamedev"
PLAYTEST_FILE = GAMEDEV_DIR / "playtest-data.jsonl"
REPORTS_DIR = GAMEDEV_DIR / "playtest-reports"


def _load_playtest_data(max_sessions: int = 50) -> list[dict]:
    """Load playtest session data from JSONL file."""
    if not PLAYTEST_FILE.exists():
        return []
    sessions = []
    try:
        with open(PLAYTEST_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        sessions.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        log.warning("Failed to read playtest data: %s", e)
    # Return most recent sessions
    return sessions[-max_sessions:]


def _compute_basic_stats(sessions: list[dict]) -> dict:
    """Compute basic playtest statistics from session data."""
    if not sessions:
        return {}

    total = len(sessions)
    durations = [s.get("duration_seconds", 0) for s in sessions if s.get("duration_seconds")]
    deaths = [s.get("deaths", 0) for s in sessions if "deaths" in s]
    completions = [s for s in sessions if s.get("completed")]
    quit_points = {}

    for s in sessions:
        qp = s.get("quit_point") or s.get("quit_level")
        if qp and not s.get("completed"):
            quit_points[str(qp)] = quit_points.get(str(qp), 0) + 1

    stats = {
        "total_sessions": total,
        "completion_rate": round(len(completions) / total * 100, 1) if total else 0,
    }
    if durations:
        stats["avg_duration_seconds"] = round(sum(durations) / len(durations), 1)
        stats["min_duration_seconds"] = min(durations)
        stats["max_duration_seconds"] = max(durations)
    if deaths:
        stats["avg_deaths"] = round(sum(deaths) / len(deaths), 1)
        stats["max_deaths"] = max(deaths)
    if quit_points:
        stats["top_quit_points"] = dict(
            sorted(quit_points.items(), key=lambda x: x[1], reverse=True)[:5]
        )

    return stats


def _detect_issues(sessions: list[dict], stats: dict) -> list[str]:
    """Pure Python issue detection from playtest data."""
    issues = []

    completion_rate = stats.get("completion_rate", 100)
    if completion_rate < 30:
        issues.append(f"Very low completion rate ({completion_rate}%) — game may be too hard or tedious.")
    elif completion_rate < 60:
        issues.append(f"Moderate completion rate ({completion_rate}%) — check difficulty spikes.")

    avg_deaths = stats.get("avg_deaths")
    if avg_deaths is not None and avg_deaths > 20:
        issues.append(f"High average deaths ({avg_deaths}) — may frustrate casual players.")

    quit_points = stats.get("top_quit_points", {})
    if quit_points:
        worst = max(quit_points.items(), key=lambda x: x[1])
        if worst[1] >= 3:
            issues.append(f"Quit cluster at '{worst[0]}' ({worst[1]} players quit here) — investigate difficulty or UX.")

    avg_duration = stats.get("avg_duration_seconds")
    if avg_duration is not None and avg_duration < 60:
        issues.append(f"Very short avg session ({avg_duration:.0f}s) — players may not be engaged.")

    return issues


def _save_report(report: dict) -> None:
    """Persist the playtest report."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out_file = REPORTS_DIR / f"report-{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def run(**kwargs) -> dict:
    """
    Analyze playtest data and generate a structured report.

    kwargs:
        max_sessions (int): Max number of recent sessions to analyze (default 50).
    """
    GAMEDEV_DIR.mkdir(parents=True, exist_ok=True)

    max_sessions = kwargs.get("max_sessions", 50)
    sessions = _load_playtest_data(max_sessions)

    if not sessions:
        return {
            "status": "partial",
            "summary": "No playtest data found in state/gamedev/playtest-data.jsonl.",
            "critical_issues": [],
            "metrics": {
                "sessions_analyzed": 0,
                "model_available": is_available(MODEL, host=OLLAMA_HOST),
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    stats = _compute_basic_stats(sessions)
    issues = _detect_issues(sessions, stats)

    # LLM deep analysis
    llm_analysis = ""
    if is_available(MODEL, host=OLLAMA_HOST):
        # Build a summary of sessions for the LLM (avoid sending all raw data)
        session_summaries = []
        for s in sessions[-20:]:  # last 20 for context window
            parts = []
            if s.get("player_id"):
                parts.append(f"player={s['player_id']}")
            if s.get("duration_seconds"):
                parts.append(f"duration={s['duration_seconds']}s")
            if "deaths" in s:
                parts.append(f"deaths={s['deaths']}")
            if s.get("completed"):
                parts.append("completed")
            elif s.get("quit_point"):
                parts.append(f"quit_at={s['quit_point']}")
            if s.get("feedback"):
                parts.append(f"feedback=\"{s['feedback'][:100]}\"")
            session_summaries.append(", ".join(parts))

        system_prompt = (
            "You are a playtest analyst for a game development team. "
            "Analyze the playtest session data and statistics. Identify:\n"
            "1. DIFFICULTY ISSUES: Where are players struggling or breezing through?\n"
            "2. ENGAGEMENT DROPS: When do players stop playing and why?\n"
            "3. FUN MOMENTS: What patterns correlate with longer sessions?\n"
            "4. RECOMMENDATIONS: 3-5 specific, actionable changes to improve the experience.\n"
            "Be data-driven — cite specific numbers from the stats."
        )

        user_prompt = (
            f"Playtest statistics:\n{json.dumps(stats, indent=2)}\n\n"
            f"Detected issues:\n" + "\n".join(f"- {i}" for i in issues) + "\n\n"
            f"Recent sessions:\n" + "\n".join(f"  {s}" for s in session_summaries)
        )

        try:
            llm_analysis = chat(MODEL, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ], temperature=0.2, max_tokens=600, task_type="playtest-report")
        except Exception as e:
            log.warning("playtest-report LLM analysis failed: %s", e)

    # Build summary
    summary_parts = [
        f"Playtest report: {stats.get('total_sessions', 0)} sessions analyzed.",
        f"Completion rate: {stats.get('completion_rate', 'N/A')}%.",
    ]
    if stats.get("avg_deaths") is not None:
        summary_parts.append(f"Avg deaths: {stats['avg_deaths']}.")
    if issues:
        summary_parts.append(f"{len(issues)} issue(s) detected.")
    if llm_analysis:
        # Add first sentence of LLM analysis
        first_line = llm_analysis.split("\n")[0][:200]
        summary_parts.append(first_line)

    # Critical issues are high-severity for the orchestrator
    critical_issues = [i for i in issues if "Very low" in i or "cluster" in i]

    # Escalate if completion rate is dangerously low
    escalate = stats.get("completion_rate", 100) < 20 and stats.get("total_sessions", 0) >= 10
    escalation_reason = (
        f"Completion rate at {stats.get('completion_rate')}% across {stats['total_sessions']} sessions"
        if escalate else ""
    )

    # Persist report
    report_data = {
        "stats": stats,
        "issues": issues,
        "llm_analysis": llm_analysis,
        "sessions_count": len(sessions),
    }
    _save_report(report_data)

    return {
        "status": "success",
        "summary": " ".join(summary_parts),
        "llm_analysis": llm_analysis,
        "critical_issues": critical_issues,
        "metrics": {
            "sessions_analyzed": stats.get("total_sessions", 0),
            "completion_rate": stats.get("completion_rate"),
            "avg_deaths": stats.get("avg_deaths"),
            "issues_count": len(issues),
            "model_available": is_available(MODEL, host=OLLAMA_HOST),
        },
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }
