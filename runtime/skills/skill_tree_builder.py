"""
skill-tree-builder skill — Designs character progression/skill trees
with tiered nodes, prerequisites, synergies, and circular-dependency validation.
Reads character sheets for class context.
Saves trees to state/gamedev/skill-trees/{tree_name}.json.
Tier 1 (7B local).
"""

import json
import logging
from collections import deque
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_7B, STATE_DIR
from runtime.ollama_client import chat_json, is_available

log = logging.getLogger(__name__)
MODEL = MODEL_7B
GAMEDEV_DIR = STATE_DIR / "gamedev"
SKILL_TREES_DIR = GAMEDEV_DIR / "skill-trees"
CHARACTERS_DIR = GAMEDEV_DIR / "characters"

# Tier definitions: tier number -> required_points to unlock
TIER_REQUIREMENTS = {
    1: 0,
    2: 5,
    3: 15,
    4: 30,
}

NODE_TYPES = ("active", "passive", "toggle", "ultimate", "aura", "buff")


# ── State helpers ────────────────────────────────────────────────────────────

def _load_tree(tree_name: str) -> dict | None:
    """Load an existing skill tree, or None if not found."""
    safe_name = tree_name.lower().replace(" ", "_").replace("/", "_")
    fpath = SKILL_TREES_DIR / f"{safe_name}.json"
    if fpath.exists():
        try:
            with open(fpath, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load skill tree '%s': %s", tree_name, e)
    return None


def _save_tree(tree: dict) -> None:
    """Persist a skill tree to its own file."""
    SKILL_TREES_DIR.mkdir(parents=True, exist_ok=True)
    name = tree.get("tree_name", "unnamed_tree")
    safe_name = name.lower().replace(" ", "_").replace("/", "_")
    fpath = SKILL_TREES_DIR / f"{safe_name}.json"
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)


# ── Context gathering ────────────────────────────────────────────────────────

def _load_character_context(class_type: str) -> str:
    """Read character sheets to understand the class archetype."""
    parts = []
    if CHARACTERS_DIR.exists():
        try:
            for fpath in sorted(CHARACTERS_DIR.glob("*.json"))[:15]:
                with open(fpath, encoding="utf-8") as f:
                    char = json.load(f)
                char_class = char.get("class", char.get("class_type", ""))
                if char_class.lower() == class_type.lower() or not class_type:
                    name = char.get("name", fpath.stem)
                    role = char.get("role", "")
                    abilities = char.get("abilities", [])
                    stats = char.get("base_stats", {})
                    parts.append(f"  {name} ({role}, class: {char_class})")
                    if abilities:
                        ability_names = [a.get("name", a) if isinstance(a, dict) else str(a) for a in abilities[:5]]
                        parts.append(f"    Abilities: {', '.join(ability_names)}")
                    if stats:
                        stat_str = ", ".join(f"{k}={v}" for k, v in list(stats.items())[:6])
                        parts.append(f"    Stats: {stat_str}")
        except Exception as e:
            log.warning("Failed to read character data for class context: %s", e)

    if parts:
        return "Related characters:\n" + "\n".join(parts)
    return ""


def _load_existing_trees_context() -> str:
    """Summarise existing skill trees to avoid duplication."""
    if not SKILL_TREES_DIR.exists():
        return ""
    parts = []
    try:
        for fpath in sorted(SKILL_TREES_DIR.glob("*.json"))[:10]:
            with open(fpath, encoding="utf-8") as f:
                tree = json.load(f)
            name = tree.get("tree_name", fpath.stem)
            cls = tree.get("class_type", "?")
            tier_count = len(tree.get("tiers", []))
            total_nodes = sum(len(t.get("nodes", [])) for t in tree.get("tiers", []))
            parts.append(f"  {name} ({cls}): {tier_count} tiers, {total_nodes} nodes")
    except Exception as e:
        log.warning("Failed to read existing skill trees: %s", e)

    if parts:
        return "Existing skill trees:\n" + "\n".join(parts)
    return ""


# ── Circular dependency validation ───────────────────────────────────────────

def _detect_circular_deps(tiers: list[dict]) -> list[str]:
    """
    Check all node prerequisites for circular dependencies.
    Returns a list of error messages (empty = no cycles).
    Uses Kahn's algorithm (topological sort) on the prerequisite graph.
    """
    # Build adjacency from prerequisites -> node
    all_nodes: dict[str, set[str]] = {}  # node_id -> set of prerequisite node_ids
    node_ids: set[str] = set()

    for tier in tiers:
        for node in tier.get("nodes", []):
            nid = node.get("id", "")
            if not nid:
                continue
            node_ids.add(nid)
            prereqs = set()
            for p in (node.get("prerequisites") or []):
                pid = p if isinstance(p, str) else ""
                if pid:
                    prereqs.add(pid)
            all_nodes[nid] = prereqs

    # Kahn's algorithm
    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}

    for nid, prereqs in all_nodes.items():
        for p in prereqs:
            if p in node_ids:
                adjacency[p].append(nid)
                in_degree[nid] = in_degree.get(nid, 0) + 1

    queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
    sorted_count = 0

    while queue:
        current = queue.popleft()
        sorted_count += 1
        for neighbor in adjacency.get(current, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if sorted_count < len(node_ids):
        # There's a cycle — find which nodes are involved
        cycle_nodes = [nid for nid, deg in in_degree.items() if deg > 0]
        return [f"Circular dependency detected involving nodes: {', '.join(cycle_nodes)}"]

    return []


def _break_circular_deps(tiers: list[dict]) -> tuple[list[dict], int]:
    """
    Attempt to break circular dependencies by removing back-edges.
    Returns (fixed tiers, number of edges removed).
    """
    removed = 0
    # Build set of all node IDs with their tier numbers
    node_tiers: dict[str, int] = {}
    for tier in tiers:
        tier_num = tier.get("tier", 0)
        for node in tier.get("nodes", []):
            nid = node.get("id", "")
            if nid:
                node_tiers[nid] = tier_num

    # Remove any prerequisite that is from a higher or same tier (back-edge)
    for tier in tiers:
        tier_num = tier.get("tier", 0)
        for node in tier.get("nodes", []):
            nid = node.get("id", "")
            if not nid:
                continue
            original_prereqs = node.get("prerequisites", [])
            if not isinstance(original_prereqs, list):
                node["prerequisites"] = []
                continue
            cleaned = []
            for p in original_prereqs:
                pid = p if isinstance(p, str) else ""
                if not pid:
                    continue
                prereq_tier = node_tiers.get(pid, 0)
                if prereq_tier >= tier_num:
                    # Back-edge or same-tier edge — skip it
                    removed += 1
                    log.info("skill-tree-builder: removed back-edge %s -> %s (tier %d -> %d)",
                             pid, nid, prereq_tier, tier_num)
                else:
                    cleaned.append(pid)
            node["prerequisites"] = cleaned

    return tiers, removed


# ── Scaffold (fallback) ─────────────────────────────────────────────────────

def _scaffold_tree(class_type: str, tree_name: str, prompt: str) -> dict:
    """Return a minimal skill tree scaffold when LLM is unavailable."""
    return {
        "tree_name": tree_name,
        "class_type": class_type,
        "max_points": 40,
        "tiers": [
            {
                "tier": 1,
                "required_points": TIER_REQUIREMENTS[1],
                "nodes": [
                    {
                        "id": f"{class_type.lower()}_t1_basic",
                        "name": f"Basic {class_type} Skill",
                        "type": "active",
                        "description": f"A foundational ability for the {class_type} class.",
                        "max_rank": 3,
                        "cost_per_rank": 1,
                        "effects": ["+10% base damage per rank"],
                        "prerequisites": [],
                        "synergies": [],
                    },
                ],
            },
            {
                "tier": 2,
                "required_points": TIER_REQUIREMENTS[2],
                "nodes": [
                    {
                        "id": f"{class_type.lower()}_t2_power",
                        "name": f"Empowered {class_type} Strike",
                        "type": "active",
                        "description": f"Enhanced combat technique for {class_type}.",
                        "max_rank": 3,
                        "cost_per_rank": 2,
                        "effects": ["+25% skill damage per rank"],
                        "prerequisites": [f"{class_type.lower()}_t1_basic"],
                        "synergies": [],
                    },
                ],
            },
            {
                "tier": 3,
                "required_points": TIER_REQUIREMENTS[3],
                "nodes": [
                    {
                        "id": f"{class_type.lower()}_t3_mastery",
                        "name": f"{class_type} Mastery",
                        "type": "passive",
                        "description": f"Mastery passive for the {class_type} class.",
                        "max_rank": 5,
                        "cost_per_rank": 2,
                        "effects": ["+5% all stats per rank"],
                        "prerequisites": [f"{class_type.lower()}_t2_power"],
                        "synergies": [],
                    },
                ],
            },
            {
                "tier": 4,
                "required_points": TIER_REQUIREMENTS[4],
                "nodes": [
                    {
                        "id": f"{class_type.lower()}_t4_ultimate",
                        "name": f"{class_type} Ultimate",
                        "type": "ultimate",
                        "description": f"The pinnacle ability of the {class_type} class.",
                        "max_rank": 1,
                        "cost_per_rank": 5,
                        "effects": ["Devastating ultimate ability"],
                        "prerequisites": [f"{class_type.lower()}_t3_mastery"],
                        "synergies": [],
                    },
                ],
            },
        ],
    }


# ── LLM prompts ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a Character Progression Designer for Z_Claw, a fantasy-RPG.
Design a skill tree with 3-4 tiers of abilities for a character class.

Return ONLY valid JSON with this structure:
{
  "tree_name": "name of the skill tree",
  "class_type": "class this tree belongs to",
  "max_points": 40,
  "tiers": [
    {
      "tier": 1,
      "required_points": 0,
      "nodes": [
        {
          "id": "unique_snake_case_id",
          "name": "Human-Readable Skill Name",
          "type": "active|passive|toggle|ultimate|aura|buff",
          "description": "1-2 sentence skill description",
          "max_rank": 3,
          "cost_per_rank": 1,
          "effects": ["+10% damage per rank", "additional effect text"],
          "prerequisites": ["node_id_from_lower_tier"],
          "synergies": ["node_id that combos well with this"]
        }
      ]
    }
  ]
}

Design rules:
- Tier 1 (0 points required): 3-5 basic nodes, no prerequisites
- Tier 2 (5 points required): 3-5 intermediate nodes, prerequisite from Tier 1
- Tier 3 (15 points required): 2-4 advanced nodes, prerequisite from Tier 2
- Tier 4 (30 points required): 1-2 ultimate nodes, prerequisite from Tier 3
- Prerequisites MUST only reference nodes from LOWER tiers — never same tier or higher
- Node IDs must be unique snake_case strings
- Each node should feel distinct and encourage different playstyles
- Include at least one synergy pair (nodes that work well together)
- max_rank typically 1-5; cost_per_rank typically 1-3 (ultimates cost 5)

No markdown. No explanation outside the JSON."""


def _build_user_prompt(class_type: str, tree_name: str, prompt: str,
                       char_ctx: str, trees_ctx: str) -> str:
    """Build the user prompt with all context."""
    parts = []

    if char_ctx:
        parts.append(char_ctx)
    if trees_ctx:
        parts.append(trees_ctx)

    parts.append(f"Class type: {class_type}")
    parts.append(f"Skill tree name: {tree_name}")

    if prompt:
        parts.append(f"Design direction: {prompt}")

    parts.append(
        f"Design a {class_type} skill tree with 3-4 tiers. "
        f"Make it feel unique to the {class_type} fantasy — "
        f"abilities should reflect the class identity and offer meaningful build choices."
    )

    return "\n\n".join(parts)


# ── Normalize and validate ───────────────────────────────────────────────────

def _normalize_tree(raw: dict, class_type: str, tree_name: str) -> dict:
    """Ensure all fields exist and have correct types."""
    tree = {
        "tree_name": raw.get("tree_name") or tree_name,
        "class_type": raw.get("class_type") or class_type,
        "max_points": raw.get("max_points", 40) if isinstance(raw.get("max_points"), int) else 40,
        "tiers": [],
    }

    seen_ids: set[str] = set()
    raw_tiers = raw.get("tiers", [])
    if not isinstance(raw_tiers, list):
        raw_tiers = []

    for tier_data in raw_tiers:
        if not isinstance(tier_data, dict):
            continue

        tier_num = tier_data.get("tier", 0)
        if not isinstance(tier_num, int) or tier_num < 1 or tier_num > 4:
            continue

        tier = {
            "tier": tier_num,
            "required_points": TIER_REQUIREMENTS.get(tier_num, 0),
            "nodes": [],
        }

        for node_data in (tier_data.get("nodes") or []):
            if not isinstance(node_data, dict):
                continue

            nid = node_data.get("id", "")
            if not isinstance(nid, str) or not nid:
                # Generate an ID
                nid = f"{class_type.lower()}_t{tier_num}_{len(tier['nodes']) + 1}"

            # Deduplicate
            if nid in seen_ids:
                nid = f"{nid}_{len(seen_ids)}"
            seen_ids.add(nid)

            node_type = node_data.get("type", "active")
            if node_type not in NODE_TYPES:
                node_type = "active"

            node = {
                "id": nid,
                "name": node_data.get("name", f"Skill {nid}"),
                "type": node_type,
                "description": node_data.get("description", ""),
                "max_rank": node_data.get("max_rank", 3) if isinstance(node_data.get("max_rank"), int) else 3,
                "cost_per_rank": node_data.get("cost_per_rank", 1) if isinstance(node_data.get("cost_per_rank"), int) else 1,
                "effects": node_data.get("effects", []) if isinstance(node_data.get("effects"), list) else [],
                "prerequisites": node_data.get("prerequisites", []) if isinstance(node_data.get("prerequisites"), list) else [],
                "synergies": node_data.get("synergies", []) if isinstance(node_data.get("synergies"), list) else [],
            }

            # Ensure effects are all strings
            node["effects"] = [str(e) for e in node["effects"]]
            node["prerequisites"] = [str(p) for p in node["prerequisites"]]
            node["synergies"] = [str(s) for s in node["synergies"]]

            tier["nodes"].append(node)

        if tier["nodes"]:
            tree["tiers"].append(tier)

    # Sort tiers by tier number
    tree["tiers"].sort(key=lambda t: t.get("tier", 0))

    return tree


def _count_nodes(tree: dict) -> int:
    """Total node count across all tiers."""
    return sum(len(t.get("nodes", [])) for t in tree.get("tiers", []))


def _count_total_points(tree: dict) -> int:
    """Total skill points needed to max every node."""
    total = 0
    for tier in tree.get("tiers", []):
        for node in tier.get("nodes", []):
            total += node.get("max_rank", 1) * node.get("cost_per_rank", 1)
    return total


# ── Public entry point ───────────────────────────────────────────────────────

def run(**kwargs) -> dict:
    """
    Design a character skill/progression tree.

    kwargs:
        class_type (str):  Character class (e.g., warrior, mage, rogue). Required.
        tree_name (str):   Name for this skill tree. Defaults to "{class_type} Core".
        prompt (str):      Additional design direction for the LLM.
    """
    SKILL_TREES_DIR.mkdir(parents=True, exist_ok=True)

    class_type = kwargs.get("class_type", "warrior")
    tree_name = kwargs.get("tree_name", f"{class_type.title()} Core")
    prompt = kwargs.get("prompt", "")

    # Gather context
    char_ctx = _load_character_context(class_type)
    trees_ctx = _load_existing_trees_context()

    # ── Check LLM availability ───────────────────────────────────────────
    if not is_available(MODEL, host=OLLAMA_HOST):
        log.info("skill-tree-builder: Ollama unavailable, generating scaffold")
        tree = _scaffold_tree(class_type, tree_name, prompt)
        _save_tree(tree)

        return {
            "status": "degraded",
            "summary": (
                f"Skill tree '{tree_name}' scaffolded for {class_type} (LLM unavailable). "
                f"{_count_nodes(tree)} placeholder nodes across {len(tree['tiers'])} tiers."
            ),
            "skill_tree": tree,
            "metrics": {
                "tree_name": tree_name,
                "class_type": class_type,
                "tiers": len(tree["tiers"]),
                "total_nodes": _count_nodes(tree),
                "total_points_to_max": _count_total_points(tree),
                "circular_deps_found": 0,
                "circular_deps_fixed": 0,
                "model_available": False,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [
                {"priority": "low", "description": f"Review scaffolded skill tree '{tree_name}' — needs real ability design.", "requires_matthew": False},
            ],
        }

    # ── LLM generation ───────────────────────────────────────────────────
    user_prompt = _build_user_prompt(class_type, tree_name, prompt, char_ctx, trees_ctx)

    try:
        raw = chat_json(MODEL, [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ], temperature=0.5, max_tokens=1500, task_type="skill-tree-builder")
    except Exception as e:
        log.error("skill-tree-builder LLM call failed: %s", e)
        tree = _scaffold_tree(class_type, tree_name, prompt)
        _save_tree(tree)
        return {
            "status": "failed",
            "summary": f"Skill tree LLM generation failed ({e}). Scaffold saved for '{tree_name}'.",
            "skill_tree": tree,
            "metrics": {
                "tree_name": tree_name,
                "class_type": class_type,
                "tiers": len(tree["tiers"]),
                "total_nodes": _count_nodes(tree),
                "total_points_to_max": _count_total_points(tree),
                "circular_deps_found": 0,
                "circular_deps_fixed": 0,
                "model_available": True,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # ── Parse and normalize ──────────────────────────────────────────────
    if not isinstance(raw, dict):
        log.warning("skill-tree-builder: LLM returned non-dict (%s), falling back to scaffold", type(raw).__name__)
        tree = _scaffold_tree(class_type, tree_name, prompt)
    else:
        tree = _normalize_tree(raw, class_type, tree_name)

    # If normalization produced an empty tree, fall back
    if not tree["tiers"] or _count_nodes(tree) == 0:
        log.warning("skill-tree-builder: normalization produced empty tree, using scaffold")
        tree = _scaffold_tree(class_type, tree_name, prompt)

    # ── Validate and fix circular dependencies ───────────────────────────
    cycle_errors = _detect_circular_deps(tree["tiers"])
    deps_fixed = 0

    if cycle_errors:
        log.warning("skill-tree-builder: circular dependencies detected: %s", cycle_errors)
        tree["tiers"], deps_fixed = _break_circular_deps(tree["tiers"])

        # Re-check after fixing
        remaining = _detect_circular_deps(tree["tiers"])
        if remaining:
            log.error("skill-tree-builder: could not resolve all circular deps: %s", remaining)

    # ── Persist ──────────────────────────────────────────────────────────
    _save_tree(tree)

    total_nodes = _count_nodes(tree)
    total_points = _count_total_points(tree)

    summary_parts = [
        f"Skill tree '{tree['tree_name']}' generated for {tree['class_type']}:",
        f"{len(tree['tiers'])} tiers, {total_nodes} nodes, {total_points} total points to max.",
    ]
    if deps_fixed > 0:
        summary_parts.append(f"{deps_fixed} circular dependency edge(s) auto-fixed.")

    # Tier breakdown
    for tier in tree["tiers"]:
        tier_num = tier.get("tier", "?")
        node_names = [n.get("name", "?") for n in tier.get("nodes", [])]
        summary_parts.append(f"  Tier {tier_num} ({tier.get('required_points', 0)}pts): {', '.join(node_names)}")

    summary = " ".join(summary_parts[:3]) + "\n" + "\n".join(summary_parts[3:]) if len(summary_parts) > 3 else " ".join(summary_parts)

    escalate = len(cycle_errors) > 0 and deps_fixed == 0
    escalation_reason = "Unresolvable circular dependencies in skill tree" if escalate else ""

    return {
        "status": "success",
        "summary": summary,
        "skill_tree": tree,
        "metrics": {
            "tree_name": tree["tree_name"],
            "class_type": tree["class_type"],
            "tiers": len(tree["tiers"]),
            "total_nodes": total_nodes,
            "total_points_to_max": total_points,
            "max_points": tree["max_points"],
            "circular_deps_found": len(cycle_errors),
            "circular_deps_fixed": deps_fixed,
            "model_available": True,
        },
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }
