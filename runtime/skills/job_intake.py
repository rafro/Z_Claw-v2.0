"""
job-intake skill — Tier 0 tool (pure Python, no LLM).
Fetches all job sources, deduplicates, updates state, returns new listings.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime.tools.jobs import fetch_all_jobs, deduplicate
from runtime.tools.state import (
    load_jobs_seen, save_jobs_seen,
    get_seen_ids, append_new_jobs, save_intake_temp
)
from runtime.config import LOGS_DIR

log = logging.getLogger(__name__)


def run() -> dict:
    """
    Run job-intake. Returns result dict for the orchestrator to reason over:
    {
        "new_jobs": [...],
        "source_status": {...},
        "errors": [...],
        "counts": {"fetched": N, "new": N}
    }
    """
    LOGS_DIR.mkdir(exist_ok=True)

    # 1. Load seen state
    seen_state = load_jobs_seen()
    seen_ids = get_seen_ids(seen_state)
    log.info("Loaded %d seen job IDs", len(seen_ids))

    # 2. Fetch all sources
    all_jobs, source_status, errors = fetch_all_jobs()
    log.info("Fetched %d total listings across all sources", len(all_jobs))

    # 3. Deduplicate
    new_jobs = deduplicate(all_jobs, seen_ids)

    # 4. Update seen state
    if new_jobs:
        seen_state = append_new_jobs(seen_state, new_jobs)
        save_jobs_seen(seen_state)
        log.info("Saved %d new jobs to jobs-seen.json", len(new_jobs))

    # 5. Save handoff for hard-filter
    save_intake_temp(new_jobs)

    # 6. Log errors
    if errors:
        error_log = LOGS_DIR / "job-intake-errors.log"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(error_log, "a", encoding="utf-8") as f:
            for e in errors:
                f.write(f"[{ts}] {e}\n")
        log.warning("Logged %d source errors", len(errors))

    all_failed = all(s in ("failed",) for s in source_status.values())

    return {
        "new_jobs":      new_jobs,
        "source_status": source_status,
        "errors":        errors,
        "all_failed":    all_failed,
        "counts": {
            "fetched": len(all_jobs),
            "new":     len(new_jobs),
        },
    }
