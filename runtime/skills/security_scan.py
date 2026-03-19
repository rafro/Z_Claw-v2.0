"""
security-scan skill — Tier 2 LLM (Qwen2.5 14B) with Tier 1 7B fallback.
Weekly scan for hardcoded credentials, dangerous patterns, and risky code.
Runs a pure-Python static pass first, then LLM assessment of findings.
"""

import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

from runtime.config import SKILL_MODELS, MODEL_14B_HOST, MODEL_7B, OLLAMA_HOST, ROOT
from runtime.ollama_client import chat_json, is_available

log     = logging.getLogger(__name__)
MODEL   = SKILL_MODELS["security-scan"]
HOT_DIR = ROOT / "divisions" / "op-sec" / "hot"

SCAN_DIRS  = ["runtime"]
EXTENSIONS = {".py", ".js", ".ts"}
MAX_FILES  = 25

# (compiled_pattern, vulnerability_type, severity)
RISKY_PATTERNS = [
    (re.compile(r'(password|secret|api_key|private_key)\s*=\s*["\'][^"\']{4,}', re.IGNORECASE),
     "hardcoded_credential", "HIGH"),
    (re.compile(r'subprocess\.[a-z_]+\(.*shell\s*=\s*True', re.IGNORECASE),
     "shell_injection_risk", "HIGH"),
    (re.compile(r'\beval\s*\(', re.IGNORECASE),
     "eval_usage", "MEDIUM"),
    (re.compile(r'\bpickle\.loads?\s*\(', re.IGNORECASE),
     "unsafe_deserialization", "MEDIUM"),
    (re.compile(r'os\.system\s*\(', re.IGNORECASE),
     "os_system_call", "MEDIUM"),
    (re.compile(r'except\s*:\s*$|except\s+Exception\s*:\s*\n\s*pass', re.IGNORECASE | re.MULTILINE),
     "broad_exception_suppression", "LOW"),
]


def _static_scan() -> list:
    """Regex-based static analysis. Returns list of finding dicts."""
    findings = []
    for scan_dir in SCAN_DIRS:
        p = ROOT / scan_dir
        if not p.exists():
            continue
        for f in p.rglob("*"):
            if f.suffix not in EXTENSIONS or not f.is_file():
                continue
            if any(skip in str(f) for skip in ("__pycache__", ".git", "node_modules")):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                lines   = content.splitlines()
                for pattern, vuln_type, severity in RISKY_PATTERNS:
                    for i, line in enumerate(lines, 1):
                        if pattern.search(line):
                            findings.append({
                                "severity": severity,
                                "type":     vuln_type,
                                "file":     str(f.relative_to(ROOT)),
                                "line":     i,
                                "detail":   line.strip()[:80],
                                "source":   "static",
                            })
            except Exception as e:
                log.warning("security-scan read error %s: %s", f.name, e)
    return findings


def run() -> dict:
    HOT_DIR.mkdir(parents=True, exist_ok=True)

    static_findings = _static_scan()
    high_static     = [f for f in static_findings if f["severity"] == "HIGH"]

    if is_available(MODEL, host=MODEL_14B_HOST):
        use_model, use_host = MODEL, MODEL_14B_HOST
    elif is_available(MODEL_7B, host=OLLAMA_HOST):
        use_model, use_host = MODEL_7B, OLLAMA_HOST
    else:
        summary = (
            f"Static scan only (no model available). "
            f"{len(static_findings)} issues: {len(high_static)} HIGH."
        )
        return {
            "status":             "partial",
            "summary":            summary,
            "findings":           static_findings,
            "escalate":           len(high_static) > 0,
            "escalation_reason":  f"{len(high_static)} HIGH severity issues" if high_static else "",
            "model_used":         None,
        }

    static_text = "\n".join(
        f"[{f['severity']}] {f['file']}:{f['line']} — {f['type']}: {f['detail']}"
        for f in static_findings[:25]
    ) or "No static findings."

    messages = [
        {
            "role": "system",
            "content": (
                "You are the OP-Sec security scanner for J_Claw. "
                "Review these static analysis findings from a Python runtime codebase. "
                "Identify real risks vs false positives. "
                'Return JSON: {"summary": "1-2 sentences", "findings": ['
                '{"severity": "HIGH|MEDIUM|LOW", "type": "", "file": "", '
                '"detail": "", "fix": ""}], '
                '"false_positives": ["describe any false positives"]} '
                "Max 10 findings. Return valid JSON only."
            ),
        },
        {
            "role": "user",
            "content": f"Static scan ({len(static_findings)} findings):\n{static_text}",
        },
    ]

    try:
        result   = chat_json(use_model, messages, host=use_host, temperature=0.05, max_tokens=800)
        findings = result.get("findings", static_findings) if isinstance(result, dict) else static_findings
        summary  = result.get("summary", "") if isinstance(result, dict) else ""
        if not summary:
            summary = f"{len(findings)} security issues found."

        today = date.today().isoformat()
        with open(HOT_DIR / f"security-scan-{today}.json", "w", encoding="utf-8") as fh:
            json.dump({
                "date":         today,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "summary":      summary,
                "findings":     findings,
            }, fh, indent=2)

        high_count = sum(1 for f in findings if f.get("severity") == "HIGH")
        return {
            "status":            "success",
            "summary":           summary,
            "findings":          findings,
            "escalate":          high_count > 0,
            "escalation_reason": f"{high_count} HIGH severity security issues" if high_count else "",
            "model_used":        use_model,
        }
    except Exception as e:
        log.error("security-scan LLM failed: %s", e)
        summary = (
            f"Static: {len(static_findings)} issues ({len(high_static)} HIGH). "
            f"LLM analysis failed."
        )
        return {
            "status":            "partial",
            "summary":           summary,
            "findings":          static_findings,
            "escalate":          len(high_static) > 0,
            "escalation_reason": f"{len(high_static)} HIGH severity issues" if high_static else "",
            "model_used":        use_model,
        }
