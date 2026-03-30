"""
refine-loop skill — Multi-source feedback aggregation and iterative fixing.
Reads ALL feedback: code-review, code-test, game-runner, visual-qa, balance-audit, playtest-report.
Prioritizes fixes, applies them via LLM, re-tests, and iterates until quality threshold met.
Tier 2 (Coder 14B for fix generation).
"""

import ast
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.config import OLLAMA_HOST, MODEL_CODER_14B, MODEL_CODER_7B, STATE_DIR
from runtime.ollama_client import chat, is_available
from runtime import packet

log = logging.getLogger(__name__)

GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"
REFINE_LOG_FILE = GAMEDEV_DIR / "refine-log.jsonl"

# Maximum age (minutes) for feedback packets to be considered fresh
_FEEDBACK_MAX_AGE = 4320  # 3 days
_BALANCE_MAX_AGE = 1440   # 1 day

# All feedback sources and their packet skill names
_FEEDBACK_SOURCES = [
    ("code-review", _FEEDBACK_MAX_AGE),
    ("code-test", _FEEDBACK_MAX_AGE),
    ("game-runner", _FEEDBACK_MAX_AGE),
    ("visual-qa", _FEEDBACK_MAX_AGE),
    ("balance-audit", _BALANCE_MAX_AGE),
    ("playtest-report", _FEEDBACK_MAX_AGE),
]

TARGET_EXTENSIONS = {
    "godot": ".gd",
    "pygame": ".py",
    "generic": ".py",
}

# Priority order for issue triage.  Lower index = higher priority.
# (source, severity_key, weight_for_quality_score)
PRIORITY_ORDER = [
    ("game-runner", "crash", 0.3),
    ("code-test", "failure", 0.4),
    ("code-review", "critical", 0.2),
    ("visual-qa", "issue", 0.1),
    ("balance-audit", "high", 0.0),
    ("playtest-report", "critical", 0.0),
]

# ── Helpers ──────────────────────────────────────────────────────────────────


