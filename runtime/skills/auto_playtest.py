"""
auto-playtest skill — Generates and runs automated gameplay test harnesses.
For Pygame: generates a Python script that imports the game, sends simulated
input via pygame.event.post(), and captures gameplay metrics.
Uses LLM (Coder 7B) to analyze game code and generate appropriate input sequences.
Fallback: random input sequences.
Tier 1 for harness generation, Tier 0 for execution.
"""

import json
import logging
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_CODER_7B, STATE_DIR
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
MODEL = MODEL_CODER_7B
GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"
PLAYTEST_DATA = GAMEDEV_DIR / "playtest-data.jsonl"
SCREENSHOTS_DIR = GAMEDEV_DIR / "screenshots"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_main_file(target: str) -> Path | None:
    """Locate the game's main entry point."""
    target_dir = PROJECT_DIR / target
    if not target_dir.exists():
        return None
    candidates = ["main.py", "game.py", "app.py", "run.py"]
    for name in candidates:
        p = target_dir / name
        if p.exists():
            return p
    # Fallback: first .py file at top level
    py_files = sorted(target_dir.glob("*.py"))
    return py_files[0] if py_files else None


def _read_source_code(path: Path, max_chars: int = 4000) -> str:
    """Read source code, truncating if necessary."""
    try:
        text = path.read_text(encoding="utf-8")
        if len(text) > max_chars:
            return text[:max_chars] + "\n# ... (truncated)"
        return text
    except Exception as e:
        log.warning("Failed to read %s: %s", path, e)
        return ""


def _generate_smart_harness(source_code: str, main_file: Path, duration: int) -> str | None:
    """Use Coder 7B to generate a playtest harness from game source code."""
    if not is_available(MODEL, host=OLLAMA_HOST):
        log.info("Coder 7B not available, falling back to random harness")
        return None

    system_prompt = (
        "You are a game test automation engineer. Generate a Python script that:\n"
        "1. Imports and runs the target Pygame game in a controlled manner.\n"
        "2. Sends simulated input via pygame.event.post() based on the game's input handlers.\n"
        "3. Tracks gameplay metrics: frames rendered, exceptions, game-over events, etc.\n"
        "4. Prints a single JSON line to stdout on the last line with collected metrics.\n\n"
        "The harness MUST:\n"
        "- Use pygame.event.post() to send KEYDOWN/KEYUP events.\n"
        "- Run for the specified duration then quit cleanly.\n"
        "- Catch and count exceptions without crashing.\n"
        "- Print metrics as the LAST line of stdout in this JSON format:\n"
        '  {"frames_rendered": N, "exceptions": N, "game_over_events": N, '
        '"health_min": N, "health_max": N, "enemies_killed": N, '
        '"items_collected": N, "time_alive_seconds": N.N}\n\n'
        "Only output the Python code. No explanations."
    )

    user_prompt = (
        f"Generate a playtest harness for this Pygame game.\n"
        f"Game file: {main_file.name}\n"
        f"Duration: {duration} seconds\n\n"
        f"```python\n{source_code}\n```"
    )

    try:
        response = chat(MODEL, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.15, max_tokens=2048, task_type="auto-playtest")
    except Exception as e:
        log.error("LLM harness generation failed: %s", e)
        return None

    # Strip markdown code fences
    code = response.strip()
    if code.startswith("```"):
        lines = code.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        code = "\n".join(lines)

    return code


def _generate_random_harness(main_file: Path, duration: int) -> str:
    """Generate a fallback harness that sends random pygame inputs."""
    # Use the game's directory name and main file for import path construction
    game_dir = main_file.parent
    module_name = main_file.stem

    return textwrap.dedent(f"""\
        \"\"\"Auto-generated random playtest harness.\"\"\"
        import json
        import os
        import random
        import sys
        import time
        import traceback

        # Ensure the game directory is on sys.path
        sys.path.insert(0, {str(game_dir)!r})

        # Set SDL to use a virtual display if no display is available
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

        import pygame

        DURATION = {duration}
        KEYS = [
            pygame.K_w, pygame.K_a, pygame.K_s, pygame.K_d,
            pygame.K_SPACE, pygame.K_RETURN, pygame.K_UP,
            pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT,
            pygame.K_e, pygame.K_q, pygame.K_ESCAPE,
        ]

        metrics = {{
            "frames_rendered": 0,
            "exceptions": 0,
            "game_over_events": 0,
            "health_min": 100,
            "health_max": 100,
            "enemies_killed": 0,
            "items_collected": 0,
            "time_alive_seconds": 0.0,
        }}


        def send_random_input():
            \"\"\"Post a random key event to the pygame event queue.\"\"\"
            key = random.choice(KEYS)
            event_type = random.choice([pygame.KEYDOWN, pygame.KEYUP])
            try:
                event = pygame.event.Event(event_type, key=key)
                pygame.event.post(event)
            except Exception:
                pass


        def main():
            pygame.init()
            try:
                screen = pygame.display.set_mode((800, 600))
            except Exception:
                screen = None

            clock = pygame.time.Clock()
            start_time = time.time()
            last_input_time = start_time
            input_interval = 0.3  # seconds between random inputs

            try:
                while True:
                    elapsed = time.time() - start_time
                    if elapsed >= DURATION:
                        break

                    # Send random inputs at intervals
                    if time.time() - last_input_time >= input_interval:
                        send_random_input()
                        last_input_time = time.time()

                    # Process events
                    for event in pygame.event.get():
                        if event.type == pygame.QUIT:
                            metrics["game_over_events"] += 1

                    # Tick and count frames
                    try:
                        clock.tick(30)
                        metrics["frames_rendered"] += 1
                    except Exception:
                        metrics["exceptions"] += 1

                    metrics["time_alive_seconds"] = round(elapsed, 1)

            except Exception as exc:
                metrics["exceptions"] += 1
                traceback.print_exc(file=sys.stderr)
            finally:
                metrics["time_alive_seconds"] = round(time.time() - start_time, 1)
                try:
                    pygame.quit()
                except Exception:
                    pass

            # Print metrics as the LAST line of stdout
            print(json.dumps(metrics))


        if __name__ == "__main__":
            main()
    """)


