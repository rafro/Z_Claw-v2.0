"""
code-generate skill — Generates game source code from tech specs and GDD.
Uses Coder model (14B preferred, 7B fallback) for higher code quality.
Supports multiple targets: godot (GDScript), pygame (Python), generic.
Saves generated files to state/gamedev/project/{target}/.
Tier 2 preferred (Coder 14B).
"""

import json
import logging
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_CODER_14B, MODEL_CODER_7B, STATE_DIR
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"
GDD_FILE = GAMEDEV_DIR / "gdd.json"
SPECS_DIR = GAMEDEV_DIR / "tech-specs"
MANIFEST_FILE = PROJECT_DIR / "manifest.json"

TARGET_EXTENSIONS = {
    "godot": ".gd",
    "pygame": ".py",
    "generic": ".py",
}


def _pick_model() -> str | None:
    """Return the best available Coder model, or None."""
    if is_available(MODEL_CODER_14B, host=OLLAMA_HOST):
        return MODEL_CODER_14B
    if is_available(MODEL_CODER_7B, host=OLLAMA_HOST):
        return MODEL_CODER_7B
    return None


def _load_gdd() -> dict:
    """Load the current GDD for project context."""
    if GDD_FILE.exists():
        try:
            with open(GDD_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load gdd.json: %s", e)
    return {}


def _load_tech_spec(system_name: str) -> str:
    """Load a matching tech spec if one exists."""
    safe = system_name.lower().replace(" ", "-").replace("/", "-")[:60]
    spec_file = SPECS_DIR / f"{safe}.json"
    if spec_file.exists():
        try:
            with open(spec_file, encoding="utf-8") as f:
                spec = json.load(f)
            return spec.get("specification", "")
        except Exception as e:
            log.warning("Failed to load tech spec '%s': %s", safe, e)
    return ""


def _load_manifest() -> dict:
    """Load the existing project manifest."""
    if MANIFEST_FILE.exists():
        try:
            with open(MANIFEST_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"files": [], "targets": {}}


def _save_manifest(manifest: dict) -> None:
    """Persist the project manifest."""
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def _build_system_prompt(target: str) -> str:
    """Build the LLM system prompt based on target engine."""
    base = (
        "You are an expert game programmer. Generate clean, well-commented, "
        "production-quality game code. Follow best practices for the target engine. "
        "Only output the code — no explanations before or after."
    )
    if target == "godot":
        return base + (
            "\n\nTarget: Godot 4 / GDScript. "
            "Use proper class structure with class_name declarations. "
            "Use typed variables (var name: Type). "
            "Define signals with the 'signal' keyword. "
            "Use @export for inspector-visible properties. "
            "Use @onready for node references. "
            "Follow Godot naming conventions: snake_case for functions/variables, "
            "PascalCase for classes and signals. "
            "Include _ready(), _process(delta), and _physics_process(delta) as needed."
        )
    elif target == "pygame":
        return base + (
            "\n\nTarget: Pygame / Python. "
            "Include a proper game loop with event handling, update, and draw phases. "
            "Use pygame.sprite.Sprite subclasses for game entities. "
            "Use pygame.sprite.Group for sprite management. "
            "Handle screen setup, clock, and FPS control. "
            "Use type hints throughout. "
            "Include if __name__ == '__main__' guard for runnable scripts."
        )
    else:  # generic
        return base + (
            "\n\nTarget: Generic Python. "
            "Write clean, modular Python with type hints. "
            "Use dataclasses or plain classes as appropriate. "
            "Include docstrings for all public interfaces."
        )


def run(**kwargs) -> dict:
    """
    Generate game source code from tech specs and GDD.

    kwargs:
        system_name (str):  Name of the system to generate (e.g. "player_controller", "inventory").
        target (str):       Target engine — "godot", "pygame", or "generic". Default "godot".
        description (str):  Additional description or requirements for the code.
    """
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)

    system_name = kwargs.get("system_name", "")
    target = kwargs.get("target", "godot")
    description = kwargs.get("description", "")

    if target not in TARGET_EXTENSIONS:
        target = "godot"

    if not system_name:
        return {
            "status": "partial",
            "summary": "No system_name provided for code generation.",
            "metrics": {"model_available": _pick_model() is not None},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    model = _pick_model()
    if not model:
        return {
            "status": "degraded",
            "summary": f"Code generation for '{system_name}' ({target}) — no Coder model available.",
            "metrics": {
                "system_name": system_name,
                "target": target,
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Gather context
    gdd = _load_gdd()
    tech_spec = _load_tech_spec(system_name)

    # Build user prompt
    user_parts = [f"Generate code for the '{system_name}' system."]

    if gdd:
        context_bits = []
        if gdd.get("title"):
            context_bits.append(f"Project: {gdd['title']}")
        if gdd.get("genre"):
            context_bits.append(f"Genre: {gdd['genre']}")
        if gdd.get("core_loop"):
            context_bits.append(f"Core loop: {gdd['core_loop']}")
        if gdd.get("mechanics"):
            mechs = [m if isinstance(m, str) else m.get("name", "") for m in gdd["mechanics"]]
            context_bits.append(f"Mechanics: {', '.join(mechs)}")
        if context_bits:
            user_parts.append("Game context:\n" + "\n".join(context_bits))

    if tech_spec:
        # Truncate very long specs to stay within token budget
        spec_text = tech_spec[:2000]
        if len(tech_spec) > 2000:
            spec_text += "\n... (spec truncated)"
        user_parts.append(f"Technical specification:\n{spec_text}")

    if description:
        user_parts.append(f"Additional requirements: {description}")

    # Load lessons from previous refine-loop runs
    lessons_file = GAMEDEV_DIR / "lessons-learned.json"
    if lessons_file.exists():
        try:
            with open(lessons_file, encoding="utf-8") as f:
                all_lessons = json.load(f)
            # Filter to lessons relevant to this system
            relevant = [l for l in all_lessons if system_name and system_name.lower() in l.get("system", "").lower()]
            if relevant:
                lessons_text = "\n".join(f"- {l['issue']}" for l in relevant[-5:])  # Last 5 relevant
                user_parts.append(f"\nPast issues to avoid (from previous builds):\n{lessons_text}")
        except Exception:
            pass  # Lessons file is optional

    user_prompt = "\n\n".join(user_parts)
    system_prompt = _build_system_prompt(target)

    try:
        response = chat(model, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.15, max_tokens=2048, task_type="code-generate")
    except Exception as e:
        log.error("code-generate LLM call failed: %s", e)
        return {
            "status": "failed",
            "summary": f"Code generation LLM call failed: {e}",
            "metrics": {"system_name": system_name, "target": target, "model_available": True},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Save the generated code
    ext = TARGET_EXTENSIONS[target]
    safe_name = system_name.lower().replace(" ", "_").replace("-", "_").replace("/", "_")[:60]
    target_dir = PROJECT_DIR / target
    target_dir.mkdir(parents=True, exist_ok=True)
    out_file = target_dir / f"{safe_name}{ext}"

    # Strip markdown code fences if the LLM wrapped the output
    code = response.strip()
    if code.startswith("```"):
        lines = code.split("\n")
        # Remove first line (```lang) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        code = "\n".join(lines)

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(code)

    lines_of_code = len(code.splitlines())

    # Update manifest
    manifest = _load_manifest()
    file_entry = {
        "path": str(out_file.relative_to(PROJECT_DIR)),
        "system_name": system_name,
        "target": target,
        "lines": lines_of_code,
    }
    # Replace existing entry for same system/target or append
    manifest["files"] = [
        fe for fe in manifest.get("files", [])
        if not (fe.get("system_name") == system_name and fe.get("target") == target)
    ]
    manifest["files"].append(file_entry)
    manifest.setdefault("targets", {})[target] = True
    _save_manifest(manifest)

    summary_text = (
        f"Generated {lines_of_code}-line {target} code for '{system_name}' → {out_file.name}"
    )

    return {
        "status": "success",
        "summary": summary_text,
        "generated_code": code,
        "metrics": {
            "files_generated": 1,
            "target": target,
            "system_name": system_name,
            "lines_of_code": lines_of_code,
            "model_used": model,
            "model_available": True,
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
