"""
game-runner skill — Launches the built game and captures runtime behavior.
For Pygame: subprocess.run([python, main.py]) with timeout + stderr capture.
For Godot: subprocess.run([godot, --headless]) if binary available.
Tier 0 (pure subprocess execution — no LLM).
"""

import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"

# Regex to capture Python tracebacks from stderr
_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):.*?(?=\nTraceback |\n[^\s]|\Z)",
    re.DOTALL,
)

# Common Godot error patterns
_GODOT_ERROR_RE = re.compile(
    r"(ERROR|SCRIPT ERROR|FATAL):?\s*.+",
    re.IGNORECASE,
)

_MAX_OUTPUT_CHARS = 2000


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    """Truncate text to limit, appending an indicator if truncated."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"


def _parse_python_errors(stderr: str) -> list[dict]:
    """Extract structured error information from Python stderr output."""
    errors = []
    for match in _TRACEBACK_RE.finditer(stderr):
        tb_text = match.group(0).strip()
        lines = tb_text.splitlines()

        # The last line of a traceback is typically the exception
        error_line = lines[-1] if lines else tb_text
        error_parts = error_line.split(":", 1)
        error_type = error_parts[0].strip() if error_parts else "UnknownError"
        error_msg = error_parts[1].strip() if len(error_parts) > 1 else error_line

        errors.append({
            "type": error_type,
            "message": error_msg,
            "traceback": tb_text,
        })

    # If no tracebacks found but stderr has content, capture raw error lines
    if not errors and stderr.strip():
        for line in stderr.strip().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(("pygame ", "Warning:", "ALSA", "libpng")):
                errors.append({
                    "type": "RuntimeError",
                    "message": stripped,
                    "traceback": "",
                })
                break  # Only capture the first meaningful error line

    return errors


def _parse_godot_errors(stderr: str) -> list[dict]:
    """Extract structured error information from Godot stderr output."""
    errors = []
    seen = set()

    for match in _GODOT_ERROR_RE.finditer(stderr):
        error_text = match.group(0).strip()
        if error_text in seen:
            continue
        seen.add(error_text)

        parts = error_text.split(":", 1)
        error_type = parts[0].strip() if parts else "GodotError"
        error_msg = parts[1].strip() if len(parts) > 1 else error_text

        errors.append({
            "type": error_type,
            "message": error_msg,
            "traceback": "",
        })

    return errors


def _run_pygame(timeout: int, headless: bool) -> dict:
    """Launch a Pygame project and capture results."""
    project_dir = PROJECT_DIR / "pygame"
    main_file = project_dir / "main.py"

    if not project_dir.exists():
        return {
            "status": "skipped",
            "summary": "Pygame project directory not found at state/gamedev/project/pygame/.",
            "metrics": {
                "exit_code": -1,
                "runtime_seconds": 0.0,
                "stderr_lines": 0,
                "crash_detected": False,
                "target": "pygame",
            },
            "errors": [],
            "stdout": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [{
                "priority": "normal",
                "description": "Generate Pygame project code before running.",
                "requires_matthew": False,
            }],
        }

    if not main_file.exists():
        # Try alternative entry points
        entry_point = None
        for candidate in ("game.py", "app.py", "run.py"):
            if (project_dir / candidate).exists():
                entry_point = project_dir / candidate
                break
        if entry_point is None:
            return {
                "status": "skipped",
                "summary": "No main.py (or game.py/app.py) found in pygame project directory.",
                "metrics": {
                    "exit_code": -1,
                    "runtime_seconds": 0.0,
                    "stderr_lines": 0,
                    "crash_detected": False,
                    "target": "pygame",
                },
                "errors": [],
                "stdout": "",
                "escalate": False,
                "escalation_reason": "",
                "action_items": [{
                    "priority": "normal",
                    "description": "Create main.py entry point in state/gamedev/project/pygame/.",
                    "requires_matthew": False,
                }],
            }
    else:
        entry_point = main_file

    # Build environment: set SDL_VIDEODRIVER=dummy for headless if requested
    env = None
    if headless:
        import os
        env = os.environ.copy()
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"

    start_time = time.monotonic()

    try:
        result = subprocess.run(
            [sys.executable, str(entry_point.name)],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        elapsed = time.monotonic() - start_time
        exit_code = result.returncode
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        timed_out = False

    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - start_time
        exit_code = -1
        stdout = e.stdout if isinstance(e.stdout, str) else (e.stdout.decode("utf-8", errors="replace") if e.stdout else "")
        stderr = e.stderr if isinstance(e.stderr, str) else (e.stderr.decode("utf-8", errors="replace") if e.stderr else "")
        timed_out = True

    except FileNotFoundError:
        return {
            "status": "failed",
            "summary": f"Python interpreter not found: {sys.executable}",
            "metrics": {
                "exit_code": -1,
                "runtime_seconds": 0.0,
                "stderr_lines": 0,
                "crash_detected": False,
                "target": "pygame",
            },
            "errors": [{"type": "FileNotFoundError", "message": f"Interpreter not found: {sys.executable}", "traceback": ""}],
            "stdout": "",
            "escalate": True,
            "escalation_reason": "Python interpreter not found",
            "action_items": [],
        }

    except OSError as e:
        return {
            "status": "failed",
            "summary": f"OS error launching game: {e}",
            "metrics": {
                "exit_code": -1,
                "runtime_seconds": 0.0,
                "stderr_lines": 0,
                "crash_detected": False,
                "target": "pygame",
            },
            "errors": [{"type": "OSError", "message": str(e), "traceback": ""}],
            "stdout": "",
            "escalate": True,
            "escalation_reason": str(e),
            "action_items": [],
        }

    # Parse errors
    errors = _parse_python_errors(stderr)
    crash_detected = exit_code != 0 and not timed_out
    stderr_lines = len(stderr.splitlines()) if stderr else 0

    # Determine status and summary
    if timed_out:
        # A timeout on a game is actually expected — the game ran for the full
        # duration without crashing, which typically means success.
        status = "success"
        summary = (
            f"Game ran for the full {timeout}s timeout with no crash detected. "
            "This usually indicates the game loop is running correctly."
        )
        crash_detected = False
    elif exit_code == 0:
        status = "success"
        if errors:
            summary = (
                f"Game exited cleanly (code 0) after {elapsed:.1f}s, "
                f"but {len(errors)} warning(s) found in stderr."
            )
        else:
            summary = f"Game ran successfully for {elapsed:.1f}s with no crashes."
    else:
        status = "failed"
        if errors:
            first_error = errors[0]
            summary = (
                f"Game crashed (exit code {exit_code}) after {elapsed:.1f}s: "
                f"{first_error['type']}: {first_error['message']}"
            )
        else:
            summary = f"Game exited with code {exit_code} after {elapsed:.1f}s."

    escalate = crash_detected
    escalation_reason = (
        f"Pygame game crashed with exit code {exit_code}"
        if crash_detected else ""
    )

    action_items = []
    if crash_detected and errors:
        action_items.append({
            "priority": "high",
            "description": (
                f"Fix crash: {errors[0]['type']}: {errors[0]['message']}. "
                "See errors list for full traceback."
            ),
            "requires_matthew": False,
        })

    return {
        "status": status,
        "summary": summary,
        "metrics": {
            "exit_code": exit_code,
            "runtime_seconds": round(elapsed, 2),
            "stderr_lines": stderr_lines,
            "crash_detected": crash_detected,
            "target": "pygame",
        },
        "errors": errors,
        "stdout": _truncate(stdout),
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": action_items,
    }


def _run_godot(timeout: int, headless: bool) -> dict:
    """Launch a Godot project and capture results."""
    godot_bin = shutil.which("godot")
    if godot_bin is None:
        return {
            "status": "skipped",
            "summary": "Godot binary not found on PATH. Install Godot or add it to PATH to enable game-runner for Godot targets.",
            "metrics": {
                "exit_code": -1,
                "runtime_seconds": 0.0,
                "stderr_lines": 0,
                "crash_detected": False,
                "target": "godot",
            },
            "errors": [],
            "stdout": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [{
                "priority": "normal",
                "description": "Install Godot and ensure 'godot' is on PATH.",
                "requires_matthew": True,
            }],
        }

    project_dir = PROJECT_DIR / "godot"
    project_file = project_dir / "project.godot"

    if not project_file.exists():
        return {
            "status": "skipped",
            "summary": "No project.godot found at state/gamedev/project/godot/project.godot.",
            "metrics": {
                "exit_code": -1,
                "runtime_seconds": 0.0,
                "stderr_lines": 0,
                "crash_detected": False,
                "target": "godot",
            },
            "errors": [],
            "stdout": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [{
                "priority": "normal",
                "description": "Generate Godot project before running.",
                "requires_matthew": False,
            }],
        }

    cmd = [godot_bin, "--path", str(project_dir), "--quit-after", str(timeout)]
    if headless:
        cmd.insert(1, "--headless")

    start_time = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,  # Extra buffer beyond Godot's own quit-after
        )
        elapsed = time.monotonic() - start_time
        exit_code = result.returncode
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        timed_out = False

    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - start_time
        exit_code = -1
        stdout = e.stdout if isinstance(e.stdout, str) else (e.stdout.decode("utf-8", errors="replace") if e.stdout else "")
        stderr = e.stderr if isinstance(e.stderr, str) else (e.stderr.decode("utf-8", errors="replace") if e.stderr else "")
        timed_out = True

    except OSError as e:
        return {
            "status": "failed",
            "summary": f"OS error launching Godot: {e}",
            "metrics": {
                "exit_code": -1,
                "runtime_seconds": 0.0,
                "stderr_lines": 0,
                "crash_detected": False,
                "target": "godot",
            },
            "errors": [{"type": "OSError", "message": str(e), "traceback": ""}],
            "stdout": "",
            "escalate": True,
            "escalation_reason": str(e),
            "action_items": [],
        }

    # Parse errors
    errors = _parse_godot_errors(stderr)
    stderr_lines = len(stderr.splitlines()) if stderr else 0

    # For Godot, non-zero exit is common even on success (editor quirks).
    # Focus on whether actual SCRIPT ERROR / FATAL errors appeared.
    has_fatal = any(e["type"].upper() in ("FATAL", "SCRIPT ERROR") for e in errors)
    crash_detected = has_fatal

    if timed_out:
        status = "success"
        summary = (
            f"Godot ran for the full {timeout}s timeout with no fatal errors. "
            "Game loop appears stable."
        )
        crash_detected = False
    elif has_fatal:
        status = "failed"
        first_err = next(e for e in errors if e["type"].upper() in ("FATAL", "SCRIPT ERROR"))
        summary = (
            f"Godot reported fatal error after {elapsed:.1f}s: "
            f"{first_err['type']}: {first_err['message']}"
        )
    elif errors:
        status = "success"
        summary = (
            f"Godot ran for {elapsed:.1f}s (exit code {exit_code}). "
            f"{len(errors)} non-fatal warning(s) in stderr."
        )
    else:
        status = "success"
        summary = f"Godot ran successfully for {elapsed:.1f}s with no errors."

    escalate = crash_detected
    escalation_reason = (
        f"Godot game has fatal error: {errors[0]['message']}"
        if crash_detected and errors else ""
    )

    action_items = []
    if crash_detected and errors:
        action_items.append({
            "priority": "high",
            "description": (
                f"Fix Godot error: {errors[0]['type']}: {errors[0]['message']}"
            ),
            "requires_matthew": False,
        })

    return {
        "status": status,
        "summary": summary,
        "metrics": {
            "exit_code": exit_code,
            "runtime_seconds": round(elapsed, 2),
            "stderr_lines": stderr_lines,
            "crash_detected": crash_detected,
            "target": "godot",
        },
        "errors": errors,
        "stdout": _truncate(stdout),
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": action_items,
    }


def run(**kwargs) -> dict:
    """
    Launch the built game and capture runtime behavior. Pure subprocess — no LLM.

    kwargs:
        target (str):    Target engine — "pygame" or "godot". Default "pygame".
        timeout (int):   Max seconds to run before killing the process. Default 30.
        headless (bool): Run without display (SDL_VIDEODRIVER=dummy for Pygame,
                         --headless for Godot). Default True.
    """
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)

    target: str = kwargs.get("target", "pygame")
    timeout: int = kwargs.get("timeout", 30)
    headless: bool = kwargs.get("headless", True)

    # Clamp timeout to reasonable bounds
    timeout = max(5, min(timeout, 300))

    if target == "pygame":
        return _run_pygame(timeout, headless)
    elif target == "godot":
        return _run_godot(timeout, headless)
    else:
        return {
            "status": "skipped",
            "summary": f"Unknown target '{target}'. Supported: pygame, godot.",
            "metrics": {
                "exit_code": -1,
                "runtime_seconds": 0.0,
                "stderr_lines": 0,
                "crash_detected": False,
                "target": target,
            },
            "errors": [],
            "stdout": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }
