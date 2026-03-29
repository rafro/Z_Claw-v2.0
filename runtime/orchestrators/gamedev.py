"""
Game Development Division Orchestrator — ARDENT, Warden of the Eternal Engine.
Skills: game-design, balance-audit -> Tier 1 (7B local).
        mechanic-prototype, level-design, tech-spec -> Tier 1 (7B local).
        playtest-report -> Tier 1.
        asset-integration -> Tier 0 (deterministic cross-division packet reader).
        gamedev-digest -> orchestrator synthesis (reads all gamedev packets).
"""

import logging

from runtime.config import SKILL_MODELS, OLLAMA_HOST, MODEL_7B
from runtime.ollama_client import chat, is_available
from runtime.skills import (
    game_design, mechanic_prototype, balance_audit,
    level_design, tech_spec, playtest_report, asset_integration,
)
from runtime import packet
from runtime.tools.xp import grant_skill_xp

log = logging.getLogger(__name__)
MODEL = MODEL_7B


# -- Orchestrator reasoning ----------------------------------------------------

def _synthesize_gamedev_state(
    design_pkt: dict | None,
    mechanic_pkt: dict | None,
    balance_pkt: dict | None,
    level_pkt: dict | None,
    tech_pkt: dict | None,
    playtest_pkt: dict | None,
    asset_pkt: dict | None,
) -> str:
    """
    Cross-skill synthesis: combine all gamedev skill outputs into an executive
    summary for the nightly briefing. This is where the Game Dev Division
    orchestrator earns its LLM tier.
    """
    summaries = {}
    for label, pkt_data in [
        ("Game Design", design_pkt),
        ("Mechanic Prototype", mechanic_pkt),
        ("Balance Audit", balance_pkt),
        ("Level Design", level_pkt),
        ("Tech Spec", tech_pkt),
        ("Playtest Report", playtest_pkt),
        ("Asset Integration", asset_pkt),
    ]:
        if pkt_data:
            summaries[label] = pkt_data.get("summary", "No data.")

    if not summaries:
        return "No gamedev skill data available for synthesis."

    if not is_available(MODEL):
        parts = [f"{k}: {v}" for k, v in summaries.items() if v and "No data" not in v]
        return " | ".join(parts) if parts else "Gamedev data logged — LLM unavailable for synthesis."

    context = "\n".join(f"{k}: {v}" for k, v in summaries.items())

    messages = [
        {
            "role": "system",
            "content": (
                "You are ARDENT, Warden of the Eternal Engine — the Game Development "
                "Division orchestrator for Z_Claw. Given today's outputs from game design, "
                "mechanic prototyping, balance auditing, level design, tech specs, playtesting, "
                "and asset integration, write a 2-3 sentence executive summary for Matthew. "
                "Highlight: progress on the current game project, any blockers or risks, "
                "and what the next priority should be. Be direct — no filler."
            ),
        },
        {"role": "user", "content": context},
    ]
    try:
        result = chat(MODEL, messages, temperature=0.2, max_tokens=200, task_type="gamedev-digest")
        lines = result.strip().splitlines()
        if lines and lines[0].rstrip().endswith(":"):
            result = "\n".join(lines[1:]).lstrip()
        return result
    except Exception as e:
        log.warning("gamedev orchestrator synthesis failed: %s", e)
        parts = [f"{k}: {v}" for k, v in summaries.items()]
        return " | ".join(parts)


# -- Individual skill runners --------------------------------------------------

