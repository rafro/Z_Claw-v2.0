"""
auto-fix skill — Reads scan findings, generates fixes, applies them to source.
Connects dev-automation analysis (refactor-scan, debug-agent) to actual code changes.
Safety rails: only runtime/ files, max 50 lines per change, must pass tests,
.bak backup, capped at 3 fixes per run, all disabled by default.
Tier 2 (Coder 14B for fix generation).
"""

import ast
import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path

from runtime.config import (
    AUTO_FIX_ENABLED,
    MODEL_CODER_14B,
    MODEL_CODER_7B,
    OLLAMA_HOST,
    ROOT,
    STATE_DIR,
)
from runtime.ollama_client import chat, is_available
from runtime import packet

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------

MAX_LINES_PER_CHANGE = 50     # reject any fix that touches more than this
MAX_SOURCE_LINES     = 500    # skip files larger than this
ALLOWED_ROOT         = ROOT / "runtime"
LOG_FILE             = STATE_DIR / "dev" / "auto-fix-log.jsonl"

# Files that must never be modified, even if they live under runtime/
DENY_LIST = {
    "config.py",
    "__init__.py",
}

# Severity levels considered actionable
ACTIONABLE_SEVERITIES = {"high", "critical"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_model() -> tuple[str | None, str]:
    """Return (model_name, host) for the best available coder model, or (None, "")."""
    if is_available(MODEL_CODER_14B, host=OLLAMA_HOST):
        return MODEL_CODER_14B, OLLAMA_HOST
    if is_available(MODEL_CODER_7B, host=OLLAMA_HOST):
        return MODEL_CODER_7B, OLLAMA_HOST
    return None, ""


def _is_safe_path(file_path: Path) -> bool:
    """Return True only if the file lives under runtime/ and is not deny-listed."""
    try:
        file_path.resolve().relative_to(ALLOWED_ROOT.resolve())
    except ValueError:
        return False
    if file_path.name in DENY_LIST:
        return False
    # Extra guard: never touch dotfiles or non-Python
    if file_path.suffix != ".py":
        return False
    return True


def _extract_refactor_findings(pkt: dict | None) -> list[dict]:
    """Pull actionable findings from a refactor-scan packet."""
    if pkt is None:
        return []
    findings = pkt.get("findings") or pkt.get("metrics", {}).get("findings", [])
    if not isinstance(findings, list):
        return []
    out = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        severity = str(f.get("severity", "")).lower()
        file_rel = f.get("file", "")
        if severity in ACTIONABLE_SEVERITIES and file_rel:
            out.append({
                "source": "refactor-scan",
                "file": file_rel,
                "severity": severity,
                "detail": f.get("detail", f.get("type", "")),
                "suggestion": f.get("suggestion", ""),
            })
    return out


def _extract_debug_findings(pkt: dict | None) -> list[dict]:
    """Pull an actionable finding from a debug-agent packet."""
    if pkt is None:
        return []
    if pkt.get("status") != "success":
        return []
    file_loc = pkt.get("file_location")
    confidence = str(pkt.get("confidence", "")).lower()
    if not file_loc or confidence == "low":
        return []

    # file_location might be "runtime/skills/foo.py:42" — extract the path part
    file_rel = file_loc.split(":")[0].strip()
    if not file_rel:
        return []

    return [{
        "source": "debug-agent",
        "file": file_rel,
        "severity": "high",
        "detail": pkt.get("root_cause", ""),
        "suggestion": pkt.get("suggested_fix", ""),
    }]


def _strip_markdown_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```python or ```)
        lines = lines[1:]
        # Remove trailing fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def _count_changed_lines(original: str, fixed: str) -> int:
    """Count the number of added + removed lines between original and fixed."""
    orig_lines = original.splitlines(keepends=True)
    fix_lines = fixed.splitlines(keepends=True)
    diff = list(unified_diff(orig_lines, fix_lines, n=0))
    changed = sum(1 for line in diff if line.startswith("+") or line.startswith("-"))
    # Subtract the --- and +++ header lines
    changed = max(0, changed - 2)
    return changed


def _generate_fix(model: str, host: str, source_code: str, finding: dict) -> str:
    """
    Ask the LLM for a minimal fix.  Returns the complete fixed file content,
    or empty string on failure.
    """
    system_prompt = (
        "You are an expert Python developer performing a targeted code fix. "
        "You will receive a source file and a description of one specific issue. "
        "Generate a MINIMAL fix — change as few lines as possible. "
        "Output ONLY the complete fixed file. "
        "No markdown fences, no explanations, no commentary."
    )

    detail = finding.get("detail", "")
    suggestion = finding.get("suggestion", "")
    issue_desc = detail
    if suggestion:
        issue_desc += f"\nSuggested fix: {suggestion}"

    user_prompt = (
        f"Issue: {issue_desc}\n\n"
        f"Source file:\n{source_code}"
    )

    try:
        response = chat(
            model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            host=host,
            temperature=0.05,
            max_tokens=4096,
            task_type="auto-fix",
        )
        return _strip_markdown_fences(response)
    except Exception as e:
        log.error("auto-fix: LLM call failed: %s", e)
        return ""


def _append_log(entry: dict) -> None:
    """Append a structured entry to the auto-fix log file."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        log.warning("auto-fix: failed to write log: %s", e)


def _git_commit(file_path: Path, description: str) -> bool:
    """Stage and commit a single file with a descriptive message."""
    try:
        subprocess.run(
            ["/usr/bin/env", "git", "add", str(file_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        result = subprocess.run(
            ["/usr/bin/env", "git", "commit", "-m", f"auto(dev): {description}"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            log.info("auto-fix: committed %s", file_path.name)
            return True
        log.warning("auto-fix: git commit failed: %s", result.stderr.strip()[:200])
        return False
    except Exception as e:
        log.warning("auto-fix: git commit error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(**kwargs) -> dict:
    """
    Read scan findings, generate fixes, apply them to source files.

    kwargs:
        max_fixes (int):   Maximum fixes to attempt per run. Default 3.
        dry_run (bool):    If True, report changes without applying them. Default False.
        source (str):      Which findings to consume: "refactor", "debug", or "all". Default "all".
    """
    max_fixes = int(kwargs.get("max_fixes", 3))
    dry_run = bool(kwargs.get("dry_run", False))
    source = str(kwargs.get("source", "all")).lower()

    # ── Gate: opt-in only ──────────────────────────────────────────────────
    if not AUTO_FIX_ENABLED:
        return {
            "status": "skipped",
            "summary": (
                "Auto-fix is disabled. Set AUTO_FIX_ENABLED=true in .env to enable."
            ),
            "metrics": {
                "findings_total": 0,
                "attempted": 0,
                "fixed": 0,
                "failed": 0,
                "skipped": 0,
                "dry_run": dry_run,
            },
            "fixes": [],
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # ── Model availability ─────────────────────────────────────────────────
    model, host = _pick_model()
    if model is None:
        return {
            "status": "skipped",
            "summary": "Auto-fix: no Coder model available.",
            "metrics": {
                "findings_total": 0,
                "attempted": 0,
                "fixed": 0,
                "failed": 0,
                "skipped": 0,
                "dry_run": dry_run,
            },
            "fixes": [],
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # ── Gather findings ────────────────────────────────────────────────────
    all_findings: list[dict] = []

    if source in ("refactor", "all"):
        refactor_pkt = packet.read_fresh("dev-automation", "refactor-scan", 10080)
        all_findings.extend(_extract_refactor_findings(refactor_pkt))

    if source in ("debug", "all"):
        debug_pkt = packet.read_fresh("dev-automation", "debug-agent", 4320)
        all_findings.extend(_extract_debug_findings(debug_pkt))

    if not all_findings:
        return {
            "status": "success",
            "summary": "Auto-fix: no actionable findings from recent scans.",
            "metrics": {
                "findings_total": 0,
                "attempted": 0,
                "fixed": 0,
                "failed": 0,
                "skipped": 0,
                "dry_run": dry_run,
            },
            "fixes": [],
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # ── Filter for safe, runtime-only files ────────────────────────────────
    safe_findings: list[dict] = []
    for finding in all_findings:
        file_rel = finding["file"]
        file_path = Path(file_rel)
        if not file_path.is_absolute():
            file_path = ROOT / file_path

        if not _is_safe_path(file_path):
            log.info("auto-fix: skipping %s (outside safety boundary)", file_rel)
            continue
        if not file_path.is_file():
            log.info("auto-fix: skipping %s (file not found)", file_rel)
            continue

        finding["_resolved_path"] = file_path
        safe_findings.append(finding)

    # Deduplicate by file path (take the first finding per file)
    seen_files: set[str] = set()
    deduped: list[dict] = []
    for f in safe_findings:
        key = str(f["_resolved_path"])
        if key not in seen_files:
            seen_files.add(key)
            deduped.append(f)
    safe_findings = deduped

    findings_total = len(all_findings)
    candidates = safe_findings[:max_fixes]
    skipped_count = len(safe_findings) - len(candidates)

    # ── Process each finding ───────────────────────────────────────────────
    fixes: list[dict] = []
    attempted = 0
    fixed = 0
    failed = 0

    for finding in candidates:
        file_path: Path = finding["_resolved_path"]
        file_rel = str(file_path.relative_to(ROOT))
        detail = finding.get("detail", "unknown issue")

        fix_entry: dict = {
            "file": file_rel,
            "finding": detail[:200],
            "status": "skipped",
            "lines_changed": 0,
        }

        # Read source
        try:
            source_code = file_path.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("auto-fix: cannot read %s: %s", file_rel, e)
            fix_entry["status"] = "skipped"
            fixes.append(fix_entry)
            skipped_count += 1
            continue

        # Size guard
        line_count = source_code.count("\n") + 1
        if line_count > MAX_SOURCE_LINES:
            log.info("auto-fix: skipping %s (%d lines > %d limit)", file_rel, line_count, MAX_SOURCE_LINES)
            fix_entry["status"] = "skipped"
            fixes.append(fix_entry)
            skipped_count += 1
            continue

        attempted += 1

        # Generate fix via LLM
        fixed_code = _generate_fix(model, host, source_code, finding)
        if not fixed_code:
            log.warning("auto-fix: LLM returned empty fix for %s", file_rel)
            fix_entry["status"] = "failed"
            fixes.append(fix_entry)
            failed += 1
            _append_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "file": file_rel,
                "finding": detail[:200],
                "result": "failed",
                "reason": "LLM returned empty response",
            })
            continue

        # Check diff size
        lines_changed = _count_changed_lines(source_code, fixed_code)
        fix_entry["lines_changed"] = lines_changed

        if lines_changed == 0:
            log.info("auto-fix: fix for %s produced no changes", file_rel)
            fix_entry["status"] = "skipped"
            fixes.append(fix_entry)
            skipped_count += 1
            _append_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "file": file_rel,
                "finding": detail[:200],
                "result": "skipped",
                "reason": "no changes produced",
            })
            continue

        if lines_changed > MAX_LINES_PER_CHANGE:
            log.warning(
                "auto-fix: fix for %s changes %d lines (max %d) — rejecting",
                file_rel, lines_changed, MAX_LINES_PER_CHANGE,
            )
            fix_entry["status"] = "failed"
            fixes.append(fix_entry)
            failed += 1
            _append_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "file": file_rel,
                "finding": detail[:200],
                "result": "failed",
                "reason": f"too many lines changed ({lines_changed} > {MAX_LINES_PER_CHANGE})",
            })
            continue

        # Syntax check the fixed code
        try:
            ast.parse(fixed_code, filename=str(file_path))
        except SyntaxError as e:
            log.warning("auto-fix: fixed code for %s has syntax error: %s", file_rel, e)
            fix_entry["status"] = "failed"
            fixes.append(fix_entry)
            failed += 1
            _append_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "file": file_rel,
                "finding": detail[:200],
                "result": "failed",
                "reason": f"syntax error in fix: {e}",
            })
            continue

        # Dry run: report without applying
        if dry_run:
            fix_entry["status"] = "dry_run"
            fixes.append(fix_entry)
            fixed += 1
            _append_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "file": file_rel,
                "finding": detail[:200],
                "result": "dry_run",
                "lines_changed": lines_changed,
            })
            continue

        # Save backup
        backup_path = Path(f"{file_path}.bak")
        try:
            shutil.copy2(file_path, backup_path)
        except Exception as e:
            log.error("auto-fix: cannot create backup for %s: %s", file_rel, e)
            fix_entry["status"] = "failed"
            fixes.append(fix_entry)
            failed += 1
            continue

        # Write the fix
        try:
            file_path.write_text(fixed_code, encoding="utf-8")
        except Exception as e:
            log.error("auto-fix: cannot write fix to %s: %s", file_rel, e)
            # Restore backup
            shutil.copy2(backup_path, file_path)
            fix_entry["status"] = "failed"
            fixes.append(fix_entry)
            failed += 1
            continue

        # Regression guard — check for accidentally removed symbols
        try:
            from runtime.skills.regression_guard import run as regression_run
            guard_result = regression_run(file_path=str(file_path))
            if not guard_result.get("safe", True):
                # Critical symbols removed — revert immediately
                log.warning("Regression guard BLOCKED fix for %s: %s", file_path.name, guard_result.get("summary", ""))
                shutil.copy2(str(backup_path), str(file_path))
                # Log as failed with reason
                fix_entry["status"] = "blocked"
                fix_entry["reason"] = f"Regression guard: {guard_result.get('summary', 'removed critical symbols')}"
                fixes.append(fix_entry)
                failed += 1
                continue  # skip to next finding
        except ImportError:
            pass  # regression_guard not available, continue without it
        except Exception as e:
            log.warning("Regression guard check failed: %s — continuing without guard", e)

        # Run CI check
        try:
            from runtime.skills.ci_runner import run as ci_run
            ci_result = ci_run(file_path=str(file_path), revert_on_failure=False)
        except Exception as e:
            log.error("auto-fix: CI runner call failed for %s: %s", file_rel, e)
            ci_result = {"status": "failed", "metrics": {"tests_failed": 1}}

        ci_passed = (
            ci_result.get("status") == "success"
            and ci_result.get("metrics", {}).get("tests_failed", 0) == 0
        )

        if not ci_passed:
            # Revert from backup
            log.warning("auto-fix: CI failed for %s — reverting", file_rel)
            try:
                shutil.copy2(backup_path, file_path)
            except Exception as e:
                log.error("auto-fix: revert failed for %s: %s", file_rel, e)

            fix_entry["status"] = "failed"
            fixes.append(fix_entry)
            failed += 1
            _append_log({
                "ts": datetime.now(timezone.utc).isoformat(),
                "file": file_rel,
                "finding": detail[:200],
                "result": "failed",
                "reason": "CI tests failed after applying fix",
                "ci_summary": ci_result.get("summary", ""),
            })
            continue

        # CI passed — keep the fix
        fix_entry["status"] = "applied"
        fixes.append(fix_entry)
        fixed += 1

        _append_log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "file": file_rel,
            "finding": detail[:200],
            "result": "applied",
            "lines_changed": lines_changed,
        })

        # Git commit
        commit_desc = f"fix {file_path.name}: {detail[:60]}"
        _git_commit(file_path, commit_desc)

        # Clean up backup after successful commit
        try:
            backup_path.unlink(missing_ok=True)
        except Exception:
            pass

    # ── Build result ───────────────────────────────────────────────────────
    total_skipped = skipped_count + (len(all_findings) - len(safe_findings))

    if fixed == attempted and attempted > 0:
        status = "success"
    elif fixed > 0:
        status = "partial"
    elif attempted == 0:
        status = "success"  # nothing to do = success
    else:
        status = "partial"

    summary_parts = [f"Auto-fix: {fixed}/{attempted} finding(s) fixed."]
    if failed:
        summary_parts.append(f"{failed} failed.")
    if total_skipped:
        summary_parts.append(f"{total_skipped} skipped.")
    if dry_run:
        summary_parts.append("(dry run — no files modified)")

    escalate = failed > 0 and fixed == 0
    escalation_reason = (
        f"Auto-fix attempted {attempted} fix(es) but all failed"
        if escalate else ""
    )

    return {
        "status": status,
        "summary": " ".join(summary_parts),
        "metrics": {
            "findings_total": findings_total,
            "attempted": attempted,
            "fixed": fixed,
            "failed": failed,
            "skipped": total_skipped,
            "dry_run": dry_run,
        },
        "fixes": fixes,
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }
