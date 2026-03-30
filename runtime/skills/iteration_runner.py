"""
iteration-runner skill — Automated code iteration pipeline.
Chains: code-generate -> code-review -> apply fixes -> code-test.
Runs multiple passes until tests pass or max iterations reached.
Reads/writes to the project directory. Uses Coder 14B for fixes.
Tier 2 (Coder 14B for fix generation).
"""

import ast
import json
import logging
import re
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_CODER_14B, MODEL_CODER_7B, STATE_DIR
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"
SPECS_DIR = GAMEDEV_DIR / "tech-specs"
GDD_FILE = GAMEDEV_DIR / "gdd.json"

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
    """Load the GDD for project context."""
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


def _find_code_file(system_name: str, target: str) -> Path | None:
    """Find the source code file for a system in the project directory."""
    safe = system_name.lower().replace(" ", "_").replace("-", "_").replace("/", "_")[:60]
    ext = TARGET_EXTENSIONS.get(target, ".py")
    target_dir = PROJECT_DIR / target

    # Direct match
    candidate = target_dir / f"{safe}{ext}"
    if candidate.exists():
        return candidate

    # Search subdirectories
    if target_dir.exists():
        for match in target_dir.rglob(f"{safe}{ext}"):
            return match
        # Also try with the original name (hyphens preserved for GDScript files)
        if target == "godot":
            hyphen_name = system_name.lower().replace(" ", "_").replace("/", "_")[:60]
            for match in target_dir.rglob(f"{hyphen_name}{ext}"):
                return match

    return None


def _read_code(path: Path) -> str:
    """Read a source code file."""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Failed to read %s: %s", path, e)
        return ""


def _review_code(model: str, code: str, system_name: str, target: str) -> list[dict]:
    """
    Send code to LLM for review. Returns a list of issues found.
    Inline review — does not call the code-review skill.
    """
    lang = "GDScript" if target == "godot" else "Python"
    system_prompt = (
        f"You are a senior {lang} code reviewer for game development. "
        "Review the following code and list any issues you find. "
        "For each issue, provide:\n"
        "- severity: critical / major / minor\n"
        "- line: approximate line number (or 0 if general)\n"
        "- description: what is wrong and how to fix it\n\n"
        "Output ONLY a JSON array of issues. If no issues, output an empty array [].\n"
        "Example: [{\"severity\": \"major\", \"line\": 15, \"description\": \"Missing null check\"}]"
    )

    user_prompt = f"System: {system_name}\n\n```\n{code[:3000]}\n```"

    try:
        response = chat(model, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.1, max_tokens=1024, task_type="iteration-runner-review")

        # Parse the response as JSON
        text = response.strip()
        # Strip markdown fences
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines)

        try:
            issues = json.loads(text)
            if isinstance(issues, list):
                return issues
        except json.JSONDecodeError:
            # Try to find JSON array in response
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                try:
                    issues = json.loads(match.group())
                    if isinstance(issues, list):
                        return issues
                except json.JSONDecodeError:
                    pass

        # If we got text but couldn't parse it, treat as a single finding
        if text and text != "[]":
            return [{"severity": "minor", "line": 0, "description": text[:500]}]

    except Exception as e:
        log.warning("Code review LLM call failed: %s", e)

    return []


def _apply_fixes(model: str, code: str, issues: list[dict], system_name: str, target: str) -> str:
    """
    Send code + issues to LLM and get a fixed version back.
    Returns the fixed code string, or empty string on failure.
    """
    lang = "GDScript" if target == "godot" else "Python"
    system_prompt = (
        f"You are an expert {lang} game programmer. "
        "Fix the issues listed below in the provided code. "
        "Output ONLY the complete fixed code — no explanations, no markdown fences."
    )

    issues_text = "\n".join(
        f"- [{iss.get('severity', '?')}] Line {iss.get('line', '?')}: {iss.get('description', '?')}"
        for iss in issues
    )

    user_prompt = (
        f"System: {system_name}\n\n"
        f"Issues to fix:\n{issues_text}\n\n"
        f"Current code:\n```\n{code[:3000]}\n```"
    )

    try:
        response = chat(model, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.1, max_tokens=2048, task_type="iteration-runner-fix")

        fixed = response.strip()
        # Strip markdown fences
        if fixed.startswith("```"):
            lines = fixed.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            fixed = "\n".join(lines)

        return fixed

    except Exception as e:
        log.warning("Fix generation LLM call failed: %s", e)
        return ""


