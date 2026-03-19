"""
Entry point for the OpenClaw Python runtime.
Called by Windows Task Scheduler or PM2 to run a division task.

Usage:
  python run_division.py opportunity job-intake
  python run_division.py personal health-logger
  python run_division.py trading trading-report
  python run_division.py dev-automation repo-monitor
"""

import sys
import json
import logging
import traceback
from datetime import datetime, timezone

from runtime.config import ensure_dirs

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_division")


def run(division: str, task: str) -> dict:
    ensure_dirs()

    if division == "opportunity":
        from runtime.orchestrators.opportunity import run_job_intake
        if task == "job-intake":
            return run_job_intake()
        else:
            raise ValueError(f"Unknown task for opportunity: {task}")

    # Stubs for other divisions — to be implemented
    elif division == "personal":
        raise NotImplementedError(f"personal/{task} not yet implemented")
    elif division == "trading":
        raise NotImplementedError(f"trading/{task} not yet implemented")
    elif division == "dev-automation":
        raise NotImplementedError(f"dev-automation/{task} not yet implemented")
    else:
        raise ValueError(f"Unknown division: {division}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python run_division.py <division> <task>")
        sys.exit(1)

    division = sys.argv[1]
    task = sys.argv[2]
    log.info("Starting: %s / %s", division, task)

    try:
        result = run(division, task)
        print(json.dumps(result, indent=2, default=str))
        log.info("Completed: %s / %s | status=%s escalate=%s",
                 division, task,
                 result.get("status"), result.get("escalate"))
        sys.exit(0)

    except NotImplementedError as e:
        log.warning("Not yet implemented: %s", e)
        sys.exit(2)
    except Exception as e:
        log.error("FAILED: %s / %s — %s", division, task, e)
        traceback.print_exc()
        sys.exit(1)
