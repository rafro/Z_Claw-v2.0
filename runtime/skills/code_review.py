"""
code-review skill — Reviews generated game code for bugs, architecture, and best practices.
Reads code from state/gamedev/project/ and uses Coder model for analysis.
Tier 2 preferred (Coder 14B).
"""

import json
import logging
import os
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_CODER_14B, MODEL_CODER_7B, STATE_DIR
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"

REVIEW_TYPES = {"full", "security", "performance", "architecture"}


def _pick_model() -> str | None:
    """Return the best available Coder model, or None."""
    if is_available(MODEL_CODER_14B, host=OLLAMA_HOST):
        return MODEL_CODER_14B
    if is_available(MODEL_CODER_7B, host=OLLAMA_HOST):
        return MODEL_CODER_7B
    return None


def _find_most_recent_file() -> Path | None:
    """Find the most recently modified source file in the project directory."""
    if not PROJECT_DIR.exists():
        return None
    candidates = []
    for ext in ("*.gd", "*.py", "*.json"):
        for p in PROJECT_DIR.rglob(ext):
            # Skip test files, manifest, and __pycache__
            if "tests" in p.parts or "__pycache__" in p.parts or p.name == "manifest.json":
                continue
            candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _build_system_prompt(review_type: str) -> str:
    """Build review-focused system prompt."""
    base = (
        "You are a senior game code reviewer. Analyze the provided code and report "
        "issues in a structured format. For each issue provide:\n"
        "- SEVERITY: critical / high / medium / low\n"
        "- LINE: approximate line number or range\n"
        "- ISSUE: concise description of the problem\n"
        "- FIX: suggested fix or improvement\n\n"
        "After all issues, provide a brief SUMMARY with overall code quality assessment."
    )
    focus_map = {
        "full": (
            "\n\nPerform a full review covering: bugs, logic errors, architecture, "
            "error handling, performance, naming, style, and best practices."
        ),
        "security": (
            "\n\nFocus on security concerns: input validation, injection risks, "
            "file path traversal, unsafe deserialization, hardcoded secrets, "
            "and trust boundaries."
        ),
        "performance": (
            "\n\nFocus on performance: unnecessary allocations in hot loops, "
            "O(n^2) algorithms, redundant computations, memory leaks, "
            "missing object pooling, and frame-budget concerns."
        ),
        "architecture": (
            "\n\nFocus on architecture: coupling between systems, SOLID violations, "
            "god objects, missing abstractions, signal/event misuse, "
            "and scalability concerns."
        ),
    }
    return base + focus_map.get(review_type, focus_map["full"])


def _parse_findings(response: str) -> list[dict]:
    """
    Best-effort parse of the LLM review output into structured findings.
    Falls back to a single finding containing the full response if parsing fails.
    """
    findings = []
    current = {}
    severity_keywords = {"critical", "high", "medium", "low"}

    for line in response.splitlines():
        stripped = line.strip()
        upper = stripped.upper()

        if upper.startswith("SEVERITY:") or upper.startswith("- SEVERITY:"):
            # Start a new finding
            if current.get("issue"):
                findings.append(current)
            sev_text = stripped.split(":", 1)[1].strip().lower()
            # Extract just the severity keyword
            sev = "medium"
            for kw in severity_keywords:
                if kw in sev_text:
                    sev = kw
                    break
            current = {"severity": sev, "line": "", "issue": "", "fix": ""}

        elif upper.startswith("LINE:") or upper.startswith("- LINE:"):
            if current is not None:
                current["line"] = stripped.split(":", 1)[1].strip()

        elif upper.startswith("ISSUE:") or upper.startswith("- ISSUE:"):
            if current is not None:
                current["issue"] = stripped.split(":", 1)[1].strip()

        elif upper.startswith("FIX:") or upper.startswith("- FIX:"):
            if current is not None:
                current["fix"] = stripped.split(":", 1)[1].strip()

    # Don't forget the last finding
    if current.get("issue"):
        findings.append(current)

    # If parsing found nothing, wrap the whole response as a single finding
    if not findings and response.strip():
        findings.append({
            "severity": "medium",
            "line": "N/A",
            "issue": response[:500],
            "fix": "",
        })

    return findings


