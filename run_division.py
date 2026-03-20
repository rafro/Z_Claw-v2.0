"""
Entry point for the OpenClaw Python runtime.
Called by J_Claw (via shell tool) before reading the executive packet.

Usage:
  python run_division.py opportunity job-intake
  python run_division.py opportunity funding-finder
  python run_division.py trading trading-report
  python run_division.py trading market-scan
  python run_division.py personal health-logger <reply_text>
  python run_division.py personal perf-correlation
  python run_division.py personal burnout-monitor
  python run_division.py personal personal-digest
  python run_division.py dev-automation repo-monitor
  python run_division.py dev-automation debug-agent <error_text> [context_file ...]
  python run_division.py dev-automation refactor-scan
  python run_division.py dev-automation security-scan
  python run_division.py dev-automation doc-update
  python run_division.py dev-automation artifact-manager
  python run_division.py dev-automation dev-digest
  python run_division.py dev pipeline '<json_spec>'
  python run_division.py sentinel provider-health
  python run_division.py sentinel queue-monitor
  python run_division.py sentinel sentinel-digest
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
        from runtime.orchestrators.opportunity import run_job_intake, run_funding_finder
        if task == "job-intake":
            return run_job_intake()
        if task == "funding-finder":
            return run_funding_finder()
        raise ValueError(f"Unknown task for opportunity: {task}")

    # ── Trading ───────────────────────────────────────────────────────────────
    elif division == "trading":
        from runtime.orchestrators.trading import run_trading_report, run_market_scan
        if task == "trading-report":
            return run_trading_report()
        if task == "market-scan":
            return run_market_scan()
        raise ValueError(f"Unknown task for trading: {task}")

    # ── Personal ──────────────────────────────────────────────────────────────
    elif division == "personal":
        from runtime.orchestrators.personal import run_health_logger, run_perf_correlation, run_burnout_monitor, run_personal_digest
        if task == "health-logger":
            reply_text = args[0] if args else ""
            if not reply_text:
                log.warning("health-logger skipped — no reply_text provided (requires Telegram check-in)")
                return {
                    "status": "skipped",
                    "reason": "no reply_text — health-logger requires Telegram interaction",
                    "escalate": False,
                }
            return run_health_logger(reply_text)
        if task == "perf-correlation":
            return run_perf_correlation()
        if task == "burnout-monitor":
            return run_burnout_monitor()
        if task == "personal-digest":
            return run_personal_digest()
        raise ValueError(f"Unknown task for personal: {task}")

    # ── OP-Sec ────────────────────────────────────────────────────────────────
    elif division == "op-sec":
        from runtime.orchestrators.op_sec import (
            run_device_posture, run_breach_check, run_threat_surface,
            run_cred_audit, run_privacy_scan, run_security_scan, run_opsec_digest,
        )
        if task == "device-posture":
            return run_device_posture()
        if task == "breach-check":
            return run_breach_check()
        if task == "threat-surface":
            return run_threat_surface()
        if task == "cred-audit":
            return run_cred_audit()
        if task == "privacy-scan":
            return run_privacy_scan()
        if task == "security-scan":
            return run_security_scan()
        if task == "opsec-digest":
            return run_opsec_digest()
        raise ValueError(f"Unknown task for op-sec: {task}")

    # ── Dev Automation ────────────────────────────────────────────────────────
    elif division == "dev-automation":
        from runtime.orchestrators.dev_automation import (
            run_repo_monitor, run_debug_agent, run_refactor_scan,
            run_doc_update, run_artifact_manager, run_dev_digest,
        )
        if task == "repo-monitor":
            return run_repo_monitor()
        if task == "debug-agent":
            error_text = args[0] if args else ""
            if not error_text:
                log.error("debug-agent requires error_text argument")
                sys.exit(1)
            context_files = [a for a in args[1:] if not a.startswith("--")]
            return run_debug_agent(error_text, context_files or None)
        if task == "refactor-scan":
            return run_refactor_scan()
        if task == "doc-update":
            return run_doc_update()
        if task == "artifact-manager":
            return run_artifact_manager()
        if task == "dev-digest":
            return run_dev_digest()
        raise ValueError(f"Unknown task for dev-automation: {task}")

    # ── Dev Pipeline (new — supplements dev-automation) ───────────────────────
    elif division == "dev":
        from runtime.orchestrators.dev import run_dev_pipeline
        if task == "pipeline":
            import json as _json
            spec_str = args[0] if args else "{}"
            try:
                spec = _json.loads(spec_str)
            except _json.JSONDecodeError:
                # Treat bare string as description
                spec = {"description": spec_str}
            return run_dev_pipeline(spec)
        raise ValueError(f"Unknown task for dev: {task}")

    # ── Sentinel (provider + system health) ───────────────────────────────────
    elif division == "sentinel":
        from runtime.orchestrators.sentinel import (
            run_provider_health, run_queue_monitor,
            run_agent_network_monitor, run_sentinel_digest
        )
        if task == "provider-health":
            return run_provider_health()
        if task == "queue-monitor":
            return run_queue_monitor()
        if task == "agent-network-monitor":
            return run_agent_network_monitor()
        if task == "sentinel-digest":
            return run_sentinel_digest()
        raise ValueError(f"Unknown task for sentinel: {task}")

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