def run_game_design(**kwargs) -> dict:
    """Generate or iterate on game design document sections."""
    log.info("=== Game Dev Division: game-design run ===")

    result = game_design.run(**kwargs)

    pkt = packet.build(
        division="gamedev",
        skill="game-design",
        status=result["status"],
        summary=result.get("summary", "Game design pass complete."),
        metrics=result.get("metrics", {}),
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    if result["status"] in ("success", "partial"):
        grant_skill_xp("game-design")
    log.info("Game-design packet written. Status=%s", result["status"])
    return pkt


def run_mechanic_prototype(**kwargs) -> dict:
    """Prototype a game mechanic with pseudocode and logic spec."""
    log.info("=== Game Dev Division: mechanic-prototype run ===")

    result = mechanic_prototype.run(**kwargs)

    pkt = packet.build(
        division="gamedev",
        skill="mechanic-prototype",
        status=result["status"],
        summary=result.get("summary", "Mechanic prototype complete."),
        metrics=result.get("metrics", {}),
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    if result["status"] in ("success", "partial"):
        grant_skill_xp("mechanic-prototype")
    log.info("Mechanic-prototype packet written. Status=%s", result["status"])
    return pkt


def run_balance_audit(**kwargs) -> dict:
    """Audit game balance data — damage tables, economy, progression curves."""
    log.info("=== Game Dev Division: balance-audit run ===")

    result = balance_audit.run(**kwargs)

    action_items = []
    for finding in result.get("findings", []):
        if finding.get("severity") in ("high", "critical"):
            action_items.append(packet.action_item(
                f"[BALANCE {finding['severity'].upper()}] {finding.get('description', 'Issue detected')}",
                priority="high",
                requires_matthew=False,
            ))

    pkt = packet.build(
        division="gamedev",
        skill="balance-audit",
        status=result["status"],
        summary=result.get("summary", "Balance audit complete."),
        metrics=result.get("metrics", {}),
        action_items=action_items,
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    if result["status"] in ("success", "partial"):
        grant_skill_xp("balance-audit")
    log.info(
        "Balance-audit packet written. Status=%s findings=%d",
        result["status"], len(result.get("findings", [])),
    )
    return pkt


def run_level_design(**kwargs) -> dict:
    """Generate procedural level layout suggestions."""
    log.info("=== Game Dev Division: level-design run ===")

    result = level_design.run(**kwargs)

    pkt = packet.build(
        division="gamedev",
        skill="level-design",
        status=result["status"],
        summary=result.get("summary", "Level design pass complete."),
        metrics=result.get("metrics", {}),
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    if result["status"] in ("success", "partial"):
        grant_skill_xp("level-design")
    log.info("Level-design packet written. Status=%s", result["status"])
    return pkt


def run_tech_spec(**kwargs) -> dict:
    """Generate technical design document for a game system."""
    log.info("=== Game Dev Division: tech-spec run ===")

    result = tech_spec.run(**kwargs)

    pkt = packet.build(
        division="gamedev",
        skill="tech-spec",
        status=result["status"],
        summary=result.get("summary", "Tech spec generation complete."),
        metrics=result.get("metrics", {}),
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    if result["status"] in ("success", "partial"):
        grant_skill_xp("tech-spec")
    log.info("Tech-spec packet written. Status=%s", result["status"])
    return pkt


def run_playtest_report(**kwargs) -> dict:
    """Analyze playtest data and generate a structured report."""
    log.info("=== Game Dev Division: playtest-report run ===")

    result = playtest_report.run(**kwargs)

    action_items = []
    for issue in result.get("critical_issues", []):
        action_items.append(packet.action_item(
            f"[PLAYTEST] {issue}",
            priority="high",
            requires_matthew=False,
        ))

    pkt = packet.build(
        division="gamedev",
        skill="playtest-report",
        status=result["status"],
        summary=result.get("summary", "Playtest report complete."),
        metrics=result.get("metrics", {}),
        action_items=action_items,
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    if result["status"] in ("success", "partial"):
        grant_skill_xp("playtest-report")
    log.info(
        "Playtest-report packet written. Status=%s sessions=%d",
        result["status"], result.get("metrics", {}).get("sessions_analyzed", 0),
    )
    return pkt


def run_asset_integration() -> dict:
    """Cross-division asset gap analysis — reads production packets."""
    log.info("=== Game Dev Division: asset-integration run ===")

    result = asset_integration.run()

    action_items = []
    for gap in result.get("gaps", []):
        action_items.append(packet.action_item(
            f"[ASSET GAP] {gap.get('asset', 'unknown')}: {gap.get('reason', 'missing')}",
            priority="normal",
            requires_matthew=False,
        ))

    pkt = packet.build(
        division="gamedev",
        skill="asset-integration",
        status=result["status"],
        summary=result.get("summary", "Asset integration check complete."),
        metrics=result.get("metrics", {}),
        action_items=action_items,
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
        provider_used="deterministic",
    )

    packet.write(pkt)
    if result["status"] in ("success", "partial"):
        grant_skill_xp("asset-integration")
    log.info(
        "Asset-integration packet written. Status=%s gaps=%d",
        result["status"], len(result.get("gaps", [])),
    )
    return pkt


def run_gamedev_digest() -> dict:
    """
    Orchestrator synthesis — reads all gamedev skill packets and produces
    a single cross-skill executive summary for the nightly briefing.
    This is where the Game Dev Division orchestrator earns its LLM tier.
    """
    log.info("=== Game Dev Division: gamedev-digest synthesis ===")

    design_pkt   = packet.read_fresh("gamedev", "game-design", 4320)       # 3 days
    mechanic_pkt = packet.read_fresh("gamedev", "mechanic-prototype", 4320)
    balance_pkt  = packet.read_fresh("gamedev", "balance-audit", 1440)     # daily
    level_pkt    = packet.read_fresh("gamedev", "level-design", 4320)
    tech_pkt     = packet.read_fresh("gamedev", "tech-spec", 4320)
    playtest_pkt = packet.read_fresh("gamedev", "playtest-report", 1440)
    asset_pkt    = packet.read_fresh("gamedev", "asset-integration", 1440)

    synthesis = _synthesize_gamedev_state(
        design_pkt, mechanic_pkt, balance_pkt,
        level_pkt, tech_pkt, playtest_pkt, asset_pkt,
    )

    # Aggregate escalation signals
    all_pkts = [design_pkt, mechanic_pkt, balance_pkt, level_pkt,
                tech_pkt, playtest_pkt, asset_pkt]
    escalate = any(
        p.get("escalate", False) for p in all_pkts if p
    )
    escalation_reasons = [
        p.get("escalation_reason", "")
        for p in all_pkts
        if p and p.get("escalation_reason")
    ]

    data_sources = sum(1 for p in all_pkts if p)

    pkt = packet.build(
        division="gamedev",
        skill="gamedev-digest",
        status="success",
        summary=synthesis,
        metrics={
            "data_sources":       data_sources,
            "has_design":         bool(design_pkt),
            "has_mechanic":       bool(mechanic_pkt),
            "has_balance":        bool(balance_pkt),
            "has_level":          bool(level_pkt),
            "has_tech_spec":      bool(tech_pkt),
            "has_playtest":       bool(playtest_pkt),
            "has_asset_check":    bool(asset_pkt),
        },
        escalate=escalate,
        escalation_reason=" | ".join(escalation_reasons) if escalation_reasons else "",
    )

    packet.write(pkt)
    grant_skill_xp("gamedev-digest")
    log.info("Gamedev digest packet written. Sources=%d Escalate=%s", data_sources, escalate)
    return pkt