def run(**kwargs) -> dict:
    """
    Review generated game code for quality, bugs, and architecture.

    kwargs:
        file_path (str):    Path relative to project dir (e.g. "godot/player_controller.gd").
        target (str):       Target engine hint for context.
        review_type (str):  "full" | "security" | "performance" | "architecture". Default "full".
    """
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)

    file_path = kwargs.get("file_path", "")
    target = kwargs.get("target", "")
    review_type = kwargs.get("review_type", "full")

    if review_type not in REVIEW_TYPES:
        review_type = "full"

    # Resolve the file to review
    if file_path:
        code_file = PROJECT_DIR / file_path
    else:
        code_file = _find_most_recent_file()

    if not code_file or not code_file.exists():
        return {
            "status": "partial",
            "summary": "No code file found to review. Generate code first or specify file_path.",
            "findings": [],
            "metrics": {
                "issues_found": 0,
                "severity_breakdown": {},
                "file_reviewed": "",
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Read the code
    try:
        code_content = code_file.read_text(encoding="utf-8")
    except Exception as e:
        log.error("Failed to read code file %s: %s", code_file, e)
        return {
            "status": "failed",
            "summary": f"Failed to read code file: {e}",
            "findings": [],
            "metrics": {"file_reviewed": str(code_file)},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    if not code_content.strip():
        return {
            "status": "partial",
            "summary": f"Code file '{code_file.name}' is empty.",
            "findings": [],
            "metrics": {"file_reviewed": code_file.name, "issues_found": 0},
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Infer target from file extension if not provided
    if not target:
        if code_file.suffix == ".gd":
            target = "godot"
        elif code_file.suffix == ".py":
            target = "pygame"
        else:
            target = "generic"

    model = _pick_model()
    if not model:
        return {
            "status": "degraded",
            "summary": f"Code review for '{code_file.name}' — no Coder model available.",
            "findings": [],
            "metrics": {
                "file_reviewed": code_file.name,
                "issues_found": 0,
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Truncate very large files for the LLM context window
    max_chars = 6000
    code_for_review = code_content
    if len(code_content) > max_chars:
        code_for_review = code_content[:max_chars] + "\n\n... (file truncated for review)"

    system_prompt = _build_system_prompt(review_type)
    user_prompt = (
        f"Review the following {target} game code ({review_type} review).\n"
        f"File: {code_file.name}\n"
        f"Lines: {len(code_content.splitlines())}\n\n"
        f"```\n{code_for_review}\n```"
    )

    try:
        response = chat(model, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.1, max_tokens=1500, task_type="code-review")
    except Exception as e:
        log.error("code-review LLM call failed: %s", e)
        return {
            "status": "failed",
            "summary": f"Code review LLM call failed: {e}",
            "findings": [],
            "metrics": {
                "file_reviewed": code_file.name,
                "model_available": True,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Parse the response into structured findings
    findings = _parse_findings(response)

    # Build severity breakdown
    severity_breakdown = {}
    for f in findings:
        sev = f.get("severity", "medium")
        severity_breakdown[sev] = severity_breakdown.get(sev, 0) + 1

    critical_count = severity_breakdown.get("critical", 0)
    high_count = severity_breakdown.get("high", 0)

    # Build summary
    relative_path = str(code_file.relative_to(PROJECT_DIR))
    summary_parts = [
        f"Reviewed '{relative_path}' ({review_type}): {len(findings)} issue(s) found.",
    ]
    if critical_count:
        summary_parts.append(f"{critical_count} critical.")
    if high_count:
        summary_parts.append(f"{high_count} high.")

    # Escalate if too many critical issues
    escalate = critical_count >= 2
    escalation_reason = (
        f"{critical_count} critical issues found in {code_file.name}"
        if escalate else ""
    )

    return {
        "status": "success",
        "summary": " ".join(summary_parts),
        "findings": findings,
        "review_output": response,
        "metrics": {
            "issues_found": len(findings),
            "severity_breakdown": severity_breakdown,
            "file_reviewed": relative_path,
            "review_type": review_type,
            "lines_reviewed": len(code_content.splitlines()),
            "model_used": model,
            "model_available": True,
        },
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }
