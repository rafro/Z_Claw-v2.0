"""
mechanic-prototype skill — Takes a mechanic concept and generates pseudocode/logic spec.
Uses LLM with a code-focused system prompt to produce structured mechanic definitions.
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
PROTOTYPES_DIR = GAMEDEV_DIR / "prototypes"


def _load_gdd_mechanics() -> list:
    """Load mechanics list from GDD if available."""
    gdd_file = GAMEDEV_DIR / "gdd.json"
    if gdd_file.exists():
        try:
            with open(gdd_file, encoding="utf-8") as f:
                gdd = json.load(f)
            return gdd.get("mechanics", [])
        except Exception:
            pass
    return []


def _save_prototype(name: str, prototype: dict) -> None:
    """Save a mechanic prototype to state/gamedev/prototypes/."""
    PROTOTYPES_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = name.lower().replace(" ", "-").replace("/", "-")[:60]
    out_file = PROTOTYPES_DIR / f"{safe_name}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(prototype, f, indent=2, ensure_ascii=False)


def run(**kwargs) -> dict:
    """
    Generate pseudocode/logic spec for a game mechanic.

    kwargs:
        mechanic (str):  Name or description of the mechanic to prototype.
        context (str):   Additional game context (genre, related mechanics).
        constraints (str): Design constraints (performance budget, complexity limit).
    """
    GAMEDEV_DIR.mkdir(parents=True, exist_ok=True)

    mechanic = kwargs.get("mechanic", "")
    context = kwargs.get("context", "")
    constraints = kwargs.get("constraints", "")

    if not mechanic:
        # Try to pick the first un-prototyped mechanic from GDD
        existing_mechs = _load_gdd_mechanics()
        existing_protos = set()
        if PROTOTYPES_DIR.exists():
            existing_protos = {p.stem for p in PROTOTYPES_DIR.glob("*.json")}

        for m in existing_mechs:
            m_name = m if isinstance(m, str) else m.get("name", "")
            safe = m_name.lower().replace(" ", "-").replace("/", "-")[:60]
            if safe and safe not in existing_protos:
                mechanic = m_name
                break

    if not mechanic:
        return {
            "status": "partial",
            "summary": "No mechanic specified and no un-prototyped mechanics in GDD.",
            "metrics": {"model_available": is_available(MODEL, host=OLLAMA_HOST)},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    if not is_available(MODEL, host=OLLAMA_HOST):
        return {
            "status": "degraded",
            "summary": f"Mechanic prototype for '{mechanic}' — LLM unavailable.",
            "metrics": {"mechanic": mechanic, "model_available": False},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    system_prompt = (
        "You are a game systems engineer. Given a mechanic concept, produce a "
        "structured prototype specification with:\n"
        "1. OVERVIEW: 1-2 sentence description of what the mechanic does.\n"
        "2. INPUTS: What triggers or feeds this mechanic (player actions, timers, events).\n"
        "3. STATE: Data structures needed (variables, tables, queues).\n"
        "4. LOGIC: Pseudocode for the core update loop / event handler.\n"
        "5. OUTPUTS: What this mechanic produces (score changes, visual feedback, state transitions).\n"
        "6. EDGE CASES: At least 2 edge cases and how to handle them.\n"
        "7. TUNING KNOBS: Parameters that designers should be able to adjust.\n"
        "Be precise. Use pseudocode, not prose."
    )

    user_parts = [f"Mechanic to prototype: {mechanic}"]
    if context:
        user_parts.append(f"Game context: {context}")
    if constraints:
        user_parts.append(f"Constraints: {constraints}")

    user_prompt = "\n".join(user_parts)

    try:
        response = chat(MODEL, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.3, max_tokens=1000, task_type="mechanic-prototype")
    except Exception as e:
        log.error("mechanic-prototype LLM call failed: %s", e)
        return {
            "status": "failed",
            "summary": f"Mechanic prototype LLM call failed: {e}",
            "metrics": {"mechanic": mechanic, "model_available": True},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Persist the prototype
    prototype_data = {
        "mechanic": mechanic,
        "context": context,
        "constraints": constraints,
        "specification": response,
    }
    _save_prototype(mechanic, prototype_data)

    summary_text = (
        f"Prototyped '{mechanic}': "
        + (response[:200].rsplit(" ", 1)[0] + "..." if len(response) > 200 else response)
    )

    return {
        "status": "success",
        "summary": summary_text,
        "specification": response,
        "metrics": {
            "mechanic": mechanic,
            "output_length": len(response),
            "model_available": True,
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
