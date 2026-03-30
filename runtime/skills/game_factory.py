"""
game-factory skill — Master pipeline that autonomously creates a game from scratch.
Chains gamedev skills in 6 stages: Preflight -> Design -> Specs -> Assets -> Code -> Ship.
Each stage gates on critical failure. Pipeline state saved for resume capability.
Tier 1 for synthesis, Tier 0 for orchestration logic.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.config import STATE_DIR
from runtime.orchestrators.gamedev import (
    run_game_design, run_story_writer, run_character_designer,
    run_enemy_designer, run_item_forge, run_quest_writer,
    run_skill_tree_builder, run_tech_spec, run_mechanic_prototype,
    run_level_design, run_data_populate, run_balance_audit,
    run_asset_requester, run_production_bridge, run_asset_integration,
    run_project_init, run_code_generate, run_code_review,
    run_code_test, run_iteration_runner, run_scene_assemble,
    run_build_pipeline, run_game_runner, run_auto_playtest,
    run_refine_loop, run_gamedev_digest,
)

log = logging.getLogger(__name__)

FACTORY_DIR = STATE_DIR / "gamedev"
CONFIG_PATH = FACTORY_DIR / "factory-config.json"
STATE_PATH = FACTORY_DIR / "factory-state.json"
RUNS_PATH = FACTORY_DIR / "factory-runs.jsonl"

# ---------------------------------------------------------------------------
# Stage definitions — ordered list of (stage_name, skill_steps)
# Each skill_step is (label, callable, kwargs_override | None)
# ---------------------------------------------------------------------------

STAGE_NAMES = ("PREFLIGHT", "DESIGN", "SPECS", "ASSETS", "CODE", "SHIP")

_SENTINEL = object()  # used for deferred kwargs that depend on run() args


def _run_preflight(target="pygame", **kwargs):
    """Stage 0: Verify environment before starting the pipeline."""
    from runtime.ollama_client import is_available
    from runtime.config import MODEL_7B, OLLAMA_HOST
    import shutil

    checks = {}
    issues = []

    # Check Ollama
    ollama_ok = is_available(MODEL_7B, host=OLLAMA_HOST)
    checks["ollama"] = ollama_ok
    if not ollama_ok:
        issues.append("Ollama not available — LLM skills will return degraded results")

    # Check target engine
    if target == "godot":
        godot_ok = shutil.which("godot") is not None
        checks["godot"] = godot_ok
        if not godot_ok:
            issues.append("Godot binary not found in PATH — game-runner and build-pipeline will skip Godot features")

    # Check state directory writable
    try:
        test_file = STATE_DIR / "gamedev" / ".preflight-test"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("ok")
        test_file.unlink()
        checks["state_writable"] = True
    except Exception:
        checks["state_writable"] = False
        issues.append("Cannot write to state/gamedev/ directory")

    status = "success" if not issues else "partial"
    escalate = not checks.get("state_writable", True)  # Only escalate if can't write state

    return {
        "status": status,
        "summary": f"Preflight: {len(checks) - len(issues)}/{len(checks)} checks passed." + (f" Issues: {'; '.join(issues)}" if issues else ""),
        "metrics": checks,
        "escalate": escalate,
        "escalation_reason": "; ".join(issues) if escalate else "",
        "action_items": [{"priority": "high", "description": issue} for issue in issues],
    }


def _build_stages(target: str) -> list[tuple[str, list[tuple[str, Any, dict | None]]]]:
    """
    Construct the stage list.  Each stage is a tuple of
    (stage_name, [(step_label, callable, extra_kwargs | None), ...]).
    `target` is threaded into steps that need it.
    """
    # Build code-generate steps from tech specs
    code_gen_steps = []
    specs_dir = STATE_DIR / "gamedev" / "tech-specs"
    if specs_dir.exists():
        spec_files = sorted(specs_dir.glob("*.json"))
        if spec_files:
            for spec_file in spec_files:
                system_name = spec_file.stem
                code_gen_steps.append(
                    (f"code-generate:{system_name}", run_code_generate, {"system_name": system_name, "target": target})
                )
    if not code_gen_steps:
        code_gen_steps = [("code-generate", run_code_generate, {"target": target})]

    return [
        ("PREFLIGHT", [
            ("preflight-check", _run_preflight, {"target": target}),
        ]),
        ("DESIGN", [
            ("game-design",        run_game_design,        None),
            ("story-writer",       run_story_writer,       {"section": "overview"}),
            ("character-designer", run_character_designer,  None),
            ("enemy-designer",     run_enemy_designer,      None),
            ("item-forge",         run_item_forge,          None),
            ("quest-writer",       run_quest_writer,        None),
            ("skill-tree-builder", run_skill_tree_builder,  None),
        ]),
        ("SPECS", [
            ("tech-spec",           run_tech_spec,           None),
            ("mechanic-prototype",  run_mechanic_prototype,  None),
            ("level-design",        run_level_design,        None),
            ("data-populate",       run_data_populate,       None),
            ("balance-audit",       run_balance_audit,       None),
        ]),
        ("ASSETS", [
            ("asset-requester",     run_asset_requester,     None),
            ("production-bridge",   run_production_bridge,   None),
            ("asset-integration",   run_asset_integration,   None),
        ]),
        ("CODE", [
            ("project-init",      run_project_init,      {"target": target}),
            *code_gen_steps,
            ("code-review",       run_code_review,       None),
            ("code-test-gen",     run_code_test,         {"action": "generate"}),
            ("code-test-run",     run_code_test,         {"action": "run"}),
            ("iteration-runner",  run_iteration_runner,  None),
            ("scene-assemble",    run_scene_assemble,    None),
        ]),
        ("SHIP", [
            ("build-pipeline",    run_build_pipeline,    {"action": "package", "target": target}),
            ("game-runner",       run_game_runner,       {"target": target}),
            ("auto-playtest",     run_auto_playtest,     {"target": target}),
            ("refine-loop",       run_refine_loop,       {"target": target}),
            ("gamedev-digest",    run_gamedev_digest,    None),
        ]),
    ]


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "default_target": "pygame",
    "max_retries_per_skill": 1,
    "halt_on_critical": True,
    "production_bridge_enabled": True,
    "created_at": None,  # filled on first write
}


def _load_config() -> dict:
    """Load factory config; create default if missing."""
    FACTORY_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load factory config, using defaults: %s", e)

    cfg = {**_DEFAULT_CONFIG, "created_at": datetime.now(timezone.utc).isoformat()}
    _save_json(CONFIG_PATH, cfg)
    return cfg


def _save_json(path: Path, data: Any) -> None:
    """Atomic-ish JSON write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        tmp.replace(path)
    except Exception as e:
        log.error("Failed to write %s: %s", path, e)
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _append_jsonl(path: Path, record: dict) -> None:
    """Append a single JSON record to a .jsonl file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        log.error("Failed to append to %s: %s", path, e)


# ---------------------------------------------------------------------------
# Pipeline state persistence
# ---------------------------------------------------------------------------

def _load_state() -> dict | None:
    """Load previous pipeline state for resume, or None."""
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load factory state: %s", e)
    return None


def _save_state(state: dict) -> None:
    _save_json(STATE_PATH, state)


def _new_state(target: str) -> dict:
    """Create a fresh pipeline state dict."""
    return {
        "target": target,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "overall_status": "running",
        "stages": {},
    }


# ---------------------------------------------------------------------------
# Skill execution helpers
# ---------------------------------------------------------------------------

def _run_skill(label: str, fn: Any, extra_kwargs: dict | None) -> dict:
    """
    Invoke a single orchestrator run_* function with exception handling.
    Returns the packet dict from the orchestrator, or a synthetic failure
    packet if the call threw.
    """
    kwargs = extra_kwargs or {}
    try:
        # asset_integration takes no kwargs
        if fn is run_asset_integration or fn is run_gamedev_digest:
            result = fn()
        else:
            result = fn(**kwargs)
        return result
    except Exception as e:
        log.error("game-factory: skill '%s' raised %s: %s", label, type(e).__name__, e)
        return {
            "status": "failed",
            "summary": f"Skill '{label}' raised {type(e).__name__}: {e}",
            "escalate": False,
            "escalation_reason": "",
            "metrics": {},
        }


def _is_critical_failure(result: dict) -> bool:
    """True if the result indicates a critical (halting) failure."""
    return result.get("status") == "failed" and result.get("escalate", False)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(**kwargs) -> dict:
    """
    Master game-factory pipeline.

    kwargs:
        target (str):       "pygame" | "godot".  Default "pygame".
        stages (list[str]): Stage names to run (e.g. ["DESIGN", "CODE"]).
                            Default: all five stages.
        resume (bool):      If True, skip stages already completed in
                            factory-state.json.  Default False.
    """
    t0 = time.monotonic()

    config = _load_config()
    target = kwargs.get("target", config.get("default_target", "pygame"))
    requested_stages = kwargs.get("stages", None)  # None means "all"
    resume = kwargs.get("resume", False)

    log.info(
        "=== GAME FACTORY: starting pipeline  target=%s  resume=%s  stages=%s ===",
        target, resume, requested_stages or "ALL",
    )

    # Build the stage definitions
    all_stages = _build_stages(target)

    # Filter to requested stages (preserve ordering)
    if requested_stages:
        requested_set = {s.upper() for s in requested_stages}
        all_stages = [(name, steps) for name, steps in all_stages if name in requested_set]

    if not all_stages:
        return {
            "status": "failed",
            "summary": "No valid stages to run.",
            "metrics": _empty_metrics(target),
            "escalate": False,
            "escalation_reason": "No stages matched the requested list.",
            "action_items": [],
        }

    # Load or create pipeline state
    prev_state = _load_state() if resume else None
    completed_stages: set[str] = set()
    if prev_state and resume:
        for stage_name, stage_info in prev_state.get("stages", {}).items():
            if stage_info.get("status") == "completed":
                completed_stages.add(stage_name)
        log.info("Resuming — skipping completed stages: %s", completed_stages or "(none)")

    state = _new_state(target)

    # Tracking counters
    stages_total = len(all_stages)
    stages_completed = 0
    skills_run_total = 0
    skills_passed = 0
    skills_failed = 0
    stage_results: dict[str, dict] = {}
    all_action_items: list[str] = []
    halted = False
    halt_reason = ""

    # ── Execute stages sequentially ──────────────────────────────────────
    for stage_name, steps in all_stages:
        # Resume: skip already-completed stages
        if stage_name in completed_stages:
            log.info("STAGE %s — skipped (already completed in previous run)", stage_name)
            stage_results[stage_name] = {
                "status": "skipped-resume",
                "skills_run": 0,
                "skills_passed": 0,
                "skills_failed": 0,
            }
            stages_completed += 1
            continue

        log.info("STAGE %s — starting (%d skills)", stage_name, len(steps))
        stage_t0 = time.monotonic()

        stage_skills_run = 0
        stage_skills_passed = 0
        stage_skills_failed = 0
        stage_skill_results: list[dict] = []
        stage_halted = False

        for step_label, step_fn, step_kwargs in steps:
            log.info("  [%s] %s ...", stage_name, step_label)
            result = _run_skill(step_label, step_fn, step_kwargs)

            status = result.get("status", "unknown")
            skills_run_total += 1
            stage_skills_run += 1

            step_record = {
                "skill": step_label,
                "status": status,
                "summary": result.get("summary", ""),
                "escalate": result.get("escalate", False),
            }
            stage_skill_results.append(step_record)

            if status in ("success", "partial", "degraded"):
                skills_passed += 1
                stage_skills_passed += 1
                log.info("  [%s] %s -> %s", stage_name, step_label, status)
            else:
                skills_failed += 1
                stage_skills_failed += 1
                log.warning("  [%s] %s -> FAILED", stage_name, step_label)

                # Check for critical failure (escalate=True on a failed result)
                if _is_critical_failure(result):
                    reason = result.get("escalation_reason", f"Critical failure in {step_label}")
                    all_action_items.append(
                        f"[FACTORY HALT] Stage {stage_name} halted at {step_label}: {reason}"
                    )
                    log.error(
                        "  [%s] CRITICAL FAILURE at %s — halting stage. Reason: %s",
                        stage_name, step_label, reason,
                    )
                    stage_halted = True
                    halt_reason = reason
                    break
                else:
                    # Non-critical failure — log and continue
                    all_action_items.append(
                        f"[FACTORY WARN] {step_label} failed (non-critical): "
                        f"{result.get('summary', 'no details')[:120]}"
                    )

        stage_elapsed = time.monotonic() - stage_t0

        stage_info = {
            "stage_name": stage_name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": "halted" if stage_halted else "completed",
            "elapsed_seconds": round(stage_elapsed, 1),
            "skills_run": stage_skills_run,
            "skills_passed": stage_skills_passed,
            "skills_failed": stage_skills_failed,
            "skill_results": stage_skill_results,
        }

        stage_results[stage_name] = stage_info
        state["stages"][stage_name] = stage_info
        _save_state(state)

        if stage_halted:
            halted = True
            log.error("STAGE %s — HALTED after %.1fs", stage_name, stage_elapsed)
            break
        else:
            stages_completed += 1
            log.info(
                "STAGE %s — completed in %.1fs  (%d/%d skills passed)",
                stage_name, stage_elapsed, stage_skills_passed, stage_skills_run,
            )

    # ── Compute overall status ───────────────────────────────────────────
    elapsed_minutes = round((time.monotonic() - t0) / 60, 2)

    if halted:
        overall_status = "failed"
    elif stages_completed == stages_total:
        overall_status = "success"
    else:
        overall_status = "partial"

    # Finalize state
    state["completed_at"] = datetime.now(timezone.utc).isoformat()
    state["overall_status"] = overall_status
    _save_state(state)

    # ── Build summary ────────────────────────────────────────────────────
    summary_parts = [
        f"Game factory {overall_status}: {stages_completed}/{stages_total} stages completed",
        f"({skills_passed}/{skills_run_total} skills passed)",
        f"in {elapsed_minutes:.1f} min.",
    ]
    if halted:
        summary_parts.append(f"Halted: {halt_reason}")
    if skills_failed > 0 and not halted:
        summary_parts.append(f"{skills_failed} non-critical failure(s) logged.")

    summary = " ".join(summary_parts)

    # ── Build return dict ────────────────────────────────────────────────
    result = {
        "status": overall_status,
        "summary": summary,
        "metrics": {
            "stages_completed": stages_completed,
            "stages_total": stages_total,
            "skills_run": skills_run_total,
            "skills_passed": skills_passed,
            "skills_failed": skills_failed,
            "target": target,
            "elapsed_minutes": elapsed_minutes,
            "stage_results": stage_results,
        },
        "escalate": halted,
        "escalation_reason": halt_reason if halted else "",
        "action_items": all_action_items,
    }

    # ── Persist run history ──────────────────────────────────────────────
    run_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": overall_status,
        "target": target,
        "stages_completed": stages_completed,
        "stages_total": stages_total,
        "skills_run": skills_run_total,
        "skills_passed": skills_passed,
        "skills_failed": skills_failed,
        "elapsed_minutes": elapsed_minutes,
        "halted": halted,
        "halt_reason": halt_reason,
        "resume": resume,
    }
    _append_jsonl(RUNS_PATH, run_record)

    log.info(
        "=== GAME FACTORY: pipeline %s  stages=%d/%d  skills=%d/%d  %.1f min ===",
        overall_status, stages_completed, stages_total,
        skills_passed, skills_run_total, elapsed_minutes,
    )

    return result


def _empty_metrics(target: str) -> dict:
    """Return a zeroed-out metrics dict for early-exit returns."""
    return {
        "stages_completed": 0,
        "stages_total": 0,
        "skills_run": 0,
        "skills_passed": 0,
        "skills_failed": 0,
        "target": target,
        "elapsed_minutes": 0.0,
        "stage_results": {},
    }
