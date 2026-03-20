"""
Shared config loader for the OpenClaw Python runtime.
Loads .env and provides paths + Ollama model config.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent          # OpenClaw-Orchestrator/
STATE_DIR = ROOT / "state"
LOGS_DIR = ROOT / "logs"
DIVISIONS_DIR = ROOT / "divisions"
REPORTS_DIR = ROOT / "reports"

# ── Environment ───────────────────────────────────────────────────────────────
load_dotenv(ROOT / ".env")

ADZUNA_APP_ID  = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")

# ── Ollama ────────────────────────────────────────────────────────────────────
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# ── Ollama models (all run on your 9070 XT via ROCm) ─────────────────────────
# Tier 1 (7B/8B) — fast, daily workhorses; Tier 2 (14B) — deep reasoning only
MODEL_7B        = os.getenv("MODEL_7B",        "qwen2.5:7b-instruct-q4_K_M")
MODEL_8B        = os.getenv("MODEL_8B",        "llama3.2:3b")
MODEL_CODER_7B  = os.getenv("MODEL_CODER_7B",  "qwen2.5-coder:7b-instruct-q4_K_M")
MODEL_CODER_14B = os.getenv("MODEL_CODER_14B", "qwen2.5-coder:14b-instruct-q4_K_M")
MODEL_14B_HOST  = os.getenv("MODEL_14B_HOST",  "http://localhost:11434")

# ── Division model routing ────────────────────────────────────────────────────
# DEPRECATED: Use providers.router.ProviderRouter().get_provider(task_type) instead.
# This dict is kept for backward compatibility with any code that still imports it.
# New code should never add entries here.
SKILL_MODELS = {
    "hard-filter":       MODEL_7B,
    "funding-finder":    MODEL_7B,
    "trading-report":    MODEL_7B,
    "perf-correlation":  MODEL_7B,
    "burnout-monitor":   MODEL_7B,
    "market-scan":       MODEL_7B,
    "health-logger":     MODEL_8B,
    "repo-monitor":      MODEL_CODER_7B,
    "refactor-scan":     MODEL_CODER_7B,
    "security-scan":     MODEL_CODER_7B,
    "debug-agent":       MODEL_CODER_14B,
    "doc-update":        MODEL_CODER_14B,
    "dev-digest":        MODEL_7B,
    "threat-surface":    MODEL_CODER_7B,
    "cred-audit":        MODEL_CODER_7B,
    "privacy-scan":      MODEL_7B,
}


def division_config(division: str) -> dict:
    path = DIVISIONS_DIR / division / "config.json"
    with open(path) as f:
        return json.load(f)


def packet_path(division: str, skill: str) -> Path:
    return DIVISIONS_DIR / division / "packets" / f"{skill}.json"


def ensure_dirs():
    """Create any missing runtime directories."""
    for d in [STATE_DIR, LOGS_DIR, REPORTS_DIR]:
        d.mkdir(exist_ok=True)
    for div in ["opportunity", "trading", "personal", "dev-automation", "op-sec", "dev", "sentinel"]:
        for sub in ["packets", "hot", "cold", "manifests"]:
            (DIVISIONS_DIR / div / sub).mkdir(parents=True, exist_ok=True)