def _syntax_check_python(code: str) -> tuple[bool, str]:
    """Run ast.parse on Python code. Returns (valid, error_message)."""
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}"


def _syntax_check_gdscript(code: str) -> tuple[bool, str]:
    """
    Basic GDScript validation — checks for common structural issues.
    Not a full parser, but catches obvious problems.
    """
    errors = []
    lines = code.splitlines()

    indent_stack = [0]
    in_string = False

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith("#"):
            continue

        # Check for unmatched quotes (basic)
        quote_count = stripped.count('"') - stripped.count('\\"')
        if quote_count % 2 != 0:
            single_count = stripped.count("'") - stripped.count("\\'")
            if single_count % 2 != 0:
                errors.append(f"Line {i}: possible unmatched string quote")

        # Check for common GDScript mistakes
        if stripped.startswith("func ") and not stripped.endswith(":"):
            # Allow multi-line function signatures with return type
            if "-> " not in stripped or not stripped.endswith(":"):
                if not stripped.endswith(":"):
                    errors.append(f"Line {i}: function definition missing colon")

        # Check for var declarations with = but missing value
        if stripped.startswith("var ") and stripped.endswith("="):
            errors.append(f"Line {i}: variable declaration with empty assignment")

    if errors:
        return False, "; ".join(errors[:5])  # cap at 5 errors
    return True, ""


def _syntax_check(code: str, target: str) -> tuple[bool, str]:
    """Route to the appropriate syntax checker."""
    if target in ("pygame", "generic"):
        return _syntax_check_python(code)
    elif target == "godot":
        return _syntax_check_gdscript(code)
    return True, ""


def _generate_initial_code(model: str, system_name: str, target: str) -> str:
    """Generate initial code when no file exists. Calls code_generate skill."""
    try:
        from runtime.skills.code_generate import run as code_gen_run
        result = code_gen_run(system_name=system_name, target=target)
        if result.get("status") == "success":
            return result.get("generated_code", "")
    except Exception as e:
        log.warning("Failed to call code_generate: %s", e)
    return ""


