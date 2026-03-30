"""
build-pipeline skill — Packages game projects for distribution.
For Python/Pygame: creates distributable package with dependencies.
For Godot: generates export preset configuration and build commands.
Tier 0 (deterministic — no LLM needed).
"""

import json
import logging
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"
BUILDS_DIR = GAMEDEV_DIR / "builds"
MANIFEST_FILE = PROJECT_DIR / "manifest.json"


def _load_manifest() -> dict:
    """Load the project manifest."""
    if MANIFEST_FILE.exists():
        try:
            with open(MANIFEST_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"files": [], "targets": {}}


def _count_source_files(target_dir: Path) -> int:
    """Count source files in a target directory."""
    count = 0
    for ext in ("*.gd", "*.py", "*.json", "*.tscn", "*.tres"):
        count += len(list(target_dir.rglob(ext)))
    return count


def _dir_size_kb(path: Path) -> int:
    """Calculate total size of a directory in KB."""
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total // 1024


def _detect_targets() -> list[str]:
    """Detect which target directories exist."""
    targets = []
    if not PROJECT_DIR.exists():
        return targets
    for candidate in ("godot", "pygame", "generic"):
        if (PROJECT_DIR / candidate).exists():
            targets.append(candidate)
    return targets


def _action_status(target: str) -> dict:
    """Scan project and report build readiness."""
    targets = _detect_targets() if not target else [target]
    manifest = _load_manifest()

    if not targets:
        return {
            "status": "partial",
            "summary": "No project targets found. Generate code first.",
            "metrics": {
                "source_files": 0,
                "build_size_kb": 0,
                "target": "",
                "action": "status",
                "build_ready": False,
            },
            "build_path": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    total_source = 0
    target_status = {}
    for t in targets:
        target_dir = PROJECT_DIR / t
        count = _count_source_files(target_dir)
        total_source += count
        size_kb = _dir_size_kb(target_dir)

        # Check for existing builds
        build_dir = BUILDS_DIR / t
        has_build = build_dir.exists() and any(build_dir.iterdir()) if build_dir.exists() else False

        target_status[t] = {
            "source_files": count,
            "size_kb": size_kb,
            "has_build": has_build,
        }

    summary_parts = [f"Build status: {total_source} source file(s) across {len(targets)} target(s)."]
    for t, info in target_status.items():
        built = "built" if info["has_build"] else "not built"
        summary_parts.append(f"  {t}: {info['source_files']} files, {info['size_kb']}KB ({built})")

    return {
        "status": "success",
        "summary": " ".join(summary_parts),
        "metrics": {
            "source_files": total_source,
            "build_size_kb": sum(i["size_kb"] for i in target_status.values()),
            "target": ", ".join(targets),
            "action": "status",
            "build_ready": total_source > 0,
            "target_details": target_status,
            "manifest_files": len(manifest.get("files", [])),
        },
        "build_path": "",
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }


def _package_python(target: str) -> dict:
    """Package a Python-based project (pygame or generic)."""
    target_dir = PROJECT_DIR / target
    if not target_dir.exists():
        return {
            "status": "partial",
            "summary": f"No {target} project directory found.",
            "metrics": {"source_files": 0, "build_size_kb": 0, "target": target, "action": "package"},
            "build_path": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    source_files = list(target_dir.rglob("*.py"))
    if not source_files:
        return {
            "status": "partial",
            "summary": f"No Python files found in {target}/.",
            "metrics": {"source_files": 0, "build_size_kb": 0, "target": target, "action": "package"},
            "build_path": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    build_dir = BUILDS_DIR / target
    build_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    zip_name = f"{target}_build_{timestamp}.zip"
    zip_path = build_dir / zip_name

    # Generate requirements.txt if it doesn't exist
    requirements_file = target_dir / "requirements.txt"
    if not requirements_file.exists():
        reqs = []
        if target == "pygame":
            reqs.append("pygame>=2.5.0")
        # Scan imports for common packages
        for src in source_files:
            try:
                content = src.read_text(encoding="utf-8")
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("import ") or stripped.startswith("from "):
                        pkg = stripped.split()[1].split(".")[0]
                        if pkg in ("numpy", "PIL", "pillow", "requests"):
                            if pkg == "PIL":
                                pkg = "Pillow"
                            if pkg not in reqs and pkg.lower() not in [r.lower().split(">=")[0] for r in reqs]:
                                reqs.append(pkg)
            except Exception:
                pass

        with open(requirements_file, "w", encoding="utf-8") as f:
            f.write("\n".join(reqs) + "\n")

    # Generate run script
    run_script = target_dir / "run.sh"
    if not run_script.exists():
        # Find the most likely entry point
        entry = "main.py"
        for candidate in ("main.py", "game.py", "app.py"):
            if (target_dir / candidate).exists():
                entry = candidate
                break
        with open(run_script, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n")
            f.write("# Auto-generated run script\n")
            f.write("cd \"$(dirname \"$0\")\"\n")
            f.write("pip install -r requirements.txt 2>/dev/null\n")
            f.write(f"python {entry}\n")
        run_script.chmod(0o755)

    # Create zip package
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in target_dir.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                arcname = f.relative_to(target_dir)
                zf.write(f, arcname)

    build_size_kb = zip_path.stat().st_size // 1024

    return {
        "status": "success",
        "summary": f"Packaged {target}: {len(source_files)} files → {zip_name} ({build_size_kb}KB)",
        "metrics": {
            "source_files": len(source_files),
            "build_size_kb": build_size_kb,
            "target": target,
            "action": "package",
        },
        "build_path": str(zip_path),
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }


def _package_godot(target: str) -> dict:
    """Generate Godot export configuration and build script."""
    target_dir = PROJECT_DIR / target
    if not target_dir.exists():
        return {
            "status": "partial",
            "summary": "No godot project directory found.",
            "metrics": {"source_files": 0, "build_size_kb": 0, "target": target, "action": "package"},
            "build_path": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    source_files = list(target_dir.rglob("*.gd")) + list(target_dir.rglob("*.tscn"))
    if not source_files:
        return {
            "status": "partial",
            "summary": "No Godot files (.gd/.tscn) found.",
            "metrics": {"source_files": 0, "build_size_kb": 0, "target": target, "action": "package"},
            "build_path": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    build_dir = BUILDS_DIR / target
    build_dir.mkdir(parents=True, exist_ok=True)

    # Generate export_presets.cfg
    export_presets = target_dir / "export_presets.cfg"
    if not export_presets.exists():
        preset_content = (
            '[preset.0]\n\n'
            'name="Linux"\n'
            'platform="Linux"\n'
            'runnable=true\n'
            'dedicated_server=false\n'
            'custom_features=""\n'
            'export_filter="all_resources"\n'
            'include_filter=""\n'
            'exclude_filter=""\n'
            'export_path=""\n'
            'script_export_mode=2\n\n'
            '[preset.1]\n\n'
            'name="Windows Desktop"\n'
            'platform="Windows Desktop"\n'
            'runnable=true\n'
            'dedicated_server=false\n'
            'custom_features=""\n'
            'export_filter="all_resources"\n'
            'include_filter=""\n'
            'exclude_filter=""\n'
            'export_path=""\n'
            'script_export_mode=2\n'
        )
        with open(export_presets, "w", encoding="utf-8") as f:
            f.write(preset_content)

    # Generate build command script
    build_script = build_dir / "build_godot.sh"
    with open(build_script, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write("# Godot export build script\n")
        f.write(f"# Source: {target_dir}\n")
        f.write(f"# Generated: {datetime.now(timezone.utc).isoformat()}\n\n")
        f.write(f'PROJECT_DIR="{target_dir}"\n')
        f.write(f'BUILD_DIR="{build_dir}"\n\n')
        f.write("# Linux export\n")
        f.write('godot --headless --export-release "Linux" "$BUILD_DIR/game_linux.x86_64" --path "$PROJECT_DIR"\n\n')
        f.write("# Windows export\n")
        f.write('godot --headless --export-release "Windows Desktop" "$BUILD_DIR/game_windows.exe" --path "$PROJECT_DIR"\n')
    build_script.chmod(0o755)

    build_size_kb = _dir_size_kb(target_dir)

    return {
        "status": "success",
        "summary": (
            f"Godot build configured: {len(source_files)} source files. "
            f"Export presets and build script generated at {build_dir.name}/."
        ),
        "metrics": {
            "source_files": len(source_files),
            "build_size_kb": build_size_kb,
            "target": target,
            "action": "package",
        },
        "build_path": str(build_dir),
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }


def _action_package(target: str) -> dict:
    """Package the project for the given target."""
    if not target:
        targets = _detect_targets()
        if not targets:
            return {
                "status": "partial",
                "summary": "No project targets found. Generate code first.",
                "metrics": {"source_files": 0, "build_size_kb": 0, "target": "", "action": "package"},
                "build_path": "",
                "escalate": False,
                "escalation_reason": "",
                "action_items": [],
            }
        target = targets[0]

    if target in ("pygame", "generic"):
        return _package_python(target)
    elif target == "godot":
        return _package_godot(target)
    else:
        return {
            "status": "partial",
            "summary": f"Unknown target '{target}'. Supported: godot, pygame, generic.",
            "metrics": {"target": target, "action": "package"},
            "build_path": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }


def _action_clean(target: str) -> dict:
    """Remove old build artifacts."""
    targets = _detect_targets() if not target else [target]
    removed = 0

    for t in targets:
        build_dir = BUILDS_DIR / t
        if build_dir.exists():
            for item in build_dir.iterdir():
                if item.is_file():
                    item.unlink()
                    removed += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    removed += 1

    # Also clean generated helper files in target dirs
    for t in targets:
        target_dir = PROJECT_DIR / t
        for gen_file in ("run.sh", "export_presets.cfg"):
            gen_path = target_dir / gen_file
            if gen_path.exists():
                gen_path.unlink()
                removed += 1

    return {
        "status": "success",
        "summary": f"Cleaned {removed} build artifact(s) for {', '.join(targets) if targets else 'all targets'}.",
        "metrics": {
            "source_files": 0,
            "build_size_kb": 0,
            "target": ", ".join(targets),
            "action": "clean",
            "artifacts_removed": removed,
        },
        "build_path": "",
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }


def run(**kwargs) -> dict:
    """
    Package game projects for distribution. Pure Python — no LLM needed.

    kwargs:
        target (str):  Target engine — "godot", "pygame", or "generic".
        action (str):  "status" | "package" | "clean". Default "status".
    """
    (GAMEDEV_DIR / "project").mkdir(parents=True, exist_ok=True)

    target = kwargs.get("target", "")
    action = kwargs.get("action", "status")

    if action == "package":
        return _action_package(target)
    elif action == "clean":
        return _action_clean(target)
    else:
        return _action_status(target)