def _parse_metrics(stdout: str) -> dict | None:
    """Extract the metrics JSON from the last line of stdout."""
    lines = stdout.strip().splitlines()
    if not lines:
        return None

    # Try last line first, then scan backwards for JSON
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _append_playtest_data(target: str, duration: int, metrics: dict) -> None:
    """Append playtest results to playtest-data.jsonl."""
    PLAYTEST_DATA.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_type": "auto",
        "target": target,
        "duration": duration,
        "metrics": metrics,
    }
    with open(PLAYTEST_DATA, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(**kwargs) -> dict:
    """
    Generate and run an automated gameplay test harness.

    kwargs:
        target (str):            Target engine — "pygame" (default).
        duration_seconds (int):  How long to run the playtest (default 15).
        input_strategy (str):    "smart" (LLM-generated) or "random" (default "smart").
    """
    target = kwargs.get("target", "pygame")
    duration = kwargs.get("duration_seconds", 15)
    strategy = kwargs.get("input_strategy", "smart")

    PROJECT_DIR.mkdir(parents=True, exist_ok=True)

    # --- Phase 0: Locate game entry point ---
    main_file = _find_main_file(target)
    if not main_file:
        return {
            "status": "partial",
            "summary": f"No game entry point found in {target}/. Generate code first.",
            "metrics": {"target": target, "harness_generated": False},
            "escalate": False,
            "escalation_reason": "",
            "action_items": ["Run code-generate to create game source files."],
        }

    source_code = _read_source_code(main_file)

    # --- Phase 1: Generate harness ---
    harness_code = None
    harness_source = "random"  # track which method produced the harness

    if strategy == "smart":
        harness_code = _generate_smart_harness(source_code, main_file, duration)
        if harness_code:
            harness_source = "smart"

    if harness_code is None:
        harness_code = _generate_random_harness(main_file, duration)
        harness_source = "random"

    # Save harness
    tests_dir = PROJECT_DIR / target / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    harness_path = tests_dir / "auto_playtest_harness.py"
    harness_path.write_text(harness_code, encoding="utf-8")
    log.info("Playtest harness written to %s (strategy=%s)", harness_path, harness_source)

    # --- Phase 2: Run harness ---
    project_dir = PROJECT_DIR / target
    run_stdout = ""
    run_stderr = ""
    crashed = False

    try:
        result = subprocess.run(
            [sys.executable, str(harness_path)],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=duration + 10,
        )
        run_stdout = result.stdout
        run_stderr = result.stderr
        if result.returncode != 0:
            log.warning("Playtest harness exited with code %d", result.returncode)
            if result.returncode < 0:
                crashed = True
    except subprocess.TimeoutExpired:
        run_stderr = f"Playtest harness timed out after {duration + 10} seconds."
        crashed = True
        log.warning(run_stderr)
    except FileNotFoundError:
        run_stderr = f"Python interpreter not found: {sys.executable}"
        crashed = True
        log.error(run_stderr)
    except Exception as e:
        run_stderr = f"Playtest execution failed: {e}"
        crashed = True
        log.error(run_stderr)

    # --- Parse metrics ---
    metrics = _parse_metrics(run_stdout)
    if metrics is None:
        metrics = {
            "frames_rendered": 0,
            "exceptions": 1 if crashed else 0,
            "game_over_events": 0,
            "health_min": 0,
            "health_max": 0,
            "enemies_killed": 0,
            "items_collected": 0,
            "time_alive_seconds": 0.0,
        }

    # --- Phase 3: Write playtest data ---
    _append_playtest_data(target, duration, metrics)

    # --- Build result ---
    has_exceptions = metrics.get("exceptions", 0) > 0
    escalate = crashed or metrics.get("exceptions", 0) > 3
    escalation_reason = ""
    if crashed:
        escalation_reason = "Game crashed during automated playtest."
    elif metrics.get("exceptions", 0) > 3:
        escalation_reason = f"{metrics['exceptions']} exceptions during playtest."

    action_items = []
    if crashed:
        action_items.append("Investigate crash: check stderr output and fix game code.")
    if has_exceptions and not crashed:
        action_items.append(f"Review {metrics['exceptions']} exception(s) during playtest.")
    if metrics.get("frames_rendered", 0) == 0 and not crashed:
        action_items.append("Harness rendered 0 frames — verify game loop integration.")

    summary_parts = [
        f"Auto-playtest ({target}, {harness_source} strategy, {duration}s):",
        f"{metrics.get('frames_rendered', 0)} frames,",
        f"{metrics.get('exceptions', 0)} exceptions,",
        f"{metrics.get('time_alive_seconds', 0)}s alive.",
    ]
    if crashed:
        summary_parts.append("CRASHED.")

    status = "failed" if crashed else "success"

    return {
        "status": status,
        "summary": " ".join(summary_parts),
        "metrics": {
            **metrics,
            "target": target,
            "harness_source": harness_source,
            "harness_path": str(harness_path),
            "crashed": crashed,
        },
        "stderr": run_stderr if run_stderr else "",
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": action_items,
    }
