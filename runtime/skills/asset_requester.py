"""
asset-requester skill — Generates production asset requests from game designs.
Reads character, enemy, and item designs, then creates structured requests
that Production's art-director and generation skills can consume.
Writes requests to state/gamedev/asset-requests/.
Tier 0 + Tier 1 (Python scan + LLM for prompt crafting).
"""

import json
import logging
from pathlib import Path

from runtime.config import OLLAMA_HOST, MODEL_7B, STATE_DIR
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
MODEL = MODEL_7B
GAMEDEV_DIR = STATE_DIR / "gamedev"
REQUESTS_DIR = GAMEDEV_DIR / "asset-requests"

# Design directories to scan
DESIGN_DIRS = {
    "characters": GAMEDEV_DIR / "characters",
    "enemies": GAMEDEV_DIR / "enemies",
    "items": GAMEDEV_DIR / "items",
}

# Fields that indicate an asset is needed
VISUAL_FIELDS = ("visual_description", "sprite_spec", "appearance", "sprite", "art_notes")
AUDIO_FIELDS = ("sound_effect", "audio_spec", "sfx", "music_theme", "audio_notes")


def _scan_designs(request_type: str) -> list[dict]:
    """
    Scan design directories for entries that need assets.
    Returns a list of raw design entries with their source paths.
    """
    entries = []

    for category, dir_path in DESIGN_DIRS.items():
        if not dir_path.exists():
            continue

        for fpath in sorted(dir_path.glob("*.json")):
            try:
                with open(fpath, encoding="utf-8") as f:
                    design = json.load(f)
            except Exception as e:
                log.warning("Failed to read design file %s: %s", fpath, e)
                continue

            if not isinstance(design, dict):
                continue

            source = f"{category}/{fpath.stem}"
            name = design.get("name", fpath.stem)

            # Check for visual asset needs
            if request_type in ("sprites", "all"):
                for field in VISUAL_FIELDS:
                    value = design.get(field)
                    if value and isinstance(value, str) and value.strip():
                        entries.append({
                            "source": source,
                            "name": name,
                            "category": category,
                            "asset_type": "sprite",
                            "raw_description": value.strip(),
                        })
                        break  # one sprite request per design

            # Check for audio asset needs
            if request_type in ("audio", "all"):
                for field in AUDIO_FIELDS:
                    value = design.get(field)
                    if value and isinstance(value, str) and value.strip():
                        entries.append({
                            "source": source,
                            "name": name,
                            "category": category,
                            "asset_type": "audio",
                            "raw_description": value.strip(),
                        })
                        break  # one audio request per design

    return entries


def _craft_prompt_llm(name: str, raw_description: str, asset_type: str) -> str:
    """Use LLM to craft a production-quality generation prompt from a raw description."""
    if asset_type == "sprite":
        system_prompt = (
            "You are a pixel art prompt engineer. Given a character/item description, "
            "write a concise image generation prompt optimized for pixel art sprite generation. "
            "Include: art style, dimensions, color palette hints, pose/framing. "
            "Output ONLY the prompt text, nothing else."
        )
    else:
        system_prompt = (
            "You are a game audio prompt engineer. Given a sound description, "
            "write a concise audio generation prompt. Include: style, mood, duration hint, "
            "instrumentation or sound type. Output ONLY the prompt text, nothing else."
        )

    user_prompt = f"Name: {name}\nDescription: {raw_description}"

    try:
        response = chat(MODEL, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], temperature=0.3, max_tokens=256, task_type="asset-requester")
        return response.strip()
    except Exception as e:
        log.warning("LLM prompt crafting failed for '%s': %s", name, e)
        return ""


def _craft_prompt_fallback(name: str, raw_description: str, asset_type: str) -> str:
    """Deterministic fallback when LLM is unavailable."""
    if asset_type == "sprite":
        return f"pixel art {name}, 64x64, {raw_description}"
    return f"game {asset_type} for {name}: {raw_description}"