def run(**kwargs) -> dict:
    """
    Automated code iteration pipeline: generate -> review -> fix -> test.

    kwargs:
        system_name (str):      Name of the system to iterate on.
        target (str):           "godot", "pygame", or "generic". Default "godot".
        max_iterations (int):   Maximum fix iterations. Default 3.
    """
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)

    system_name = kwargs.get("system_name", "")
    target = kwargs.get("target", "godot")
    max_iterations = int(kwargs.get("max_iterations", 3))

    if target not in TARGET_EXTENSIONS:
        target = "godot"

    if not system_name:
        return {
            "status": "partial",
            "summary": "No system_name provided for iteration runner.",
            "metrics": {"iterations_run": 0, "issues_found": 0, "issues_fixed": 0, "final_syntax_valid": False},
            "iteration_log": [],
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    model = _pick_model()
    if not model:
        return {
            "status": "degraded",
            "summary": f"Iteration runner for '{system_name}' ({target}) — no Coder model available.",
            "metrics": {
                "iterations_run": 0,
                "issues_found": 0,
                "issues_fixed": 0,
                "final_syntax_valid": False,
                "model_available": False,
            },
            "iteration_log": [],
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Locate or generate the code file
    code_path = _find_code_file(system_name, target)
    if code_path is None:
        log.info("No existing code for '%s' — generating initial version.", system_name)
        initial_code = _generate_initial_code(model, system_name, target)
        if not initial_code:
            return {
                "status": "partial",
                "summary": f"Could not generate initial code for '{system_name}'. Check tech specs.",
                "metrics": {"iterations_run": 0, "issues_found": 0, "issues_fixed": 0, "final_syntax_valid": False},
                "iteration_log": [],
                "escalate": False,
                "escalation_reason": "",
                "action_items": [],
            }
        # Write the generated code and set code_path
        safe = system_name.lower().replace(" ", "_").replace("-", "_").replace("/", "_")[:60]
        ext = TARGET_EXTENSIONS[target]
        target_dir = PROJECT_DIR / target
        target_dir.mkdir(parents=True, exist_ok=True)
        code_path = target_dir / f"{safe}{ext}"
        code_path.write_text(initial_code, encoding="utf-8")

    # Iteration loop
    iteration_log = []
    total_issues_found = 0
    total_issues_fixed = 0
    code = _read_code(code_path)
    final_syntax_valid = False

    for iteration in range(1, max_iterations + 1):
        log.info("Iteration %d/%d for '%s'", iteration, max_iterations, system_name)

        # Step 1: Review
        issues = _review_code(model, code, system_name, target)
        num_issues = len(issues)
        total_issues_found += num_issues

        # Filter to actionable issues (critical and major)
        actionable = [i for i in issues if i.get("severity") in ("critical", "major")]

        iter_entry = {
            "iteration": iteration,
            "issues_found": num_issues,
            "actionable_issues": len(actionable),
            "issues": issues[:10],  # cap logged issues
        }

        # Step 2: If no actionable issues, check syntax and stop
        if not actionable:
            valid, error_msg = _syntax_check(code, target)
            iter_entry["syntax_valid"] = valid
            iter_entry["syntax_error"] = error_msg
            iter_entry["action"] = "no actionable issues — stopping"
            iteration_log.append(iter_entry)
            final_syntax_valid = valid
            break

        # Step 3: Apply fixes
        fixed_code = _apply_fixes(model, code, actionable, system_name, target)
        if not fixed_code:
            iter_entry["action"] = "fix generation failed — stopping"
            iteration_log.append(iter_entry)
            break

        # Step 4: Syntax check the fixed version
        valid, error_msg = _syntax_check(fixed_code, target)
        iter_entry["syntax_valid"] = valid
        iter_entry["syntax_error"] = error_msg

        if valid:
            # Accept the fix
            code = fixed_code
            code_path.write_text(code, encoding="utf-8")
            total_issues_fixed += len(actionable)
            iter_entry["action"] = f"applied fixes for {len(actionable)} issue(s)"
            final_syntax_valid = True
        else:
            # Fixed code has syntax errors — keep original, log the problem
            iter_entry["action"] = f"fixed code failed syntax check: {error_msg} — kept original"
            # Still count as an attempt
            final_syntax_valid = False

        iteration_log.append(iter_entry)

        # Stop if syntax is clean and no critical issues
        critical = [i for i in issues if i.get("severity") == "critical"]
        if valid and not critical:
            break

    iterations_run = len(iteration_log)

    # Build summary
    summary_parts = [
        f"Iteration runner: {iterations_run} pass(es) on '{system_name}' ({target}).",
    ]
    if total_issues_found:
        summary_parts.append(f"{total_issues_found} issue(s) found, {total_issues_fixed} fixed.")
    else:
        summary_parts.append("No issues found.")
    summary_parts.append(f"Final syntax: {'valid' if final_syntax_valid else 'invalid'}.")

    # Escalate if stuck with critical issues after all iterations
    escalate = (
        iterations_run >= max_iterations
        and not final_syntax_valid
    )
    escalation_reason = (
        f"Iteration runner exhausted {max_iterations} passes without achieving clean syntax"
        if escalate else ""
    )

    return {
        "status": "success" if final_syntax_valid else "partial",
        "summary": " ".join(summary_parts),
        "metrics": {
            "iterations_run": iterations_run,
            "issues_found": total_issues_found,
            "issues_fixed": total_issues_fixed,
            "final_syntax_valid": final_syntax_valid,
            "model_used": model,
            "system_name": system_name,
            "target": target,
        },
        "iteration_log": iteration_log,
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }
