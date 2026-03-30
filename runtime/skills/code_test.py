"""
code-test skill — Runs or generates test suites for game code.
For Python targets: executes pytest. For Godot: generates GUT test scripts.
Can also use LLM to generate test cases from code.
Tier 1 for test generation, Tier 0 for execution.
"""

import json
import logging
import subprocess
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_7B, STATE_DIR
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
MODEL = MODEL_7B
GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"


def _scan_source_files(target_dir: Path) -> list[Path]:
    """Find all source files in a target directory (excluding tests)."""
    files = []
    if not target_dir.exists():
        return files
    for ext in ("*.gd", "*.py"):
        for p in target_dir.rglob(ext):
            if "tests" not in p.parts and "__pycache__" not in p.parts:
                files.append(p)
    return sorted(files, key=lambda p: p.name)


def _scan_test_files(target_dir: Path) -> list[Path]:
    """Find all test files in a target's tests/ directory."""
    tests_dir = target_dir / "tests"
    if not tests_dir.exists():
        return []
    files = []
    for ext in ("*.gd", "*.py"):
        for p in tests_dir.rglob(ext):
            if "__pycache__" not in p.parts:
                files.append(p)
    return sorted(files, key=lambda p: p.name)


def _detect_targets() -> list[str]:
    """Detect which target directories exist in the project."""
    targets = []
    if not PROJECT_DIR.exists():
        return targets
    for candidate in ("godot", "pygame", "generic"):
        if (PROJECT_DIR / candidate).exists():
            targets.append(candidate)
    return targets


