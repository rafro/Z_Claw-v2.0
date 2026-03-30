"""
scene-assemble skill — Wires Production's delivered assets into game scenes.
Reads level-design specs and production asset-deliver/catalog packets.
Generates scene files that reference actual asset paths.
For Godot: creates .tscn scene files. For Pygame: creates resource loader code.
Tier 1 (LLM for scene composition, Tier 0 for asset matching).
"""

import json
import logging
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_CODER_14B, MODEL_CODER_7B, STATE_DIR
from runtime.ollama_client import chat, is_available
from runtime import packet

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"
LEVELS_DIR = GAMEDEV_DIR / "levels"


def _pick_model() -> str | None:
    """Return the best available Coder model, or None."""
    if is_available(MODEL_CODER_14B, host=OLLAMA_HOST):
        return MODEL_CODER_14B
    if is_available(MODEL_CODER_7B, host=OLLAMA_HOST):
        return MODEL_CODER_7B
    return None


def _load_level_spec(level_name: str) -> dict:
    """Load a level-design spec from state/gamedev/levels/."""
    LEVELS_DIR.mkdir(parents=True, exist_ok=True)
    safe = level_name.lower().replace(" ", "-").replace("/", "-")[:60]

    # Try exact match first, then pattern match
    for candidate in (
        LEVELS_DIR / f"{safe}.json",
        LEVELS_DIR / f"{safe}-level.json",
        LEVELS_DIR / f"level-{safe}.json",
    ):
        if candidate.exists():
            try:
                with open(candidate, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log.warning("Failed to load level spec %s: %s", candidate, e)

    # If no specific level found, check for any level spec
    level_files = sorted(LEVELS_DIR.glob("*.json"))
    if level_files:
        try:
            with open(level_files[0], encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    return {}


def _scan_local_assets() -> list[dict]:
    """Scan state/gamedev/project/assets/ for locally available assets."""
    assets_dir = PROJECT_DIR / "assets"
    if not assets_dir.exists():
        return []

    found = []
    for p in assets_dir.rglob("*"):
        if p.is_file() and not p.name.startswith("."):
            found.append({
                "name": p.stem,
                "path": str(p.relative_to(PROJECT_DIR)),
                "type": _classify_asset(p.suffix),
                "extension": p.suffix,
            })
    return found


def _classify_asset(extension: str) -> str:
    """Classify an asset type by file extension."""
    ext = extension.lower()
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".svg"):
        return "sprite"
    elif ext in (".wav", ".ogg", ".mp3", ".flac"):
        return "audio"
    elif ext in (".tscn", ".scn"):
        return "scene"
    elif ext in (".tres", ".res"):
        return "resource"
    elif ext in (".ttf", ".otf"):
        return "font"
    elif ext in (".glb", ".gltf", ".obj", ".fbx"):
        return "model3d"
    elif ext in (".json", ".cfg", ".ini"):
        return "data"
    return "unknown"


def _extract_catalog_assets(catalog_pkt: dict | None, deliver_pkt: dict | None) -> list[dict]:
    """Extract asset info from production catalog and delivery packets."""
    assets = []

    if catalog_pkt:
        items = catalog_pkt.get("metrics", {}).get("assets", [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str):
                    assets.append({"name": item, "status": "cataloged", "source": "production"})
                elif isinstance(item, dict):
                    assets.append({
                        "name": item.get("name", "unknown"),
                        "type": item.get("type", ""),
                        "path": item.get("path", ""),
                        "status": "cataloged",
                        "source": "production",
                    })

    if deliver_pkt:
        deliveries = deliver_pkt.get("metrics", {}).get("delivered", [])
        if isinstance(deliveries, list):
            for item in deliveries:
                if isinstance(item, str):
                    assets.append({"name": item, "status": "delivered", "source": "production"})
                elif isinstance(item, dict):
                    assets.append({
                        "name": item.get("name", "unknown"),
                        "type": item.get("type", ""),
                        "path": item.get("path", ""),
                        "status": "delivered",
                        "source": "production",
                    })

    return assets


def _generate_godot_scene(level_name: str, level_spec: dict, all_assets: list[dict], model: str) -> dict:
    """Use LLM to generate a Godot .tscn scene file."""
    scenes_dir = PROJECT_DIR / "godot" / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    # Build asset inventory for the LLM
    asset_summary = []
    for a in all_assets:
        path = a.get("path", a.get("name", "unknown"))
        atype = a.get("type", "unknown")
        asset_summary.append(f"  - {a['name']} ({atype}): {path}")
    asset_text = "\n".join(asset_summary) if asset_summary else "  (no assets available)"

    # Build level context
    level_text = ""
    if level_spec:
        level_text = json.dumps(level_spec, indent=2)[:3000]
    else:
        level_text = f"Level: {level_name} (no detailed spec available — generate a sensible default layout)"

    system_prompt = (
        "You are a Godot 4 scene assembler. Generate a valid .tscn file for a game level.\n"
        "Follow Godot 4 .tscn format exactly:\n"
        "- Start with [gd_scene ...] header\n"
        "- Define [ext_resource ...] entries for external assets\n"
        "- Define [sub_resource ...] for inline resources\n"
        "- Define [node ...] hierarchy with proper parent references\n"
        "- Use Node2D as root for 2D games, Node3D for 3D\n"
        "- Include proper Sprite2D, CollisionShape2D, Area2D, CharacterBody2D nodes as needed\n"
        "- Reference assets by their res:// paths\n"
        "Only output the .tscn file contents, no explanations."
    )

    user_prompt = (
        f"Generate a Godot 4 scene for level: {level_name}\n\n"
        f"Level spec:\n{level_text}\n\n"
        f"Available assets:\n{asset_text}\n\n"
        "Wire the available assets into the scene. For missing assets, use placeholder "
        "comments. Create a logical node hierarchy for the level."
    )

    try:
        response = chat(model, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.15, max_tokens=2048, task_type="scene-assemble")
    except Exception as e:
        log.error("scene-assemble LLM call failed: %s", e)
        return {"error": str(e)}

    # Strip markdown fences
    scene_content = response.strip()
    if scene_content.startswith("```"):
        lines = scene_content.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        scene_content = "\n".join(lines)

    # Save .tscn file
    safe_name = level_name.lower().replace(" ", "_").replace("-", "_")[:60]
    scene_file = scenes_dir / f"{safe_name}.tscn"
    with open(scene_file, "w", encoding="utf-8") as f:
        f.write(scene_content)

    # Count nodes
    node_count = scene_content.count("[node")

    # Count which assets were referenced vs missing
    referenced = 0
    missing = 0
    for a in all_assets:
        name = a.get("name", "")
        if name and name.lower() in scene_content.lower():
            referenced += 1
        else:
            missing += 1

    return {
        "scene_file": str(scene_file.relative_to(PROJECT_DIR)),
        "scene_content": scene_content,
        "nodes_created": node_count,
        "assets_referenced": referenced,
        "assets_missing": missing,
    }


def _generate_pygame_scene(level_name: str, level_spec: dict, all_assets: list[dict], model: str) -> dict:
    """Use LLM to generate Pygame scene data and loader code."""
    scenes_dir = PROJECT_DIR / "pygame" / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    # Build asset inventory
    asset_summary = []
    for a in all_assets:
        path = a.get("path", a.get("name", "unknown"))
        atype = a.get("type", "unknown")
        asset_summary.append(f"  - {a['name']} ({atype}): {path}")
    asset_text = "\n".join(asset_summary) if asset_summary else "  (no assets available)"

    level_text = ""
    if level_spec:
        level_text = json.dumps(level_spec, indent=2)[:3000]
    else:
        level_text = f"Level: {level_name} (no detailed spec — generate sensible defaults)"

    system_prompt = (
        "You are a Pygame scene assembler. Generate two outputs:\n"
        "1. A scene_data JSON structure defining the level layout, entities, and asset references.\n"
        "2. A Python scene loader that reads the JSON and creates Pygame sprites/groups.\n\n"
        "Output format — first the JSON between ```json fences, then the Python between ```python fences.\n"
        "The loader should:\n"
        "- Load images with pygame.image.load()\n"
        "- Create sprite subclasses for each entity type\n"
        "- Return a dict with sprite groups and level metadata\n"
        "- Handle missing assets gracefully (use colored rectangles as placeholders)"
    )

    user_prompt = (
        f"Generate scene data and loader for level: {level_name}\n\n"
        f"Level spec:\n{level_text}\n\n"
        f"Available assets:\n{asset_text}"
    )

    try:
        response = chat(model, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.15, max_tokens=2048, task_type="scene-assemble")
    except Exception as e:
        log.error("scene-assemble LLM call failed: %s", e)
        return {"error": str(e)}

    safe_name = level_name.lower().replace(" ", "_").replace("-", "_")[:60]

    # Try to split response into JSON and Python parts
    json_content = ""
    py_content = ""
    parts = response.split("```")
    for i, part in enumerate(parts):
        stripped = part.strip()
        if stripped.startswith("json"):
            json_content = stripped[4:].strip()
        elif stripped.startswith("python"):
            py_content = stripped[6:].strip()

    # If splitting didn't work, treat the whole response as Python
    if not py_content:
        py_content = response.strip()
        if py_content.startswith("```"):
            lines = py_content.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            py_content = "\n".join(lines)

    # Save scene data JSON
    scene_data_file = scenes_dir / f"{safe_name}_data.json"
    if json_content:
        with open(scene_data_file, "w", encoding="utf-8") as f:
            f.write(json_content)

    # Save loader Python
    loader_file = scenes_dir / f"{safe_name}_loader.py"
    with open(loader_file, "w", encoding="utf-8") as f:
        f.write(py_content)

    # Count referenced assets
    combined = json_content + py_content
    referenced = 0
    missing = 0
    for a in all_assets:
        name = a.get("name", "")
        if name and name.lower() in combined.lower():
            referenced += 1
        else:
            missing += 1

    scene_file_str = str(loader_file.relative_to(PROJECT_DIR))
    nodes_created = py_content.count("class ") + py_content.count("pygame.sprite")

    return {
        "scene_file": scene_file_str,
        "scene_content": py_content,
        "nodes_created": nodes_created,
        "assets_referenced": referenced,
        "assets_missing": missing,
    }


def run(**kwargs) -> dict:
    """
    Wire Production's delivered assets into game scenes.

    kwargs:
        level_name (str):  Name of the level to assemble (e.g. "tutorial", "level_1").
        target (str):      Target engine — "godot" or "pygame". Default "godot".
    """
    (GAMEDEV_DIR / "project").mkdir(parents=True, exist_ok=True)

    level_name = kwargs.get("level_name", "")
    target = kwargs.get("target", "godot")

    if not level_name:
        return {
            "status": "partial",
            "summary": "No level_name provided for scene assembly.",
            "metrics": {
                "assets_referenced": 0,
                "assets_missing": 0,
                "scene_file": "",
                "nodes_created": 0,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Gather all asset sources
    catalog_pkt = packet.read_fresh("production", "asset-catalog", 1440)     # 24h
    deliver_pkt = packet.read_fresh("production", "asset-deliver", 720)      # 12h

    production_assets = _extract_catalog_assets(catalog_pkt, deliver_pkt)
    local_assets = _scan_local_assets()
    all_assets = production_assets + local_assets

    # Load level spec
    level_spec = _load_level_spec(level_name)

    # Check for LLM availability
    model = _pick_model()
    if not model:
        # Tier 0 fallback: report what we know without generating a scene
        return {
            "status": "degraded",
            "summary": (
                f"Scene assembly for '{level_name}' ({target}) — no Coder model available. "
                f"Found {len(all_assets)} asset(s), level spec {'loaded' if level_spec else 'not found'}."
            ),
            "metrics": {
                "assets_referenced": 0,
                "assets_missing": len(all_assets),
                "scene_file": "",
                "nodes_created": 0,
                "total_assets_available": len(all_assets),
                "has_level_spec": bool(level_spec),
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Generate scene based on target
    if target == "godot":
        result = _generate_godot_scene(level_name, level_spec, all_assets, model)
    elif target == "pygame":
        result = _generate_pygame_scene(level_name, level_spec, all_assets, model)
    else:
        return {
            "status": "partial",
            "summary": f"Unsupported target '{target}' for scene assembly. Use 'godot' or 'pygame'.",
            "metrics": {"assets_referenced": 0, "assets_missing": 0, "scene_file": "", "nodes_created": 0},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Handle LLM call failure
    if "error" in result:
        return {
            "status": "failed",
            "summary": f"Scene assembly LLM call failed: {result['error']}",
            "metrics": {
                "assets_referenced": 0,
                "assets_missing": len(all_assets),
                "scene_file": "",
                "nodes_created": 0,
                "model_available": True,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Build summary
    summary = (
        f"Assembled {target} scene for '{level_name}': "
        f"{result['nodes_created']} node(s), "
        f"{result['assets_referenced']} asset(s) wired, "
        f"{result['assets_missing']} missing → {result['scene_file']}"
    )

    # Escalate if too many assets are missing
    missing_ratio = (
        result["assets_missing"] / max(len(all_assets), 1)
        if all_assets else 0
    )
    escalate = missing_ratio > 0.5 and result["assets_missing"] > 3
    escalation_reason = (
        f"{result['assets_missing']}/{len(all_assets)} assets missing from scene '{level_name}'"
        if escalate else ""
    )

    return {
        "status": "success",
        "summary": summary,
        "metrics": {
            "assets_referenced": result["assets_referenced"],
            "assets_missing": result["assets_missing"],
            "scene_file": result["scene_file"],
            "nodes_created": result["nodes_created"],
            "total_assets_available": len(all_assets),
            "production_assets": len(production_assets),
            "local_assets": len(local_assets),
            "has_level_spec": bool(level_spec),
            "target": target,
            "model_used": model,
            "model_available": True,
        },
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }
