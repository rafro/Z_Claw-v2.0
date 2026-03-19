"""
Entry point for the OpenClaw Python runtime.
Called by J_Claw (via shell tool) before reading the executive packet.

Usage:
  python run_division.py opportunity job-intake
  python run_division.py opportunity funding-finder
  python run_division.py trading trading-report
  python run_division.py personal health-logger <reply_text>
  python run_division.py personal perf-correlation
  python run_division.py dev-automation repo-monitor [--digest]
  python run_division.py realm-keeper grant-skill <skill_name>
  python run_division.py realm-keeper grant-base <amount> [reason]
"""

import sys
import json
import logging
import traceback
from datetime import datetime, timezone

from runtime.config import ensure_dirs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_division")


def run(division: str, task: str, args: list) -> dict:
    ensure_dirs()

    # ── Opportunity ───────────────────────────────────────────────────────────
    if division == "opportunity":
        from runtime.orchestrators.opportunity import run_job_intake
        if task == "job-intake":
            return run_job_intake()
        raise ValueError(f"Unknown task for opportunity: {task}")

    # ── Trading ───────────────────────────────────────────────────────────────
    elif division == "trading":
        from runtime.orchestrators.trading import run_trading_report
        if task == "trading-report":
            return run_trading_report()
        raise ValueError(f"Unknown task for trading: {task}")

    # ── Personal ──────────────────────────────────────────────────────────────
    elif division == "personal":
        from runtime.orchestrators.personal import run_health_logger, run_perf_correlation
        if task == "health-logger":
            reply_text = args[0] if args else ""
            if not reply_text:
                log.error("health-logger requires reply_text argument")
                sys.exit(1)
            return run_health_logger(reply_text)
        if task == "perf-correlation":
            return run_perf_correlation()
        raise ValueError(f"Unknown task for personal: {task}")

    # ── Dev Automation ────────────────────────────────────────────────────────
    elif division == "dev-automation":
        from runtime.orchestrators.dev_automation import run_repo_monitor
        if task == "repo-monitor":
            send_digest = "--digest" in args
            return run_repo_monitor(send_digest=send_digest)
        raise ValueError(f"Unknown task for dev-automation: {task}")

    # ── Realm Keeper (cross-division, pure Python) ────────────────────────────
    elif division == "realm-keeper":
        from runtime.tools.xp import grant_skill_xp, grant_base_xp, current_stats
        if task == "grant-skill":
            skill = args[0] if args else ""
            if not skill:
                log.error("grant-skill requires skill_name argument")
                sys.exit(1)
            return grant_skill_xp(skill)
        if task == "grant-base":
            amount = int(args[0]) if args else 0
            reason = args[1] if len(args) > 1 else ""
            return grant_base_xp(amount, reason)
        if task == "stats":
            return current_stats()
        raise ValueError(f"Unknown realm-keeper task: {task}")

    else:
        raise ValueError(f"Unknown division: {division}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    division  = sys.argv[1]
    task      = sys.argv[2]
    extra_args = sys.argv[3:]

    log.info("Starting: %s / %s", division, task)

    try:
        result = run(division, task, extra_args)
        print(json.dumps(result, indent=2, default=str))
        log.info(
            "Completed: %s / %s | status=%s escalate=%s",
            division, task,
            result.get("status", "?"),
            result.get("escalate", False),
        )
        sys.exit(0)

    except Exception as e:
        log.error("FAILED: %s / %s — %s", division, task, e)
        traceback.print_exc()
        sys.exit(1)
