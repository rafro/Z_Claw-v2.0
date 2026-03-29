"""
cred-audit skill — Tier 1 LLM (Qwen2.5 7B).
System-wide scan for exposed credentials and secrets in files.
More comprehensive than security-scan (code only) — covers .env files,
config dirs, user home, and common secret storage locations.
Weekly run. Local only — credential values are redacted before LLM sees them.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import SKILL_MODELS, OLLAMA_HOST, ROOT
from runtime.ollama_client import chat_json, is_available

log        = logging.getLogger(__name__)
MODEL      = SKILL_MODELS["cred-audit"]
HOT_DIR    = ROOT / "divisions" / "op-sec" / "hot"
PACKET_DIR = ROOT / "divisions" / "op-sec" / "packets"

SCAN_DIRS = [
    Path.home(),
    ROOT,
    Path.home() / "AppData" / "Roaming",
]
SKIP_DIRS  = {"__pycache__", ".git", "node_modules", "venv", ".venv", "Temp", "Cache"}
EXTENSIONS = {".env", ".json", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".conf"}
NAMED_FILES = {".env", "credentials", "secrets", "config"}
MAX_FILES   = 50

CRED_PATTERNS = [
    (re.compile(r'(password|passwd|pwd)\s*[=:]\s*["\']?[^\s"\']{6,}', re.IGNORECASE),   "password",           "HIGH"),
    (re.compile(r'(api_key|apikey|api-key)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{16,}', re.IGNORECASE), "api_key", "HIGH"),
    (re.compile(r'(secret|token|bearer)\s*[=:]\s*["\']?[A-Za-z0-9_\-\.]{16,}', re.IGNORECASE), "secret_token", "HIGH"),
    (re.compile(r'(private_key|privatekey)\s*[=:]\s*["\']?[^\s"\']{8,}', re.IGNORECASE), "private_key",       "HIGH"),
    (re.compile(r'AKIA[0-9A-Z]{16}'),                                                     "aws_access_key",    "HIGH"),
    (re.compile(r'mongodb(\+srv)?://[^@\s"\']{4,}@', re.IGNORECASE),                     "db_connection",     "HIGH"),
    (re.compile(r'(postgres|mysql|redis)://[^@\s"\']{4,}@', re.IGNORECASE),              "db_connection",     "HIGH"),
]

_REDACT = re.compile(r'([=:]\s*["\']?)([^"\'\s]{4,})')


def _scan() -> list:
    findings = []
    scanned  = 0
    for scan_dir in SCAN_DIRS:
        if not scan_dir.exists():
            continue
        try:
            for f in scan_dir.rglob("*"):
                if scanned >= MAX_FILES:
                    break
                if not f.is_file():
                    continue
                if any(skip in f.parts for skip in SKIP_DIRS):
                    continue
                if f.suffix.lower() not in EXTENSIONS and f.name not in NAMED_FILES:
                    continue
                if f.stat().st_size > 1_000_000:
                    continue
                scanned += 1
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    for pattern, cred_type, severity in CRED_PATTERNS:
                        for i, line in enumerate(content.splitlines(), 1):
                            if not pattern.search(line):
                                continue
                            # skip obvious placeholders
                            low = line.lower()
                            if any(x in low for x in ("example", "placeholder", "your_", "<your", "changeme")):
                                continue
                            redacted = _REDACT.sub(r'\1[REDACTED]', line.strip()[:100])
                            findings.append({
                                "severity":  severity,
                                "type":      cred_type,
                                "file":      str(f),
                                "line":      i,
                                "detail":    redacted,
                            })
                except Exception:
                    pass
        except Exception as e:
            log.warning("cred-audit scan error in %s: %s", scan_dir, e)
    return findings


def run() -> dict:
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    PACKET_DIR.mkdir(parents=True, exist_ok=True)

    findings   = _scan()
    high       = [f for f in findings if f["severity"] == "HIGH"]
    escalate   = len(high) > 0

    if not findings:
        result = {
            "status":     "success",
            "escalate":   False,
            "findings":   [],
            "summary":    "Cred audit: clean — no credential patterns detected",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(PACKET_DIR / "cred-audit.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return result

    if not is_available(MODEL, host=OLLAMA_HOST):
        result = {
            "status":     "partial",
            "escalate":   escalate,
            "findings":   findings[:20],
            "summary":    f"Cred audit: {len(findings)} potential exposures ({len(high)} HIGH) — LLM unavailable",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(PACKET_DIR / "cred-audit.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return result

    findings_text = "\n".join(
        f"[{f['severity']}] {f['file']}:{f['line']} — {f['type']}: {f['detail']}"
        for f in findings[:20]
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are the OP-Sec credential auditor for J_Claw. "
                "Review potential credential exposures found in system files. "
                "Credential values have been redacted — you only see file paths and types. "
                "Distinguish real leaks from false positives (example values, template configs). "
                'Return JSON: {"summary": "1-2 sentences", "confirmed": ['
                '{"severity": "HIGH|MEDIUM", "type": "", "file": "", "action": ""}], '
                '"false_positives": ["brief description"]} '
                "Return valid JSON only."
            ),
        },
        {"role": "user", "content": f"Credential scan findings:\n{findings_text}"},
    ]

    try:
        result     = chat_json(MODEL, messages, host=OLLAMA_HOST, temperature=0.05, max_tokens=600, task_type="cred-audit")
        confirmed  = result.get("confirmed", findings[:20]) if isinstance(result, dict) else findings[:20]
        summary    = result.get("summary", f"{len(confirmed)} credential issues.") if isinstance(result, dict) else ""
        high_count = sum(1 for f in confirmed if f.get("severity") == "HIGH")

        packet = {
            "status":     "success",
            "escalate":   high_count > 0,
            "findings":   confirmed,
            "summary":    summary,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.error("cred-audit LLM failed: %s", e)
        packet = {
            "status":     "partial",
            "escalate":   escalate,
            "findings":   findings[:20],
            "summary":    f"Cred audit: {len(findings)} findings — LLM analysis failed.",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }

    with open(PACKET_DIR / "cred-audit.json", "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2)
    return packet