def _pick_model() -> str | None:
    """Return the best available Coder model, or None."""
    if is_available(MODEL_CODER_14B, host=OLLAMA_HOST):
        return MODEL_CODER_14B
    if is_available(MODEL_CODER_7B, host=OLLAMA_HOST):
        return MODEL_CODER_7B
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_refine_log(entry: dict) -> None:
    """Append a single JSON line to the persistent refine-log."""
    try:
        REFINE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(REFINE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("Failed to write refine-log: %s", exc)


# ── Feedback reading ─────────────────────────────────────────────────────────


def _read_all_feedback() -> dict[str, dict | None]:
    """Read fresh packets from every feedback source. Returns source->packet map."""
    out: dict[str, dict | None] = {}
    for skill_name, max_age in _FEEDBACK_SOURCES:
        try:
            pkt = packet.read_fresh("gamedev", skill_name, max_age)
        except Exception as exc:
            log.debug("Could not read packet for %s: %s", skill_name, exc)
            pkt = None
        out[skill_name] = pkt
    return out


# ── Issue extraction ─────────────────────────────────────────────────────────


def _extract_code_review_issues(pkt: dict) -> list[dict]:
    """Extract actionable issues from a code-review packet."""
    issues: list[dict] = []
    for finding in pkt.get("findings", []):
        severity = finding.get("severity", "medium").lower()
        if severity in ("critical", "high"):
            issues.append({
                "source": "code-review",
                "severity": severity,
                "description": finding.get("issue", finding.get("description", "")),
                "fix_hint": finding.get("fix", ""),
                "line": finding.get("line", ""),
                "file_path": pkt.get("metrics", {}).get("file_reviewed", ""),
            })
    return issues


def _extract_code_test_issues(pkt: dict) -> list[dict]:
    """Extract test failure issues from a code-test packet."""
    issues: list[dict] = []
    metrics = pkt.get("metrics", {})
    failed = metrics.get("tests_failed", 0)
    if failed > 0:
        # Parse test_output for individual failures if available
        test_output = pkt.get("test_output", "")
        failure_lines = [
            line.strip() for line in test_output.splitlines()
            if "FAILED" in line or "ERROR" in line or "assert" in line.lower()
        ]
        if failure_lines:
            for fl in failure_lines[:10]:  # cap at 10
                issues.append({
                    "source": "code-test",
                    "severity": "failure",
                    "description": fl,
                    "fix_hint": "",
                    "line": "",
                    "file_path": "",
                })
        else:
            # Generic failure entry
            issues.append({
                "source": "code-test",
                "severity": "failure",
                "description": f"{failed} test(s) failed. Output: {test_output[:300]}",
                "fix_hint": "",
                "line": "",
                "file_path": "",
            })
    return issues


def _extract_game_runner_issues(pkt: dict) -> list[dict]:
    """Extract crash/error issues from a game-runner packet."""
    issues: list[dict] = []
    for error in pkt.get("errors", []):
        issues.append({
            "source": "game-runner",
            "severity": "crash",
            "description": f"{error.get('type', 'Error')}: {error.get('message', '')}",
            "fix_hint": "",
            "line": "",
            "file_path": "",
            "traceback": error.get("traceback", ""),
        })
    # Also flag if crash_detected but no structured errors parsed
    metrics = pkt.get("metrics", {})
    if metrics.get("crash_detected") and not issues:
        issues.append({
            "source": "game-runner",
            "severity": "crash",
            "description": pkt.get("summary", "Game crashed with no structured error info."),
            "fix_hint": "",
            "line": "",
            "file_path": "",
        })
    return issues


def _extract_visual_qa_issues(pkt: dict) -> list[dict]:
    """Extract visual-qa findings."""
    issues: list[dict] = []
    for finding in pkt.get("findings", pkt.get("issues", [])):
        if isinstance(finding, dict):
            issues.append({
                "source": "visual-qa",
                "severity": "issue",
                "description": finding.get("description", finding.get("issue", str(finding))),
                "fix_hint": finding.get("fix", finding.get("suggestion", "")),
                "line": "",
                "file_path": finding.get("file_path", ""),
            })
        elif isinstance(finding, str):
            issues.append({
                "source": "visual-qa",
                "severity": "issue",
                "description": finding,
                "fix_hint": "",
                "line": "",
                "file_path": "",
            })
    return issues


def _extract_balance_audit_issues(pkt: dict) -> list[dict]:
    """Extract balance-audit findings with high severity."""
    issues: list[dict] = []
    for finding in pkt.get("findings", pkt.get("issues", [])):
        if isinstance(finding, dict):
            sev = finding.get("severity", "medium").lower()
            if sev in ("high", "critical"):
                issues.append({
                    "source": "balance-audit",
                    "severity": sev,
                    "description": finding.get("description", finding.get("issue", str(finding))),
                    "fix_hint": finding.get("fix", finding.get("recommendation", "")),
                    "line": "",
                    "file_path": finding.get("file_path", ""),
                })
    return issues


def _extract_playtest_report_issues(pkt: dict) -> list[dict]:
    """Extract critical issues from playtest reports."""
    issues: list[dict] = []
    for finding in pkt.get("findings", pkt.get("issues", pkt.get("critical_issues", []))):
        if isinstance(finding, dict):
            sev = finding.get("severity", "medium").lower()
            if sev in ("critical", "high"):
                issues.append({
                    "source": "playtest-report",
                    "severity": sev,
                    "description": finding.get("description", finding.get("issue", str(finding))),
                    "fix_hint": finding.get("fix", finding.get("suggestion", "")),
                    "line": "",
                    "file_path": finding.get("file_path", ""),
                })
        elif isinstance(finding, str):
            issues.append({
                "source": "playtest-report",
                "severity": "critical",
                "description": finding,
                "fix_hint": "",
                "line": "",
                "file_path": "",
            })
    return issues


_EXTRACTORS = {
    "code-review": _extract_code_review_issues,
    "code-test": _extract_code_test_issues,
    "game-runner": _extract_game_runner_issues,
    "visual-qa": _extract_visual_qa_issues,
    "balance-audit": _extract_balance_audit_issues,
    "playtest-report": _extract_playtest_report_issues,
}


def _extract_all_issues(feedback: dict[str, dict | None]) -> list[dict]:
    """Run every extractor on its corresponding packet."""
    all_issues: list[dict] = []
    for source, pkt in feedback.items():
        if pkt is None:
            continue
        extractor = _EXTRACTORS.get(source)
        if extractor is None:
            continue
        try:
            all_issues.extend(extractor(pkt))
        except Exception as exc:
            log.warning("Issue extraction failed for %s: %s", source, exc)
    return all_issues


# ── Prioritization ───────────────────────────────────────────────────────────

# Map (source, severity) -> numeric priority (lower = more urgent)
_PRIORITY_RANK: dict[tuple[str, str], int] = {}
for _idx, (src, sev, _w) in enumerate(PRIORITY_ORDER):
    _PRIORITY_RANK[(src, sev)] = _idx


def _issue_priority(issue: dict) -> int:
    """Return a numeric priority for sorting (lower = more urgent)."""
    key = (issue.get("source", ""), issue.get("severity", ""))
    return _PRIORITY_RANK.get(key, 100)


def _prioritize(issues: list[dict], max_per_round: int = 5) -> list[dict]:
    """Sort issues by priority and return top N."""
    ranked = sorted(issues, key=_issue_priority)
    return ranked[:max_per_round]


# ── Quality scoring ──────────────────────────────────────────────────────────


def _aggregate_feedback_metrics(feedback: dict[str, dict | None]) -> dict:
    """Build a normalized metrics dict from all available feedback packets."""
    metrics: dict[str, Any] = {
        "tests_pass_rate": 0.0,
        "crashes": False,
        "critical_issues": 0,
        "total_checks": 0,
        "visual_score": 1.0,  # default to 1.0 if visual-qa not run
    }

    # ── code-test ────────────────────────────────────────────────────────
    test_pkt = feedback.get("code-test")
    if test_pkt and test_pkt.get("status") != "partial":
        test_metrics = test_pkt.get("metrics", {})
        passed = test_metrics.get("tests_passed", 0)
        failed = test_metrics.get("tests_failed", 0)
        total = passed + failed
        metrics["tests_pass_rate"] = passed / max(total, 1)

    # ── game-runner ──────────────────────────────────────────────────────
    runner_pkt = feedback.get("game-runner")
    if runner_pkt:
        runner_metrics = runner_pkt.get("metrics", {})
        metrics["crashes"] = runner_metrics.get("crash_detected", False)

    # ── code-review ──────────────────────────────────────────────────────
    review_pkt = feedback.get("code-review")
    if review_pkt:
        review_metrics = review_pkt.get("metrics", {})
        breakdown = review_metrics.get("severity_breakdown", {})
        metrics["critical_issues"] = breakdown.get("critical", 0) + breakdown.get("high", 0)
        metrics["total_checks"] = review_metrics.get("issues_found", 0) or 1

    # ── visual-qa ────────────────────────────────────────────────────────
    visual_pkt = feedback.get("visual-qa")
    if visual_pkt:
        vis_metrics = visual_pkt.get("metrics", {})
        # Accept either a pre-computed score or derive from findings count
        if "visual_score" in vis_metrics:
            metrics["visual_score"] = float(vis_metrics["visual_score"])
        else:
            findings_count = len(visual_pkt.get("findings", visual_pkt.get("issues", [])))
            # Each finding deducts 0.2, floor at 0.0
            metrics["visual_score"] = max(0.0, 1.0 - findings_count * 0.2)

    return metrics


def compute_quality_score(feedback_metrics: dict) -> float:
    """
    Weighted quality score from 0.0 to 1.0.
    Weights:  40% tests_pass_rate  |  30% no_crashes  |  20% review_clean  |  10% visual_score
    """
    tests_pass = float(feedback_metrics.get("tests_pass_rate", 0.0))
    no_crashes = 1.0 if not feedback_metrics.get("crashes") else 0.0
    critical = feedback_metrics.get("critical_issues", 0)
    total_checks = max(feedback_metrics.get("total_checks", 1), 1)
    review_clean = 1.0 - min(critical / total_checks, 1.0)
    visual_score = float(feedback_metrics.get("visual_score", 1.0))

    return 0.4 * tests_pass + 0.3 * no_crashes + 0.2 * review_clean + 0.1 * visual_score


# ── Code file resolution ─────────────────────────────────────────────────────


def _find_code_file(file_path: str, target: str) -> Path | None:
    """Resolve an issue's file_path to an actual Path in the project."""
    if not file_path:
        return None
    # Try as-is relative to PROJECT_DIR
    candidate = PROJECT_DIR / file_path
    if candidate.exists():
        return candidate
    # Try under target subdir
    candidate = PROJECT_DIR / target / file_path
    if candidate.exists():
        return candidate
    # Try just the basename under target subdir
    basename = Path(file_path).name
    target_dir = PROJECT_DIR / target
    if target_dir.exists():
        for match in target_dir.rglob(basename):
            return match
    return None


def _find_all_source_files(target: str) -> list[Path]:
    """List all source files for the given target."""
    target_dir = PROJECT_DIR / target
    if not target_dir.exists():
        return []
    ext = TARGET_EXTENSIONS.get(target, ".py")
    files = []
    for p in target_dir.rglob(f"*{ext}"):
        if "tests" not in p.parts and "__pycache__" not in p.parts:
            files.append(p)
    return sorted(files, key=lambda p: p.name)


def _read_code(path: Path) -> str:
    """Read a source code file, returning empty string on failure."""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("Failed to read %s: %s", path, exc)
        return ""


def _write_code(path: Path, code: str) -> bool:
    """Write code back to file. Returns True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        return True
    except Exception as exc:
        log.warning("Failed to write %s: %s", path, exc)
        return False


# ── Syntax validation ────────────────────────────────────────────────────────


def _syntax_check_python(code: str) -> tuple[bool, str]:
    """Verify Python code parses successfully."""
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as exc:
        return False, f"SyntaxError at line {exc.lineno}: {exc.msg}"


def _syntax_check(code: str, target: str) -> tuple[bool, str]:
    """Route syntax checking based on target."""
    if target in ("pygame", "generic"):
        return _syntax_check_python(code)
    # Godot / other — skip syntax validation (no parser available)
    return True, ""


# ── LLM fix generation ──────────────────────────────────────────────────────


def _strip_markdown_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences from LLM output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # Drop the opening fence line
        lines = lines[1:]
        # Drop the closing fence line if present
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped


def _build_fix_prompt(code: str, issues: list[dict], system_name: str, target: str) -> tuple[str, str]:
    """Build system + user prompts for fix generation."""
    lang = "GDScript" if target == "godot" else "Python"
    system_prompt = (
        f"You are an expert {lang} game programmer performing targeted bug fixes. "
        "You will be given source code and a list of issues to fix. "
        "Apply ONLY the requested fixes — do not refactor, rename, or restructure "
        "anything beyond what is necessary to resolve the listed issues.\n\n"
        "Output ONLY the complete fixed source code — no explanations, no markdown fences, "
        "no commentary before or after the code."
    )

    issues_text = "\n".join(
        f"- [{iss.get('source', '?')}/{iss.get('severity', '?')}] "
        f"Line {iss.get('line', '?')}: {iss.get('description', '?')}"
        + (f"\n  Hint: {iss['fix_hint']}" if iss.get("fix_hint") else "")
        + (f"\n  Traceback: {iss['traceback'][:300]}" if iss.get("traceback") else "")
        for iss in issues
    )

    # Cap code at 5000 chars to stay within context window
    code_for_llm = code[:5000]
    if len(code) > 5000:
        code_for_llm += "\n# ... (file truncated — focus fixes on the shown portion)"

    user_prompt = (
        f"System/module: {system_name or 'unknown'}\n\n"
        f"Issues to fix ({len(issues)}):\n{issues_text}\n\n"
        f"Current code:\n```\n{code_for_llm}\n```"
    )

    return system_prompt, user_prompt


def _llm_fix(model: str, code: str, issues: list[dict], system_name: str, target: str) -> str:
    """Send code + issues to LLM and return fixed code. Returns empty string on failure."""
    system_prompt, user_prompt = _build_fix_prompt(code, issues, system_name, target)

    try:
        response = chat(
            model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=3000,
            task_type="refine-loop",
        )
        return _strip_markdown_fences(response)
    except Exception as exc:
        log.warning("Refine-loop LLM fix call failed: %s", exc)
        return ""


# ── Sub-skill re-runners ─────────────────────────────────────────────────────


def _rerun_code_test(target: str) -> dict | None:
    """Re-run the code-test skill and return its result dict."""
    try:
        from runtime.skills.code_test import run as code_test_run
        return code_test_run(action="run", target=target)
    except Exception as exc:
        log.warning("Failed to re-run code-test: %s", exc)
        return None


def _rerun_game_runner(target: str) -> dict | None:
    """Re-run the game-runner skill and return its result dict."""
    try:
        from runtime.skills.game_runner import run as game_runner_run
        return game_runner_run(target=target, headless=True, timeout=30)
    except Exception as exc:
        log.warning("Failed to re-run game-runner: %s", exc)
        return None


# ── Main entry point ─────────────────────────────────────────────────────────


def run(**kwargs) -> dict:
    """
    Multi-source feedback aggregation and iterative fixing.

    Reads feedback from all sources (code-review, code-test, game-runner,
    visual-qa, balance-audit, playtest-report), prioritizes issues, applies
    LLM-generated fixes, re-tests, and iterates until quality threshold is met.

    kwargs:
        target (str):             Target engine — "pygame", "godot", or "generic".
                                  Default "pygame".
        max_iterations (int):     Maximum refinement iterations. Default 3.
        quality_threshold (float): Stop when quality score >= this value (0.0–1.0).
                                  Default 0.7.
        system_name (str):        Optional — focus fixes on a specific system/module.
    """
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    GAMEDEV_DIR.mkdir(parents=True, exist_ok=True)

    target: str = kwargs.get("target", "pygame")
    max_iterations: int = int(kwargs.get("max_iterations", 3))
    quality_threshold: float = float(kwargs.get("quality_threshold", 0.7))
    system_name: str = kwargs.get("system_name", "")

    if target not in TARGET_EXTENSIONS:
        target = "pygame"

    # ── Pre-flight: model availability ───────────────────────────────────
    model = _pick_model()
    if not model:
        return {
            "status": "degraded",
            "summary": (
                "Refine loop cannot run — no Coder model available. "
                "Ensure Ollama is running with a Coder model loaded."
            ),
            "metrics": {
                "iterations_run": 0,
                "initial_score": 0.0,
                "final_score": 0.0,
                "issues_found": 0,
                "issues_fixed": 0,
                "quality_threshold": quality_threshold,
                "threshold_met": False,
            },
            "iteration_log": [],
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # ── Pre-flight: source files exist ───────────────────────────────────
    source_files = _find_all_source_files(target)
    if not source_files:
        return {
            "status": "partial",
            "summary": (
                f"Refine loop: no source files found for target '{target}'. "
                "Generate code first."
            ),
            "metrics": {
                "iterations_run": 0,
                "initial_score": 0.0,
                "final_score": 0.0,
                "issues_found": 0,
                "issues_fixed": 0,
                "quality_threshold": quality_threshold,
                "threshold_met": False,
            },
            "iteration_log": [],
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # ── Iteration loop ───────────────────────────────────────────────────
    iteration_log: list[dict] = []
    total_issues_found = 0
    total_issues_fixed = 0
    initial_score: float | None = None
    final_score: float = 0.0
    threshold_met = False

    for iteration in range(1, max_iterations + 1):
        iter_start = time.monotonic()
        log.info("Refine-loop iteration %d/%d (target=%s)", iteration, max_iterations, target)

        # ── Step 1: Aggregate all feedback ───────────────────────────────
        feedback = _read_all_feedback()
        sources_available = [src for src, pkt in feedback.items() if pkt is not None]

        # ── Step 2: Compute quality score ────────────────────────────────
        feedback_metrics = _aggregate_feedback_metrics(feedback)
        score = compute_quality_score(feedback_metrics)

        if initial_score is None:
            initial_score = score
        final_score = score

        log.info(
            "Refine-loop iteration %d: quality=%.2f (threshold=%.2f), sources=%s",
            iteration, score, quality_threshold, sources_available,
        )

        # ── Step 3: Check threshold ──────────────────────────────────────
        if score >= quality_threshold:
            threshold_met = True
            iteration_log.append({
                "iteration": iteration,
                "score": round(score, 3),
                "threshold_met": True,
                "sources_available": sources_available,
                "issues_found": 0,
                "issues_fixed": 0,
                "action": "quality threshold met — stopping",
                "duration_s": round(time.monotonic() - iter_start, 2),
            })
            log.info("Refine-loop: quality %.2f >= threshold %.2f — stopping.", score, quality_threshold)
            break

        # ── Step 4: Extract and prioritize issues ────────────────────────
        all_issues = _extract_all_issues(feedback)

        # If system_name is specified, prefer issues related to that system
        if system_name:
            system_issues = [
                iss for iss in all_issues
                if system_name.lower() in (iss.get("file_path", "") + iss.get("description", "")).lower()
            ]
            # If we found system-specific issues, prioritize those; otherwise use all
            if system_issues:
                all_issues = system_issues + [
                    iss for iss in all_issues if iss not in system_issues
                ]

        total_issues_found += len(all_issues)
        top_issues = _prioritize(all_issues, max_per_round=5)

        if not top_issues:
            # No actionable issues despite low score — nothing to fix
            iteration_log.append({
                "iteration": iteration,
                "score": round(score, 3),
                "threshold_met": False,
                "sources_available": sources_available,
                "issues_found": 0,
                "issues_fixed": 0,
                "action": "no actionable issues extracted — stopping",
                "duration_s": round(time.monotonic() - iter_start, 2),
            })
            log.info("Refine-loop: no actionable issues found at iteration %d.", iteration)
            break

        # ── Step 5: Group issues by file and apply fixes ─────────────────
        issues_by_file: dict[str, list[dict]] = {}
        unresolved_issues: list[dict] = []

        for issue in top_issues:
            file_path_str = issue.get("file_path", "")
            resolved_path = _find_code_file(file_path_str, target)

            if resolved_path is None and file_path_str:
                # Could not locate the file — try a broader search
                resolved_path = _find_code_file(Path(file_path_str).name, target)

            if resolved_path is None:
                # If we still can't find the file, try the first source file
                # (common when feedback doesn't include file paths)
                if source_files:
                    resolved_path = source_files[0]
                else:
                    unresolved_issues.append(issue)
                    continue

            key = str(resolved_path)
            issues_by_file.setdefault(key, []).append(issue)

        fixes_applied = 0
        fixes_failed = 0
        fix_details: list[dict] = []

        for file_path_str, file_issues in issues_by_file.items():
            code_path = Path(file_path_str)
            original_code = _read_code(code_path)
            if not original_code:
                log.warning("Refine-loop: empty or unreadable file %s — skipping.", code_path)
                fixes_failed += len(file_issues)
                continue

            # Generate fix via LLM
            fixed_code = _llm_fix(model, original_code, file_issues, system_name, target)
            if not fixed_code:
                log.warning("Refine-loop: LLM returned empty fix for %s.", code_path)
                fixes_failed += len(file_issues)
                fix_details.append({
                    "file": str(code_path.relative_to(PROJECT_DIR)) if code_path.is_relative_to(PROJECT_DIR) else str(code_path),
                    "issues_count": len(file_issues),
                    "result": "llm_empty",
                })
                continue

            # Don't accept a fix that is identical to the original
            if fixed_code.strip() == original_code.strip():
                log.info("Refine-loop: LLM returned identical code for %s — no fix applied.", code_path)
                fixes_failed += len(file_issues)
                fix_details.append({
                    "file": str(code_path.relative_to(PROJECT_DIR)) if code_path.is_relative_to(PROJECT_DIR) else str(code_path),
                    "issues_count": len(file_issues),
                    "result": "no_change",
                })
                continue

            # Syntax check before accepting
            valid, syntax_error = _syntax_check(fixed_code, target)
            if not valid:
                log.warning(
                    "Refine-loop: fixed code for %s failed syntax check: %s — keeping original.",
                    code_path, syntax_error,
                )
                fixes_failed += len(file_issues)
                fix_details.append({
                    "file": str(code_path.relative_to(PROJECT_DIR)) if code_path.is_relative_to(PROJECT_DIR) else str(code_path),
                    "issues_count": len(file_issues),
                    "result": "syntax_error",
                    "syntax_error": syntax_error,
                })
                continue

            # Accept the fix
            if _write_code(code_path, fixed_code):
                fixes_applied += len(file_issues)
                rel_path = str(code_path.relative_to(PROJECT_DIR)) if code_path.is_relative_to(PROJECT_DIR) else str(code_path)
                fix_details.append({
                    "file": rel_path,
                    "issues_count": len(file_issues),
                    "result": "applied",
                })
                log.info("Refine-loop: applied fix to %s (%d issue(s)).", rel_path, len(file_issues))
            else:
                fixes_failed += len(file_issues)
                fix_details.append({
                    "file": str(code_path),
                    "issues_count": len(file_issues),
                    "result": "write_failed",
                })

        total_issues_fixed += fixes_applied

        # ── Step 6: Re-run affected checks ───────────────────────────────
        rerun_results: dict[str, str] = {}

        had_test_failures = any(
            iss.get("source") == "code-test" for iss in top_issues
        )
        had_crashes = any(
            iss.get("source") == "game-runner" for iss in top_issues
        )

        if had_test_failures and fixes_applied > 0:
            log.info("Refine-loop: re-running code-test after fixes.")
            test_result = _rerun_code_test(target)
            if test_result:
                rerun_results["code-test"] = test_result.get("status", "unknown")
                # Update feedback map with fresh test results for next iteration scoring
                feedback["code-test"] = test_result

        if had_crashes and fixes_applied > 0:
            log.info("Refine-loop: re-running game-runner after fixes.")
            runner_result = _rerun_game_runner(target)
            if runner_result:
                rerun_results["game-runner"] = runner_result.get("status", "unknown")
                feedback["game-runner"] = runner_result

        # Re-compute score after re-runs
        if rerun_results:
            feedback_metrics = _aggregate_feedback_metrics(feedback)
            post_fix_score = compute_quality_score(feedback_metrics)
        else:
            post_fix_score = score

        final_score = post_fix_score

        iter_entry = {
            "iteration": iteration,
            "score_before": round(score, 3),
            "score_after": round(post_fix_score, 3),
            "threshold_met": post_fix_score >= quality_threshold,
            "sources_available": sources_available,
            "issues_found": len(all_issues),
            "issues_fixed": fixes_applied,
            "issues_failed": fixes_failed,
            "unresolved_issues": len(unresolved_issues),
            "fix_details": fix_details,
            "rerun_results": rerun_results,
            "action": (
                f"fixed {fixes_applied}, failed {fixes_failed}"
                + (f", re-ran {list(rerun_results.keys())}" if rerun_results else "")
            ),
            "duration_s": round(time.monotonic() - iter_start, 2),
        }
        iteration_log.append(iter_entry)

        # Persist iteration to log file
        _append_refine_log({
            "ts": _now_iso(),
            "target": target,
            "system_name": system_name,
            **iter_entry,
        })

        # Check if threshold met after re-runs
        if post_fix_score >= quality_threshold:
            threshold_met = True
            log.info(
                "Refine-loop: quality %.2f >= threshold %.2f after fixes — stopping.",
                post_fix_score, quality_threshold,
            )
            break

    # ── Build result ─────────────────────────────────────────────────────
    iterations_run = len(iteration_log)
    if initial_score is None:
        initial_score = 0.0

    # Determine status
    if threshold_met:
        status = "success"
    elif total_issues_fixed > 0:
        status = "partial"
    elif total_issues_found == 0:
        status = "success"  # nothing to fix
    else:
        status = "failed"

    # Build human-readable summary
    summary_parts = [
        f"Refine loop: {iterations_run} iteration(s),",
        f"quality {final_score:.2f} (threshold {quality_threshold:.1f}).",
    ]
    if total_issues_found:
        summary_parts.append(f"{total_issues_found} issue(s) found, {total_issues_fixed} fixed.")
    else:
        summary_parts.append("No issues found in available feedback.")
    summary = " ".join(summary_parts)

    # Escalation
    escalate = (
        not threshold_met
        and iterations_run >= max_iterations
        and total_issues_found > 0
    )
    escalation_reason = (
        f"Refine loop exhausted {max_iterations} iteration(s) without meeting "
        f"quality threshold ({final_score:.2f} < {quality_threshold:.1f}). "
        f"{total_issues_found - total_issues_fixed} issue(s) remain unresolved."
        if escalate else ""
    )

    # Action items for unresolved issues
    action_items: list[dict] = []
    if escalate:
        # Surface the top unresolved issues as action items
        remaining_issues = _extract_all_issues(_read_all_feedback())
        for iss in _prioritize(remaining_issues, max_per_round=3):
            action_items.append({
                "priority": "high",
                "description": (
                    f"[{iss.get('source', '?')}/{iss.get('severity', '?')}] "
                    f"{iss.get('description', 'Unknown issue')[:200]}"
                ),
                "requires_matthew": iss.get("source") in ("playtest-report", "balance-audit"),
            })

    return {
        "status": status,
        "summary": summary,
        "metrics": {
            "iterations_run": iterations_run,
            "initial_score": round(initial_score, 3),
            "final_score": round(final_score, 3),
            "issues_found": total_issues_found,
            "issues_fixed": total_issues_fixed,
            "quality_threshold": quality_threshold,
            "threshold_met": threshold_met,
            "model_used": model,
            "target": target,
            "system_name": system_name or None,
        },
        "iteration_log": iteration_log,
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": action_items,
    }
