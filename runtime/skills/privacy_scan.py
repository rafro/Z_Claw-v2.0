"""
privacy-scan skill — Tier 1 LLM (Qwen2.5 7B).
Scans Desktop, Documents, Downloads, and AppData for PII leakage:
SSNs, SINs, credit cards, phone numbers, dates of birth.
Weekly run. Local only — data never leaves the machine.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import SKILL_MODELS, OLLAMA_HOST, ROOT
from runtime.ollama_client import chat_json, is_available

log        = logging.getLogger(__name__)
MODEL      = SKILL_MODELS["privacy-scan"]
HOT_DIR    = ROOT / "divisions" / "op-sec" / "hot"
PACKET_DIR = ROOT / "divisions" / "op-sec" / "packets"

SCAN_DIRS = [
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Downloads",
    Path.home() / "AppData" / "Roaming",
]
EXTENSIONS = {".txt", ".csv", ".json", ".md", ".conf", ".ini", ".log"}
MAX_FILES  = 40

PII_PATTERNS = [
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),                                                         "ssn_us",       "HIGH"),
    (re.compile(r'\bsin\s*[:#]?\s*\d{3}[-\s]?\d{3}[-\s]?\d{3}\b', re.IGNORECASE),                 "sin_canada",   "HIGH"),
    (re.compile(r'\b4[0-9]{12}(?:[0-9]{3})?\b|\b5[1-5][0-9]{14}\b'),                               "credit_card",  "HIGH"),
    (re.compile(r'\b(dob|date.of.birth|born)\s*[:\-]\s*\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}\b', re.IGNORECASE), "dob", "HIGH"),
    (re.compile(r'\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b'),                                               "phone_number", "MEDIUM"),
    (re.compile(r'\b[A-Za-z0-9._%+-]{3,}@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),                       "email",        "LOW"),
]


def _scan() -> list:
    findings = []
    scanned  = 0
    for d in SCAN_DIRS:
        if not d.exists():
            continue
        try:
            for f in d.rglob("*"):
                if scanned >= MAX_FILES:
                    break
                if not f.is_file() or f.suffix.lower() not in EXTENSIONS:
                    continue
                if f.stat().st_size > 2_000_000:
                    continue
                scanned += 1
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    for pattern, pii_type, severity in PII_PATTERNS:
                        matches = pattern.findall(content)
                        if matches:
                            findings.append({
                                "severity": severity,
                                "type":     pii_type,
                                "file":     str(f),
                                "matches":  len(matches),
                                "detail":   f"{len(matches)} instance(s) of {pii_type} in {f.name}",
                            })
                except Exception:
                    pass
        except Exception as e:
            log.warning("privacy-scan error in %s: %s", d, e)
    return findings


def run() -> dict:
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    PACKET_DIR.mkdir(parents=True, exist_ok=True)

    findings = _scan()
    high     = [f for f in findings if f["severity"] == "HIGH"]

    if not findings:
        result = {
            "status":     "success",
            "escalate":   False,
            "findings":   [],
            "summary":    "Privacy scan: clean — no PII patterns detected in scanned directories",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(PACKET_DIR / "privacy-scan.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return result

    if not is_available(MODEL, host=OLLAMA_HOST):
        result = {
            "status":     "partial",
            "escalate":   len(high) > 0,
            "findings":   findings,
            "summary":    f"Privacy scan: {len(findings)} PII patterns found ({len(high)} HIGH) — review manually",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(PACKET_DIR / "privacy-scan.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return result

    findings_text = "\n".join(
        f"[{f['severity']}] {f['file']} — {f['type']}: {f['detail']}"
        for f in findings[:20]
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are the OP-Sec privacy auditor for J_Claw. "
                "Review PII patterns found in files on Matthew's Windows 11 system. "
                "Identify genuine privacy risks vs false positives "
                "(e.g., an email address in a config file is normal; an SSN in a text file is not). "
                'Return JSON: {"summary": "1-2 sentences", "risks": ['
                '{"severity": "HIGH|MEDIUM|LOW", "type": "", "file": "", "action": ""}]} '
                "Return valid JSON only."
            ),
        },
        {"role": "user", "content": f"PII scan findings:\n{findings_text}"},
    ]

    try:
        result     = chat_json(MODEL, messages, host=OLLAMA_HOST, temperature=0.05, max_tokens=600)
        risks      = result.get("risks", findings) if isinstance(result, dict) else findings
        summary    = result.get("summary", f"{len(risks)} privacy risks found.") if isinstance(result, dict) else ""
        high_count = sum(1 for r in risks if r.get("severity") == "HIGH")

        packet = {
            "status":     "success",
            "escalate":   high_count > 0,
            "findings":   risks,
            "summary":    summary,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.error("privacy-scan LLM failed: %s", e)
        packet = {
            "status":     "partial",
            "escalate":   len(high) > 0,
            "findings":   findings,
            "summary":    f"Privacy scan: {len(findings)} patterns found — LLM analysis failed.",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }

    with open(PACKET_DIR / "privacy-scan.json", "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2)
    return packet