def _action_status(target: str) -> dict:
    """Report on project test status."""
    targets = _detect_targets() if not target else [target]
    if not targets:
        return {
            "status": "partial",
            "summary": "No project targets found. Generate code first.",
            "metrics": {
                "source_files": 0,
                "test_files": 0,
                "targets": [],
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    total_source = 0
    total_tests = 0
    target_details = {}

    for t in targets:
        target_dir = PROJECT_DIR / t
        sources = _scan_source_files(target_dir)
        tests = _scan_test_files(target_dir)
        total_source += len(sources)
        total_tests += len(tests)
        coverage_estimate = (
            f"{len(tests)}/{len(sources)} files"
            if sources else "no source files"
        )
        target_details[t] = {
            "source_files": len(sources),
            "test_files": len(tests),
            "coverage_estimate": coverage_estimate,
        }

    summary_parts = [f"Test status: {total_source} source file(s), {total_tests} test file(s)."]
    for t, d in target_details.items():
        summary_parts.append(f"  {t}: {d['coverage_estimate']}")

    return {
        "status": "success",
        "summary": " ".join(summary_parts),
        "metrics": {
            "source_files": total_source,
            "test_files": total_tests,
            "targets": targets,
            "target_details": target_details,
            "tests_generated": 0,
            "tests_passed": 0,
            "tests_failed": 0,
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }


def _action_generate(target: str, file_path: str) -> dict:
    """Generate test cases from source code using LLM."""
    if not target:
        targets = _detect_targets()
        target = targets[0] if targets else "godot"

    target_dir = PROJECT_DIR / target

    # Determine which file(s) to generate tests for
    if file_path:
        source_file = PROJECT_DIR / file_path
        if not source_file.exists():
            return {
                "status": "partial",
                "summary": f"Source file not found: {file_path}",
                "metrics": {"tests_generated": 0, "test_files": []},
                "escalate": False,
                "escalation_reason": "",
                "action_items": [],
            }
        source_files = [source_file]
    else:
        source_files = _scan_source_files(target_dir)
        if not source_files:
            return {
                "status": "partial",
                "summary": f"No source files found in {target}/ to generate tests from.",
                "metrics": {"tests_generated": 0, "test_files": []},
                "escalate": False,
                "escalation_reason": "",
                "action_items": [],
            }

    if not is_available(MODEL, host=OLLAMA_HOST):
        return {
            "status": "degraded",
            "summary": f"Test generation for {target} — LLM unavailable.",
            "metrics": {
                "tests_generated": 0,
                "test_files": [],
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    tests_dir = target_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    generated_files = []
    total_tests_generated = 0

    for src in source_files:
        try:
            code_content = src.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Failed to read %s: %s", src, e)
            continue

        if not code_content.strip():
            continue

        # Truncate large files
        code_for_llm = code_content[:4000]
        if len(code_content) > 4000:
            code_for_llm += "\n# ... (truncated)"

        if target == "godot":
            system_prompt = (
                "You are a game test engineer. Generate GUT (Godot Unit Test) test scripts "
                "for the provided GDScript code. Use GUT conventions:\n"
                "- Extend GutTest\n"
                "- Test functions start with test_\n"
                "- Use assert_eq, assert_true, assert_false, assert_null, etc.\n"
                "- Test edge cases and boundary conditions.\n"
                "Only output the test code, no explanations."
            )
            test_ext = ".gd"
            test_prefix = "test_"
        else:
            system_prompt = (
                "You are a game test engineer. Generate pytest test functions "
                "for the provided Python game code. Conventions:\n"
                "- Use pytest style (def test_xxx).\n"
                "- Use assert statements.\n"
                "- Mock external dependencies (pygame.display, etc.) with unittest.mock.\n"
                "- Test edge cases and boundary conditions.\n"
                "Only output the test code, no explanations."
            )
            test_ext = ".py"
            test_prefix = "test_"

        user_prompt = (
            f"Generate tests for this {target} game code.\n"
            f"File: {src.name}\n\n"
            f"```\n{code_for_llm}\n```"
        )

        try:
            response = chat(MODEL, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ], temperature=0.2, max_tokens=1500, task_type="code-test")
        except Exception as e:
            log.error("Test generation failed for %s: %s", src.name, e)
            continue

        # Strip markdown code fences
        test_code = response.strip()
        if test_code.startswith("```"):
            lines = test_code.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            test_code = "\n".join(lines)

        # Save test file
        test_name = f"{test_prefix}{src.stem}{test_ext}"
        test_file = tests_dir / test_name
        with open(test_file, "w", encoding="utf-8") as f:
            f.write(test_code)

        generated_files.append(str(test_file.relative_to(PROJECT_DIR)))
        total_tests_generated += test_code.count("def test_") + test_code.count("func test_")

    summary = (
        f"Generated {len(generated_files)} test file(s) with ~{total_tests_generated} test(s) "
        f"for {target}."
    )

    return {
        "status": "success",
        "summary": summary,
        "metrics": {
            "tests_generated": total_tests_generated,
            "tests_passed": 0,
            "tests_failed": 0,
            "test_files": generated_files,
            "target": target,
            "model_available": True,
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }


def _action_run(target: str) -> dict:
    """Run tests for the given target."""
    if not target:
        targets = _detect_targets()
        # Prefer Python targets for direct execution
        target = next((t for t in targets if t in ("pygame", "generic")), None)
        if not target and targets:
            target = targets[0]

    if not target:
        return {
            "status": "partial",
            "summary": "No target found for test execution.",
            "metrics": {"tests_passed": 0, "tests_failed": 0},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    tests_dir = PROJECT_DIR / target / "tests"
    if not tests_dir.exists() or not list(tests_dir.iterdir()):
        return {
            "status": "partial",
            "summary": f"No tests found in {target}/tests/. Run with action='generate' first.",
            "metrics": {"tests_passed": 0, "tests_failed": 0, "test_files": []},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    test_output = ""
    tests_passed = 0
    tests_failed = 0

    if target in ("pygame", "generic"):
        # Run pytest
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", str(tests_dir), "-v", "--tb=short", "--no-header"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(PROJECT_DIR / target),
            )
            test_output = result.stdout + result.stderr

            # Parse pytest output for pass/fail counts
            for line in test_output.splitlines():
                if " passed" in line and ("failed" in line or "error" in line or "passed" in line):
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if p == "passed" and i > 0:
                            try:
                                tests_passed = int(parts[i - 1])
                            except ValueError:
                                pass
                        if p == "failed" and i > 0:
                            try:
                                tests_failed = int(parts[i - 1])
                            except ValueError:
                                pass

        except FileNotFoundError:
            test_output = "pytest not found. Install with: pip install pytest"
        except subprocess.TimeoutExpired:
            test_output = "Test execution timed out after 60 seconds."
        except Exception as e:
            test_output = f"Test execution failed: {e}"

    elif target == "godot":
        # Can't run Godot tests directly — generate a command
        test_output = (
            "Godot tests require Godot editor with GUT plugin.\n"
            "Run headless: godot --headless --script res://addons/gut/gut_cmdln.gd\n"
            f"Test directory: {tests_dir}"
        )

    summary_parts = [f"Test run ({target}):"]
    if tests_passed or tests_failed:
        summary_parts.append(f"{tests_passed} passed, {tests_failed} failed.")
    else:
        summary_parts.append("see test_output for details.")

    escalate = tests_failed > 5
    escalation_reason = (
        f"{tests_failed} test failures in {target}"
        if escalate else ""
    )

    return {
        "status": "success",
        "summary": " ".join(summary_parts),
        "test_output": test_output,
        "metrics": {
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "test_files": [str(p.relative_to(PROJECT_DIR)) for p in _scan_test_files(PROJECT_DIR / target)],
            "target": target,
        },
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }


def run(**kwargs) -> dict:
    """
    Run or generate tests for game code.

    kwargs:
        action (str):    "generate" | "run" | "status". Default "status".
        target (str):    Target engine — "godot", "pygame", or "generic".
        file_path (str): Path relative to project dir for targeted test generation.
    """
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)

    action = kwargs.get("action", "status")
    target = kwargs.get("target", "")
    file_path = kwargs.get("file_path", "")

    if action == "generate":
        return _action_generate(target, file_path)
    elif action == "run":
        return _action_run(target)
    else:
        return _action_status(target)
