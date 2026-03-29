"""
balance-audit skill — Reads game data files (damage tables, economy values,
progression curves) from state/gamedev/ and uses LLM to identify balance issues.
Returns findings with severity ratings.
Tier 1 (7B local).
"""

import json
import logging
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_7B, STATE_DIR
from runtime.ollama_client import chat, chat_json, is_available

log = logging.getLogger(__name__)
MODEL = MODEL_7B
GAMEDEV_DIR = STATE_DIR / "gamedev"

# Files the balance auditor looks for
BALANCE_FILES = {
    "damage_tables": "damage-tables.json",
    "economy": "economy.json",
    "progression": "progression-curves.json",
    "enemies": "enemies.json",
    "items": "items.json",
    "skills_data": "skills-data.json",
}


def _load_balance_data() -> dict[str, dict]:
    """Load all available balance data files from state/gamedev/."""
    data = {}
    for key, filename in BALANCE_FILES.items():
        fpath = GAMEDEV_DIR / filename
        if fpath.exists():
            try:
                with open(fpath, encoding="utf-8") as f:
                    data[key] = json.load(f)
            except Exception as e:
                log.warning("Failed to load %s: %s", filename, e)
    return data


def _basic_checks(data: dict[str, dict]) -> list[dict]:
    """
    Pure Python balance checks — no LLM needed.
    Returns a list of findings: [{"category", "severity", "description"}].
    """
    findings = []

    # Check damage tables for outliers
    damage = data.get("damage_tables", {})
    if isinstance(damage, dict):
        values = []
        for weapon, stats in damage.items():
            if isinstance(stats, dict):
                dmg = stats.get("damage") or stats.get("base_damage")
                if isinstance(dmg, (int, float)):
                    values.append((weapon, dmg))

        if len(values) >= 3:
            avg = sum(v for _, v in values) / len(values)
            for weapon, dmg in values:
                ratio = dmg / avg if avg > 0 else 0
                if ratio > 3.0:
                    findings.append({
                        "category": "damage",
                        "severity": "high",
                        "description": f"'{weapon}' damage ({dmg}) is {ratio:.1f}x the average ({avg:.0f}) — likely overpowered.",
                    })
                elif ratio < 0.25:
                    findings.append({
                        "category": "damage",
                        "severity": "medium",
                        "description": f"'{weapon}' damage ({dmg}) is only {ratio:.1f}x the average ({avg:.0f}) — may be unviable.",
                    })

    # Check economy for inflation risk
    economy = data.get("economy", {})
    if isinstance(economy, dict):
        income_rate = economy.get("income_per_minute") or economy.get("gold_per_minute", 0)
        cheapest_item = economy.get("cheapest_item_cost") or economy.get("min_item_cost", 0)
        if income_rate and cheapest_item and income_rate > 0:
            minutes_to_buy = cheapest_item / income_rate
            if minutes_to_buy < 0.5:
                findings.append({
                    "category": "economy",
                    "severity": "high",
                    "description": f"Cheapest item costs {minutes_to_buy:.1f} min of income — economy may inflate too fast.",
                })

    # Check progression curves for dead zones
    progression = data.get("progression", {})
    if isinstance(progression, dict):
        xp_per_level = progression.get("xp_per_level", [])
        if isinstance(xp_per_level, list) and len(xp_per_level) >= 3:
            for i in range(1, len(xp_per_level)):
                if xp_per_level[i - 1] > 0:
                    jump = xp_per_level[i] / xp_per_level[i - 1]
                    if jump > 5.0:
                        findings.append({
                            "category": "progression",
                            "severity": "medium",
                            "description": f"XP requirement jumps {jump:.1f}x between level {i} and {i+1} — potential dead zone.",
                        })

    return findings


def run(**kwargs) -> dict:
    """
    Audit game balance data and return findings with severity ratings.

    kwargs:
        focus (str): Optional focus area — "damage", "economy", "progression", or "all".
    """
    GAMEDEV_DIR.mkdir(parents=True, exist_ok=True)

    focus = kwargs.get("focus", "all")
    data = _load_balance_data()

    if not data:
        return {
            "status": "partial",
            "summary": "No balance data files found in state/gamedev/. Add damage-tables.json, economy.json, or progression-curves.json.",
            "findings": [],
            "metrics": {
                "files_found": 0,
                "findings_count": 0,
                "model_available": is_available(MODEL, host=OLLAMA_HOST),
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Pure Python checks first
    findings = _basic_checks(data)

    # LLM deep analysis
    if is_available(MODEL, host=OLLAMA_HOST):
        # Truncate data to fit context
        data_summary = {}
        for key, content in data.items():
            serialized = json.dumps(content, indent=None)
            if len(serialized) > 2000:
                serialized = serialized[:2000] + "...(truncated)"
            data_summary[key] = serialized

        system_prompt = (
            "You are a game balance analyst. Examine the provided game data and identify "
            "balance issues. For each issue, state:\n"
            "- Category (damage/economy/progression/other)\n"
            "- Severity (low/medium/high/critical)\n"
            "- Description of the issue and recommended fix\n"
            "Focus on: power curves, economy sinks vs faucets, progression pacing, "
            "dominant strategies, and dead content. Be specific with numbers."
        )

        focus_text = f"Focus area: {focus}\n\n" if focus != "all" else ""
        user_prompt = (
            f"{focus_text}Game balance data:\n"
            + "\n".join(f"=== {k} ===\n{v}" for k, v in data_summary.items())
        )

        try:
            response = chat(MODEL, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ], temperature=0.2, max_tokens=600, task_type="balance-audit")

            # Parse LLM findings into the findings list
            findings.append({
                "category": "llm-analysis",
                "severity": "info",
                "description": response,
            })
        except Exception as e:
            log.warning("balance-audit LLM analysis failed: %s", e)

    # Determine escalation
    high_count = sum(1 for f in findings if f.get("severity") in ("high", "critical"))
    escalate = high_count >= 3
    escalation_reason = f"{high_count} high/critical balance issues detected" if escalate else ""

    summary_parts = [f"Balance audit: {len(data)} data file(s) analyzed, {len(findings)} finding(s)."]
    if high_count:
        summary_parts.append(f"{high_count} high/critical issue(s) flagged.")

    return {
        "status": "success",
        "summary": " ".join(summary_parts),
        "findings": findings,
        "metrics": {
            "files_found": len(data),
            "findings_count": len(findings),
            "high_severity_count": high_count,
            "focus": focus,
            "model_available": is_available(MODEL, host=OLLAMA_HOST),
        },
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }
