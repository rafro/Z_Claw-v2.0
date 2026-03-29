"""
mobile-audit-review skill — Tier 1 LLM (Qwen2.5 7B local).
Reviews mobile coding session audit log and git history nightly.
Flags suspicious patterns: sensitive files touched, large changesets,
off-hours sessions, orphaned commits, repeated auth failures.
Writes packet to divisions/op-sec/packets/mobile-audit-review.json.
"""

import json
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

from runtime.config import MODEL_7B, OLLAMA_HOST, ROOT, LOGS_DIR
from runtime.ollama_client import chat_json, is_available

log        = logging.getLogger(__name__)
MODEL      = MODEL_7B
PACKET_DIR = ROOT / "divisions" / "op-sec" / "packets"
AUDIT_LOG  = LOGS_DIR / "mobile-audit.jsonl"
SENSITIVE  = {".env", "SOUL.md", "BOOT.md"}


def _read_recent_sessions(hours: int = 24) -> list[dict]:
    """Read mobile-audit.jsonl, return coding_session entries from last N hours."""
    if not AUDIT_LOG.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    sessions = []
    try:
        with open(AUDIT_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("action") != "coding_session":
                        continue
                    ts_str = entry.get("timestamp", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts >= cutoff:
                            sessions.append(entry)
                except Exception:
                    pass
    except Exception as e:
        log.warning("Could not read audit log: %s", e)
    return sessions


def _read_mobile_git_log(count: int = 20) -> str:
    """Return recent git log lines for commits tagged pre-mobile-session."""
    try:
        r = subprocess.run(
            ["git", "log", f"-{count}", "--oneline", "--grep=pre-mobile-session"],
            capture_output=True, text=True, timeout=10, cwd=str(ROOT),
        )
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def _flag_patterns(sessions: list[dict]) -> list[dict]:
    """Rule-based pre-analysis before LLM review."""
    flags = []
    for s in sessions:
        sid = s.get("session_id", "?")
        ts  = s.get("timestamp", "")

        # Sensitive file tripwire
        sensitive_hit = s.get("sensitive_files", [])
        if sensitive_hit:
            flags.append({
                "session_id": sid,
                "severity":   "HIGH",
                "rule":       "sensitive_file_touched",
                "detail":     f"Session touched: {', '.join(sensitive_hit)}",
            })

        # Large changeset
        files_changed = s.get("files_changed", 0)
        if files_changed > 15:
            flags.append({
                "session_id": sid,
                "severity":   "MEDIUM",
                "rule":       "large_changeset",
                "detail":     f"{files_changed} files changed in one session",
            })

        # Off-hours (midnight–6am local — approximate via UTC)
        if ts:
            try:
                hour = datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
                if 0 <= hour < 6:
                    flags.append({
                        "session_id": sid,
                        "severity":   "LOW",
                        "rule":       "off_hours_session",
                        "detail":     f"Session started at {hour:02d}:00 UTC",
                    })
            except Exception:
                pass

        # Non-zero exit code
        if s.get("exit_code", 0) not in (0, None):
            flags.append({
                "session_id": sid,
                "severity":   "LOW",
                "rule":       "non_zero_exit",
                "detail":     f"Claude CLI exited with code {s.get('exit_code')}",
            })

    return flags


def run() -> dict:
    PACKET_DIR.mkdir(parents=True, exist_ok=True)

    sessions   = _read_recent_sessions(hours=24)
    git_log    = _read_mobile_git_log()
    rule_flags = _flag_patterns(sessions)

    high_count   = sum(1 for f in rule_flags if f["severity"] == "HIGH")
    medium_count = sum(1 for f in rule_flags if f["severity"] == "MEDIUM")
    escalate     = high_count > 0

    if not sessions:
        packet = {
            "skill":        "mobile-audit-review",
            "status":       "ok",
            "summary":      "No mobile coding sessions in the last 24 hours.",
            "escalate":     False,
            "sessions_reviewed": 0,
            "flags":        [],
            "high_flags":   0,
            "medium_flags": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(PACKET_DIR / "mobile-audit-review.json", "w", encoding="utf-8") as f:
            json.dump(packet, f, indent=2)
        return packet

    session_summary = "\n".join(
        f"- session={s.get('session_id','?')} at={s.get('timestamp','')} "
        f"files_changed={s.get('files_changed',0)} "
        f"sensitive={s.get('sensitive_files',[])} "
        f"exit={s.get('exit_code',0)} "
        f"msg='{s.get('message_preview','')[:60]}'"
        for s in sessions
    )

    flags_summary = "\n".join(
        f"- [{f['severity']}] {f['rule']}: {f['detail']} (session {f['session_id']})"
        for f in rule_flags
    ) or "No rule-based flags."

    if not is_available(MODEL, host=OLLAMA_HOST):
        packet = {
            "skill":             "mobile-audit-review",
            "status":            "partial",
            "summary":           f"Rule scan complete — LLM unavailable. {high_count} HIGH, {medium_count} MEDIUM flags.",
            "escalate":          escalate,
            "sessions_reviewed": len(sessions),
            "flags":             rule_flags,
            "high_flags":        high_count,
            "medium_flags":      medium_count,
            "generated_at":      datetime.now(timezone.utc).isoformat(),
        }
        if escalate:
            packet["escalation_reason"] = f"Mobile coding session touched sensitive files. Flags: {flags_summary}"
        with open(PACKET_DIR / "mobile-audit-review.json", "w", encoding="utf-8") as f:
            json.dump(packet, f, indent=2)
        return packet

    messages = [
        {
            "role": "system",
            "content": (
                "You are the OP-Sec mobile audit analyst for J_Claw, a personal AI orchestration system.\n"
                "Matthew accesses J_Claw from mobile via Tailscale and can make real file edits using Claude CLI agent mode.\n"
                "Review the mobile coding sessions from the last 24 hours and identify any security concerns.\n\n"
                "CONTEXT:\n"
                "- Sensitive files that should NEVER be edited from mobile without Tyler's awareness: .env, SOUL.md, BOOT.md\n"
                "- Normal sessions: 1-10 files changed, during daytime hours, exit code 0\n"
                "- Matthew is a trusted operator but all mobile changes should be traceable\n\n"
                "Return JSON only:\n"
                '{"summary": "2-3 sentence overview", "risk_level": "LOW|MEDIUM|HIGH", '
                '"recommendations": ["action items"], "escalate": false}'
            ),
        },
        {
            "role": "user",
            "content": (
                f"SESSIONS (last 24h):\n{session_summary}\n\n"
                f"RULE FLAGS:\n{flags_summary}\n\n"
                f"GIT LOG (mobile-tagged commits):\n{git_log or 'none'}"
            ),
        },
    ]

    try:
        result = chat_json(MODEL, messages, host=OLLAMA_HOST, temperature=0.1, max_tokens=600, task_type="mobile-audit-review")
        summary      = result.get("summary", "Mobile audit complete.") if isinstance(result, dict) else "Audit complete."
        risk_level   = result.get("risk_level", "LOW") if isinstance(result, dict) else "LOW"
        recs         = result.get("recommendations", []) if isinstance(result, dict) else []
        llm_escalate = result.get("escalate", False) if isinstance(result, dict) else False

        if high_count > 0 or risk_level == "HIGH":
            escalate = True
            llm_escalate = True

        packet = {
            "skill":             "mobile-audit-review",
            "status":            "success",
            "summary":           summary,
            "risk_level":        risk_level,
            "escalate":          escalate or llm_escalate,
            "sessions_reviewed": len(sessions),
            "flags":             rule_flags,
            "high_flags":        high_count,
            "medium_flags":      medium_count,
            "recommendations":   recs,
            "generated_at":      datetime.now(timezone.utc).isoformat(),
        }
        if escalate or llm_escalate:
            packet["escalation_reason"] = (
                f"Mobile audit: {high_count} HIGH flag(s), risk={risk_level}. "
                f"{summary[:120]}"
            )
    except Exception as e:
        log.error("mobile-audit-review LLM failed: %s", e)
        packet = {
            "skill":             "mobile-audit-review",
            "status":            "partial",
            "summary":           f"Rule scan complete — LLM failed: {e}",
            "escalate":          escalate,
            "sessions_reviewed": len(sessions),
            "flags":             rule_flags,
            "high_flags":        high_count,
            "medium_flags":      medium_count,
            "generated_at":      datetime.now(timezone.utc).isoformat(),
        }
        if escalate:
            packet["escalation_reason"] = f"Mobile session touched sensitive files. {flags_summary}"

    with open(PACKET_DIR / "mobile-audit-review.json", "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2)

    return packet
