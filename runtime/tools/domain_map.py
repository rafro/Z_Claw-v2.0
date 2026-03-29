"""Shared domain mapping and capture hash computation for the QVAC training pipeline."""
import hashlib
import json

DOMAINS = {
    "trading":     ["market-scan", "trading-report", "virtual-trader", "backtester"],
    "coding":      ["repo-monitor", "debug-agent", "refactor-scan", "doc-update", "dev-digest"],
    "chat":        ["chat-operator", "chat-mobile", "chat-coding"],
    "opsec":       ["threat-surface", "cred-audit", "breach-check", "privacy-scan", "security-scan", "device-posture", "opsec-digest", "network-monitor"],
    "personal":    ["health-logger", "perf-correlation", "burnout-monitor", "personal-digest", "weekly-retrospective"],
    "opportunity": ["job-intake", "hard-filter", "funding-finder", "application-tracker"],
    "production":  ["image-generate", "sprite-generate", "video-generate", "voice-generate", "music-compose", "prompt-craft", "art-director", "narrative-craft", "sfx-generate", "asset-optimize"],
}

TASK_TO_DOMAIN = {}
for _d, _tasks in DOMAINS.items():
    for _t in _tasks:
        TASK_TO_DOMAIN[_t] = _d

def get_domain(task_type: str) -> str:
    """Map a task_type string to its training domain. Returns 'other' if unknown."""
    if not task_type:
        return "other"
    # Exact match first
    if task_type in TASK_TO_DOMAIN:
        return TASK_TO_DOMAIN[task_type]
    # Prefix match fallback
    for domain, tasks in DOMAINS.items():
        for t in tasks:
            if task_type.startswith(t.split("-")[0]):
                return domain
    return "other"

def compute_capture_hash(messages: list, response: str) -> str:
    """Compute a deterministic SHA-256 hash from messages + response content.
    This hash is the universal key for tracking a capture across the entire pipeline."""
    content = json.dumps(messages, sort_keys=True, ensure_ascii=False) + "||" + (response or "")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