def _determine_priority(category: str, design_entry: dict) -> str:
    """Assign priority based on category and context."""
    # Characters are high priority (player-facing); enemies medium; items normal
    priority_map = {
        "characters": "high",
        "enemies": "medium",
        "items": "normal",
    }
    return priority_map.get(category, "normal")


def _build_requests(entries: list[dict], use_llm: bool) -> list[dict]:
    """Convert raw design entries into structured asset requests."""
    requests = []

    for entry in entries:
        # Craft the generation prompt
        if use_llm:
            prompt = _craft_prompt_llm(
                entry["name"], entry["raw_description"], entry["asset_type"]
            )
        else:
            prompt = ""

        # Fall back to deterministic prompt if LLM failed or unavailable
        if not prompt:
            prompt = _craft_prompt_fallback(
                entry["name"], entry["raw_description"], entry["asset_type"]
            )

        target_skill = "image-generate" if entry["asset_type"] == "sprite" else "audio-generate"
        priority = _determine_priority(entry["category"], entry)

        requests.append({
            "source": entry["source"],
            "asset_type": entry["asset_type"],
            "target_skill": target_skill,
            "prompt": prompt,
            "priority": priority,
            "status": "pending",
        })

    return requests


def _save_requests(requests: list[dict]) -> Path:
    """Save pending requests to the asset-requests directory."""
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = REQUESTS_DIR / "pending.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(requests, f, indent=2, ensure_ascii=False)
    return out_file


def run(**kwargs) -> dict:
    """
    Generate structured asset production requests from game designs.

    kwargs:
        request_type (str): "sprites", "audio", or "all". Default "all".
    """
    GAMEDEV_DIR.mkdir(parents=True, exist_ok=True)

    request_type = kwargs.get("request_type", "all")
    if request_type not in ("sprites", "audio", "all"):
        request_type = "all"

    # Scan design files for assets that need production
    entries = _scan_designs(request_type)

    if not entries:
        return {
            "status": "partial",
            "summary": (
                "No designs with asset descriptions found. "
                "Add visual_description or sprite_spec fields to character/enemy/item designs "
                "in state/gamedev/characters/, enemies/, items/."
            ),
            "requests": [],
            "metrics": {
                "requests_generated": 0,
                "sprites_needed": 0,
                "audio_needed": 0,
                "designs_scanned": 0,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # Determine LLM availability for prompt crafting
    use_llm = is_available(MODEL, host=OLLAMA_HOST)

    # Build structured requests
    requests = _build_requests(entries, use_llm)

    # Save to disk
    _save_requests(requests)

    # Compute metrics
    sprites_needed = sum(1 for r in requests if r["asset_type"] == "sprite")
    audio_needed = sum(1 for r in requests if r["asset_type"] == "audio")
    high_priority = sum(1 for r in requests if r["priority"] == "high")

    # Build summary
    summary_parts = [
        f"Asset requester: generated {len(requests)} request(s) from design files.",
    ]
    if sprites_needed:
        summary_parts.append(f"{sprites_needed} sprite(s) needed.")
    if audio_needed:
        summary_parts.append(f"{audio_needed} audio asset(s) needed.")
    if not use_llm:
        summary_parts.append("LLM unavailable — used fallback prompts.")
    summary_parts.append("Requests saved to asset-requests/pending.json.")

    # Escalate if many high-priority assets are missing
    escalate = high_priority >= 5
    escalation_reason = (
        f"{high_priority} high-priority asset requests pending"
        if escalate else ""
    )

    return {
        "status": "success",
        "summary": " ".join(summary_parts),
        "requests": requests,
        "metrics": {
            "requests_generated": len(requests),
            "sprites_needed": sprites_needed,
            "audio_needed": audio_needed,
            "high_priority": high_priority,
            "designs_scanned": len(entries),
            "llm_used": use_llm,
        },
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": [],
    }
