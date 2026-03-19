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

# Tier 1 models (3060 Ti, CUDA)
MODEL_7B   = os.getenv("MODEL_7B",   "qwen2.5:7b-instruct-q4_K_M")
MODEL_8B   = os.getenv("MODEL_8B",   "llama3.1:8b-q4_K_M")

# Tier 2 model (friend's 9070 XT, ROCm) — fallback to Tier 3 API if unavailable
MODEL_14B  = os.getenv("MODEL_14B",  "qwen2.5:14b-instruct-q4_K_M")
MODEL_14B_HOST = os.getenv("MODEL_14B_HOST", "http://localhost:11434")  # override if remote

# ── Division model routing ────────────────────────────────────────────────────
SKILL_MODELS = {
    # Tier 1 — 3060 Ti
    "hard-filter":       MODEL_7B,
    "funding-finder":    MODEL_7B,
    "trading-report":    MODEL_7B,
    "perf-correlation":  MODEL_7B,
    "burnout-monitor":   MODEL_7B,
    "market-scan":       MODEL_7B,
    "health-logger":     MODEL_8B,   # privacy — local only, no fallback
    # Tier 2 — friend's machine (with Tier 3 API fallback handled in skill)
    "repo-monitor":      MODEL_14B,
    "debug-agent":       MODEL_14B,
    "refactor-scan":     MODEL_14B,
    "security-scan":     MODEL_14B,
    "doc-update":        MODEL_14B,
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
    for div in ["opportunity", "trading", "personal", "dev-automation"]:
        for sub in ["packets", "hot", "cold", "manifests"]:
            (DIVISIONS_DIR / div / sub).mkdir(parents=True, exist_ok=True)
