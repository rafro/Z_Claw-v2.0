"""
ci-runner skill — Validates code changes before keeping them.
Runs syntax checks and pytest on affected files/modules.
Reverts changes (from .bak) if tests fail.
Tier 0 (pure Python + subprocess — no LLM).
"""

import ast
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

from runtime.config import ROOT

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _syntax_check(file_path: Path) -> tuple[bool, str]:
    """Run ast.parse on a Python file. Returns (ok, error_message)."""
    try:
        source = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"Could not read file: {e}"

    try:
        ast.parse(source, filename=str(file_path))
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}"


def _find_test_target(file_path: Path) -> str | None:
    """
    Resolve the best pytest target for a given source file.

    Search order:
      1. tests/test_{stem}.py          (flat test dir)
      2. tests/{subpackage}/test_{stem}.py  (mirrored structure)
      3. None  (caller falls back to full suite)
    """
    stem = file_path.stem  # e.g. "auto_fix"
    tests_root = ROOT / "tests"

    # Flat match
    flat = tests_root / f"test_{stem}.py"
    if flat.is_file():
        return str(flat)

    # Try to mirror the source subpackage.  e.g. runtime/skills/foo.py -> tests/skills/test_foo.py
    try:
        rel = file_path.relative_to(ROOT / "runtime")
        mirrored = tests_root / rel.parent / f"test_{stem}.py"
        if mirrored.is_file():
            return str(mirrored)
    except ValueError:
        pass

    # Broad search under tests/
    if tests_root.is_dir():
        for match in tests_root.rglob(f"test_{stem}.py"):
            return str(match)

    return None


def _revert_from_backup(file_path: Path) -> bool:
    """Restore file from its .bak sibling. Returns True on success."""
    backup = Path(f"{file_path}.bak")
    if not backup.is_file():
        log.warning("ci-runner: no backup found at %s — cannot revert", backup)
        return False
    try:
        shutil.copy2(backup, file_path)
        log.info("ci-runner: reverted %s from backup", file_path)
        return True
    except Exception as e:
        log.error("ci-runner: revert failed for %s: %s", file_path, e)
        return False


def _parse_pytest_summary(output: str) -> tuple[int, int]:
    """
    Extract (passed, failed) counts from pytest's final summary line.
    Handles patterns like "5 passed", "2 failed, 3 passed", etc.
    """
    passed = 0
    failed = 0
    for line in reversed(output.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            for m in re.finditer(r"(\d+)\s+(passed)", line):
                passed = int(m.group(1))
            for m in re.finditer(r"(\d+)\s+(failed|error)", line):
                failed += int(m.group(1))
            if passed or failed:
                break
    return passed, failed


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(**kwargs) -> dict:
    """
    Validate a code change via syntax check and pytest.

    kwargs:
        file_path (str):          Absolute or ROOT-relative path to the changed file. Required.
        revert_on_failure (bool): Restore from {file}.bak when tests fail. Default True.
    """
    raw_path = kwargs.get("file_path", "")
    revert_on_failure = bool(kwargs.get("revert_on_failure", True))

    # -- Validate inputs ----------------------------------------------------
    if not raw_path:
        return {
            "status": "skipped",
            "summary": "CI runner: no file_path provided.",
            "metrics": {
                "syntax_ok": False,
                "tests_run": 0,
                "tests_passed": 0,
                "tests_failed": 0,
                "reverted": False,
            },
            "test_output": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    file_path = Path(raw_path)
    if not file_path.is_absolute():
        file_path = ROOT / file_path

    if not file_path.is_file():
        return {
            "status": "failed",
            "summary": f"CI runner: file not found — {file_path}",
            "metrics": {
                "syntax_ok": False,
                "tests_run": 0,
                "tests_passed": 0,
                "tests_failed": 0,
                "reverted": False,
            },
            "test_output": "",
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # -- Step 1: Syntax check -----------------------------------------------
    syntax_ok, syntax_err = _syntax_check(file_path)

    if not syntax_ok:
        reverted = False
        if revert_on_failure:
            reverted = _revert_from_backup(file_path)

        summary = f"CI: syntax FAILED — {syntax_err}"
        if reverted:
            summary += " (reverted from backup)"

        return {
            "status": "failed",
            "summary": summary,
            "metrics": {
                "syntax_ok": False,
                "tests_run": 0,
                "tests_passed": 0,
                "tests_failed": 0,
                "reverted": reverted,
            },
            "test_output": syntax_err,
            "escalate": not reverted,
            "escalation_reason": (
                f"Syntax error in {file_path.name} and no backup to revert"
                if not reverted else ""
            ),
            "action_items": [],
        }

    # -- Step 2: Locate test target -----------------------------------------
    test_target = _find_test_target(file_path)
    if test_target is None:
        # Fall back to the full runtime test suite (fast-fail)
        test_target = str(ROOT / "runtime")

    # -- Step 3: Run pytest --------------------------------------------------
    test_output = ""
    tests_passed = 0
    tests_failed = 0
    tests_run = 0

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                test_target,
                "-x",               # stop on first failure
                "--tb=short",        # concise tracebacks
                "--timeout=30",      # per-test timeout (requires pytest-timeout)
                "-q",                # quiet — less noise in logs
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
        )

        test_output = (result.stdout + "\n" + result.stderr).strip()
        tests_passed, tests_failed = _parse_pytest_summary(test_output)
        tests_run = tests_passed + tests_failed

    except FileNotFoundError:
        test_output = "pytest not found. Install with: pip install pytest"
    except subprocess.TimeoutExpired:
        test_output = "Test execution timed out after 60 seconds."
        tests_failed = 1
    except Exception as e:
        test_output = f"Test execution error: {e}"
        tests_failed = 1

    # -- Step 4: Revert on failure ------------------------------------------
    reverted = False
    if tests_failed > 0 and revert_on_failure:
        reverted = _revert_from_backup(file_path)

    # -- Build result -------------------------------------------------------
    all_passed = tests_failed == 0 and tests_run > 0
    status = "success" if all_passed else ("failed" if tests_failed else "success")

    # Truncate test output for packet storage (keep tail — most useful part)
    max_output = 3000
    if len(test_output) > max_output:
        test_output = "...(truncated)\n" + test_output[-max_output:]

    summary_parts = [f"CI: syntax OK, {tests_passed}/{tests_run} tests passed."]
    if reverted:
        summary_parts.append("Change reverted from backup.")
    elif tests_failed and not reverted and revert_on_failure:
        summary_parts.append("Revert attempted but no .bak found.")

    escalate = tests_failed > 0 and not reverted
    escalation_reason = (
        f"{tests_failed} test failure(s) in {file_path.name} — could not auto-revert"
        if escalate else ""
    )

    return {
        "status": status,
        "summary": " ".join(summary_parts),
        "metrics": {
            "syntax_ok": True,
            "tests_run": tests_run,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "reverted": reverted,
        },
        "test_output": test_output,
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }
