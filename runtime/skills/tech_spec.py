"""
tech-spec skill — Generates technical design documents for game systems.
Takes a system name/description as input and uses LLM to produce
architecture specs. Returns structured spec.
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
SPECS_DIR = GAMEDEV_DIR / "tech-specs"


def _load_gdd_context() -> str:
    """Load relevant GDD context for tech spec generation."""
    gdd_file = GAMEDEV_DIR / "gdd.json"
    if not gdd_file.exists():
        return ""
    try:
        with open(gdd_file, encoding="utf-8") as f:
            gdd = json.load(f)
        parts = []
        if gdd.get("genre"):
            parts.append(f"Genre: {gdd['genre']}")
        if gdd.get("target_platform"):
            parts.append(f"Platform: {gdd['target_platform']}")
        if gdd.get("mechanics"):
            mechs = [m if isinstance(m, str) else m.get("name", "") for m in gdd["mechanics"]]
            parts.append(f"Mechanics: {', '.join(mechs)}")
        return "\n".join(parts)
    except Exception:
        return ""


def _load_existing_specs() -> list[str]:
    """List existing tech spec names."""
    if not SPECS_DIR.exists():
        return []
    return [p.stem for p in SPECS_DIR.glob("*.json")]


def _save_spec(name: str, spec_data: dict) -> None:
    """Persist a tech spec."""
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = name.lower().replace(" ", "-").replace("/", "-")[:60]
    out_file = SPECS_DIR / f"{safe_name}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(spec_data, f, indent=2, ensure_ascii=False)


def run(**kwargs) -> dict:
    """
    Generate a technical design document for a game system.

    kwargs:
        system_name (str):   Name of the system to spec (e.g. "inventory", "combat", "save-system").
        description (str):   Description of what the system should do.
        requirements (str):  Performance or design requirements.
        language (str):      Target language/engine (e.g. "Godot/GDScript", "Unity/C#", "Rust").
    """
    GAMEDEV_DIR.mkdir(parents=True, exist_ok=True)

    system_name = kwargs.get("system_name", "")
    description = kwargs.get("description", "")
    requirements = kwargs.get("requirements", "")
    language = kwargs.get("language", "")

    if not system_name and not description:
        return {
            "status": "partial",
            "summary": "No system_name or description provided for tech spec.",
            "metrics": {"model_available": is_available(MODEL, host=OLLAMA_HOST)},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    gdd_context = _load_gdd_context()
    existing_specs = _load_existing_specs()

    if not is_available(MODEL, host=OLLAMA_HOST):
        return {
            "status": "degraded",
            "summary": f"Tech spec for '{system_name or description[:40]}' — LLM unavailable.",
            "metrics": {
                "system_name": system_name,
                "existing_specs": len(existing_specs),
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    system_prompt = (
        "You are a game systems architect. Produce a technical design document "
        "for the specified game system. Include:\n"
        "1. OVERVIEW: What the system does and why it exists.\n"
        "2. ARCHITECTURE: High-level component diagram (text-based).\n"
        "3. DATA MODEL: Key data structures, relationships, and storage.\n"
        "4. API / INTERFACE: Public methods/signals other systems use.\n"
        "5. IMPLEMENTATION PLAN: Step-by-step build order with estimates.\n"
        "6. PERFORMANCE BUDGET: Memory, CPU, network constraints.\n"
        "7. TESTING STRATEGY: Key test cases and edge conditions.\n"
        "8. DEPENDENCIES: What other systems this integrates with.\n"
        "Be concrete — include type signatures, data schemas, and pseudocode."
    )

    user_parts = []
    if gdd_context:
        user_parts.append(f"Game context:\n{gdd_context}")
    if system_name:
        user_parts.append(f"System: {system_name}")
    if description:
        user_parts.append(f"Description: {description}")
    if requirements:
        user_parts.append(f"Requirements: {requirements}")
    if language:
        user_parts.append(f"Target language/engine: {language}")
    if existing_specs:
        user_parts.append(f"Existing specs (for cross-references): {', '.join(existing_specs[:10])}")

    user_prompt = "\n".join(user_parts)

    try:
        response = chat(MODEL, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.2, max_tokens=1200, task_type="tech-spec")
    except Exception as e:
        log.error("tech-spec LLM call failed: %s", e)
        return {
            "status": "failed",
            "summary": f"Tech spec LLM call failed: {e}",
            "metrics": {"system_name": system_name, "model_available": True},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Persist
    save_name = system_name or description[:40].replace(" ", "-")
    spec_data = {
        "system_name": system_name,
        "description": description,
        "requirements": requirements,
        "language": language,
        "specification": response,
    }
    _save_spec(save_name, spec_data)

    summary_text = (
        f"Tech spec for '{save_name}': "
        + (response[:250].rsplit(" ", 1)[0] + "..." if len(response) > 250 else response)
    )

    return {
        "status": "success",
        "summary": summary_text,
        "specification": response,
        "metrics": {
            "system_name": save_name,
            "existing_specs": len(existing_specs),
            "output_length": len(response),
            "model_available": True,
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
