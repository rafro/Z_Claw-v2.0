"""
Entry point for the OpenClaw Python runtime.
Called by J_Claw (via shell tool) before reading the executive packet.

Usage:
  python run_division.py opportunity job-intake
  python run_division.py opportunity funding-finder
  python run_division.py opportunity application-tracker
  python run_division.py trading trading-report
  python run_division.py trading market-scan
  python run_division.py personal health-logger <reply_text>
  python run_division.py personal perf-correlation
  python run_division.py personal burnout-monitor
  python run_division.py personal personal-digest
  python run_division.py personal weekly-retrospective
  python run_division.py dev-automation repo-monitor
  python run_division.py dev-automation debug-agent <error_text> [context_file ...]
  python run_division.py dev-automation refactor-scan
  python run_division.py dev-automation doc-update
  python run_division.py dev-automation artifact-manager
  python run_division.py dev-automation dev-digest
  python run_division.py dev pipeline '<json_spec>'
  python run_division.py op-sec mobile-audit-review
  python run_division.py op-sec network-monitor
  python run_division.py production image-generate portrait_bust vael
  python run_division.py production sprite-generate vael chibi_sprite
  python run_division.py production prompt-craft portrait_bust seren
  python run_division.py production style-check <image_path> vael
  python run_division.py production image-review <image_path>
  python run_division.py production audio-test <audio_path>
  python run_division.py production video-review <video_path>
  python run_division.py production asset-catalog
  python run_division.py production storyboard-compose
  python run_division.py production continuity-check vael
  python run_division.py production asset-deliver
  python run_division.py production production-digest
  python run_division.py production qa-pipeline [commander]
  python run_division.py gamedev game-design
  python run_division.py gamedev mechanic-prototype
  python run_division.py gamedev balance-audit
  python run_division.py gamedev level-design
  python run_division.py gamedev tech-spec '{"system":"combat"}'
  python run_division.py gamedev playtest-report
  python run_division.py gamedev asset-integration
  python run_division.py gamedev gamedev-digest
  python run_division.py sentinel provider-health
  python run_division.py sentinel queue-monitor
  python run_division.py sentinel sentinel-digest
  python run_division.py realm-keeper grant-skill <skill_name>
  python run_division.py realm-keeper grant-base <amount> [reason]
  python run_division.py realm-keeper grant-division <division> <amount> [skill_name] [reason]
  python run_division.py realm-keeper force-prestige
  python run_division.py realm-keeper story-state
  python run_division.py realm-keeper story-choice <division> <choice_id> [choice_text]
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
        from runtime.orchestrators.opportunity import run_job_intake, run_funding_finder, run_application_tracker
        if task == "job-intake":
            return run_job_intake()
        if task == "funding-finder":
            return run_funding_finder()
        if task == "application-tracker":
            return run_application_tracker()
        raise ValueError(f"Unknown task for opportunity: {task}")

    # ── Trading ───────────────────────────────────────────────────────────────
    elif division == "trading":
        from runtime.orchestrators.trading import run_trading_report, run_market_scan, run_virtual_trader, run_backtester, run_strategy_builder, run_strategy_tester, run_strategy_search
        if task == "trading-report":
            return run_trading_report()
        if task == "market-scan":
            return run_market_scan()
        if task == "virtual-trader":
            return run_virtual_trader()
        if task == "backtester":
            return run_backtester()
        if task == "strategy-builder":
            return run_strategy_builder()
        if task == "strategy-tester":
            return run_strategy_tester()
        if task == "strategy-search":
            return run_strategy_search()
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
        if task == "weekly-retrospective":
            from runtime.skills.weekly_retrospective import run as run_weekly_retro
            return run_weekly_retro()
        raise ValueError(f"Unknown task for personal: {task}")

    # ── OP-Sec ────────────────────────────────────────────────────────────────
    elif division == "op-sec":
        from runtime.orchestrators.op_sec import (
            run_device_posture, run_breach_check, run_threat_surface,
            run_cred_audit, run_privacy_scan, run_security_scan, run_opsec_digest,
            run_mobile_audit_review, run_network_monitor,
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
        if task == "mobile-audit-review":
            return run_mobile_audit_review()
        if task == "network-monitor":
            return run_network_monitor()
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

    # ── Production Division (LYKE — The Lykeon Forge) ────────────────────────
    elif division == "production":
        from runtime.orchestrators import production as prod_orch
        task_map = {
            "prompt-craft":       lambda: prod_orch.run_prompt_craft(
                                      asset_type=args[0] if args else "portrait_bust",
                                      commander=args[1] if len(args) > 1 else "generic",
                                      subject=args[2] if len(args) > 2 else ""),
            "image-generate":     lambda: prod_orch.run_image_generate(
                                      asset_type=args[0] if args else "portrait_bust",
                                      commander=args[1] if len(args) > 1 else "generic",
                                      subject=args[2] if len(args) > 2 else ""),
            "sprite-generate":    lambda: prod_orch.run_sprite_generate(
                                      target=args[0] if args else "vael",
                                      sprite_type=args[1] if len(args) > 1 else "chibi_sprite"),
            "video-generate":     lambda: prod_orch.run_video_generate(
                                      scene_type=args[0] if args else "battle",
                                      commander=args[1] if len(args) > 1 else "generic",
                                      description=args[2] if len(args) > 2 else ""),
            "graphic-design":     lambda: prod_orch.run_graphic_design(
                                      ui_type=args[0] if args else "card_border",
                                      theme=args[1] if len(args) > 1 else "generic"),
            "style-check":        lambda: prod_orch.run_style_check(
                                      image_path=args[0] if args else "",
                                      commander=args[1] if len(args) > 1 else "generic"),
            "image-review":       lambda: prod_orch.run_image_review(
                                      image_path=args[0] if args else ""),
            "audio-test":         lambda: prod_orch.run_audio_test(
                                      audio_path=args[0] if args else ""),
            "video-review":       lambda: prod_orch.run_video_review(
                                      video_path=args[0] if args else ""),
            "asset-catalog":      lambda: prod_orch.run_asset_catalog(),
            "storyboard-compose": lambda: prod_orch.run_storyboard_compose(),
            "continuity-check":   lambda: prod_orch.run_continuity_check(
                                      commander=args[0] if args else ""),
            "asset-deliver":      lambda: prod_orch.run_asset_deliver(),
            "production-digest":  lambda: prod_orch.run_production_digest(),
            "qa-pipeline":        lambda: prod_orch.run_qa_pipeline(
                                      commander=args[0] if args else "generic"),
            "voice-generate":     lambda: prod_orch.run_voice_generate(
                                      commander=args[0] if args else "generic",
                                      line_type=args[1] if len(args) > 1 else "greeting"),
            "music-compose":      lambda: prod_orch.run_music_compose(
                                      track_type=args[0] if args else "main_theme",
                                      division=args[1] if len(args) > 1 else "trading"),
            "art-director":       lambda: prod_orch.run_art_director(
                                      focus_area=args[0] if args else "general",
                                      commander=args[1] if len(args) > 1 else "generic"),
            "narrative-craft":    lambda: prod_orch.run_narrative_craft(
                                      event_type=args[0] if args else "auto",
                                      commander=args[1] if len(args) > 1 else "generic"),
            "sfx-generate":       lambda: prod_orch.run_sfx_generate(
                                      sfx_type=args[0] if args else "sword_slash",
                                      variation=int(args[1]) if len(args) > 1 else 0),
            "asset-optimize":     lambda: prod_orch.run_asset_optimize(
                                      image_path=args[0] if args else "",
                                      scale=int(args[1]) if len(args) > 1 else 2),
            "voice-catalog":      lambda: prod_orch.run_voice_catalog(),
            "model-trainer":      lambda: prod_orch.run_model_trainer(
                                      domain=args[0] if args else "trading",
                                      base_model=args[1] if len(args) > 1 else "bitnet-1b",
                                      action=args[2] if len(args) > 2 else "status"),
            "adapter-manager":    lambda: prod_orch.run_adapter_manager(
                                      action=args[0] if args else "status",
                                      domain=args[1] if len(args) > 1 else None),
        }
        runner = task_map.get(task)
        if not runner:
            raise ValueError(f"Unknown task for production: {task}")
        return runner()

    # ── Game Development ───────────────────────────────────────────────────
    elif division == "gamedev":
        from runtime.orchestrators.gamedev import (
            run_game_design, run_mechanic_prototype, run_balance_audit,
            run_level_design, run_tech_spec, run_playtest_report,
            run_asset_integration, run_gamedev_digest,
        )
        if task == "game-design":
            return run_game_design()
        if task == "mechanic-prototype":
            return run_mechanic_prototype()
        if task == "balance-audit":
            return run_balance_audit()
        if task == "level-design":
            return run_level_design()
        if task == "tech-spec":
            if args:
                try:
                    spec = json.loads(args[0])
                except json.JSONDecodeError:
                    spec = {"description": args[0]}
            else:
                spec = {}
            return run_tech_spec(**spec)
        if task == "playtest-report":
            return run_playtest_report()
        if task == "asset-integration":
            return run_asset_integration()
        if task == "gamedev-digest":
            return run_gamedev_digest()
        raise ValueError(f"Unknown task for gamedev: {task}")

    # ── Realm Keeper (cross-division, pure Python) ────────────────────────────
    elif division == "realm-keeper":
        from runtime.tools.xp import (
            current_stats,
            force_prestige,
            grant_base_xp,
            grant_division_xp,
            grant_skill_xp,
        )
        from runtime.realm.story import apply_choice, current_state as current_story_state
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
        if task == "grant-division":
            division_key = args[0] if args else ""
            amount = int(args[1]) if len(args) > 1 else 0
            skill_name = args[2] if len(args) > 2 else "manual-bestow"
            reason = args[3] if len(args) > 3 else ""
            if not division_key:
                log.error("grant-division requires division argument")
                sys.exit(1)
            return grant_division_xp(division_key, amount, skill_name, reason)
        if task == "force-prestige":
            return force_prestige()
        if task == "story-state":
            return current_story_state()
        if task == "story-choice":
            division_key = args[0] if args else ""
            choice_id = args[1] if len(args) > 1 else ""
            choice_text = args[2] if len(args) > 2 else ""
            if not division_key or not choice_id:
                log.error("story-choice requires division and choice_id")
                sys.exit(1)
            return apply_choice(division_key, choice_id, choice_text)
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
