"""
TestRunner — dev pipeline step 3.
Attempts to run or statically analyze generated code.
Provider chain: deterministic (primary) → ollama:coder-7b (for analysis fallback).
Never executes arbitrary code without explicit sandbox flag.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Patterns that make execution unsafe to attempt
UNSAFE_PATTERNS = [
    r"os\.system\(",
    r"subprocess\.",
    r"shutil\.rmtree\(",
    r"open\(.+[\"']w[\"']\)",
    r"socket\.",
    r"urllib\.request\.",
    r"requests\.",
    r"eval\(",
    r"exec\(",
    r"__import__\(",
]


def _is_safe_to_execute(code: str) -> bool:
    """Conservative check — if any unsafe pattern found, decline execution."""
    for pattern in UNSAFE_PATTERNS:
        if re.search(pattern, code):
            return False
    return True


class TestRunner:

    def run(
        self,
        code: str,
        language: str = "python",
        safe_execute: bool = False,
    ) -> dict[str, Any]:
        """
        Attempt static analysis and optionally safe execution.

        Returns:
        {
            "tests_run": int,
            "passed": int,
            "failed": int,
            "errors": [str],
            "syntax_ok": bool,
            "safe_to_run": bool,
            "provider_used": "deterministic",
            "status": "success" | "failed",
        }
        """
        result: dict[str, Any] = {
            "tests_run": 0,
            "passed": 0,
            "failed": 0,
            "errors": [],
            "syntax_ok": False,
            "safe_to_run": False,
            "provider_used": "deterministic",
            "status": "success",
        }

        if not code.strip():
            result["errors"].append("No code to test")
            result["status"] = "failed"
            return result

        if language == "python":
            return self._test_python(code, safe_execute, result)
        else:
            result["errors"].append(f"Static analysis not yet implemented for {language}")
            return result

    def _test_python(self, code: str, safe_execute: bool, result: dict) -> dict:
        # Step 1: Syntax check
        try:
            ast.parse(code)
            result["syntax_ok"] = True
            result["tests_run"] += 1
            result["passed"] += 1
        except SyntaxError as e:
            result["syntax_ok"] = False
            result["tests_run"] += 1
            result["failed"] += 1
            result["errors"].append(f"SyntaxError: {e}")
            return result

        # Step 2: Safety check
        result["safe_to_run"] = _is_safe_to_execute(code)

        # Step 3: Try execution in subprocess if safe and requested
        if safe_execute and result["safe_to_run"]:
            try:
                with tempfile.NamedTemporaryFile(suffix=".py", mode="w",
                                                  delete=False, encoding="utf-8") as tf:
                    tf.write(code)
                    tmp_path = tf.name

                proc = subprocess.run(
                    [sys.executable, tmp_path],
                    capture_output=True, text=True, timeout=10
                )
                result["tests_run"] += 1
                if proc.returncode == 0:
                    result["passed"] += 1
                else:
                    result["failed"] += 1
                    result["errors"].append(proc.stderr.strip()[:500])

                Path(tmp_path).unlink(missing_ok=True)
            except subprocess.TimeoutExpired:
                result["failed"] += 1
                result["errors"].append("Execution timed out (10s)")
            except Exception as e:
                result["failed"] += 1
                result["errors"].append(f"Execution error: {e}")

        return result
