"""
SFX Generator — produces Web Audio API synthesis specifications for game sound effects.

Tier 0 skill: pure Python dict construction, NO LLM calls, NO external dependencies.

The mobile PWA uses Web Audio API for battle and UI sounds.  This skill generates
JSON specs that the frontend OscillatorNode / GainNode / BiquadFilterNode chain
can consume directly.

Each SFX type defines:
  - oscillator type (sine / square / sawtooth / triangle)
  - frequency envelope (start_hz, end_hz, duration_ms)
  - gain envelope (attack_ms, decay_ms, sustain_level, release_ms)
  - optional noise layer (white / pink, mix 0-1)
  - optional filter (lowpass / highpass, cutoff_hz)

Variation parameter (0-3) applies slight randomization to frequency and timing
so repeated plays don't sound identical.

Queue file: state/sfx-queue.json (same pattern as voice-queue.json).
"""

import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import BASE_DIR

log = logging.getLogger(__name__)

QUEUE_FILE = BASE_DIR / "state" / "sfx-queue.json"

# ---------------------------------------------------------------------------
# SFX Catalog — Web Audio API synthesis specifications
# ---------------------------------------------------------------------------

SFX_CATALOG: dict[str, dict] = {
    "sword_slash": {
        "oscillator": "sawtooth",
        "frequency": {"start_hz": 800, "end_hz": 200, "duration_ms": 120},
        "gain": {"attack_ms": 5, "decay_ms": 80, "sustain_level": 0.0, "release_ms": 35},
        "noise": {"type": "white", "mix": 0.6},
        "filter": {"type": "highpass", "cutoff_hz": 400},
    },
    "magic_cast": {
        "oscillator": "sine",
        "frequency": {"start_hz": 300, "end_hz": 1200, "duration_ms": 400},
        "gain": {"attack_ms": 30, "decay_ms": 200, "sustain_level": 0.3, "release_ms": 170},
        "noise": {"type": "pink", "mix": 0.15},
        "filter": {"type": "lowpass", "cutoff_hz": 3000},
    },
    "shield_block": {
        "oscillator": "square",
        "frequency": {"start_hz": 600, "end_hz": 150, "duration_ms": 80},
        "gain": {"attack_ms": 2, "decay_ms": 50, "sustain_level": 0.0, "release_ms": 28},
        "noise": {"type": "white", "mix": 0.7},
        "filter": {"type": "lowpass", "cutoff_hz": 1500},
    },
    "critical_hit": {
        "oscillator": "sawtooth",
        "frequency": {"start_hz": 1200, "end_hz": 100, "duration_ms": 200},
        "gain": {"attack_ms": 2, "decay_ms": 100, "sustain_level": 0.1, "release_ms": 98},
        "noise": {"type": "white", "mix": 0.8},
        "filter": {"type": "highpass", "cutoff_hz": 300},
    },
    "dodge": {
        "oscillator": "sine",
        "frequency": {"start_hz": 500, "end_hz": 1500, "duration_ms": 100},
        "gain": {"attack_ms": 5, "decay_ms": 60, "sustain_level": 0.0, "release_ms": 35},
        "noise": None,
        "filter": {"type": "highpass", "cutoff_hz": 800},
    },
    "heal": {
        "oscillator": "sine",
        "frequency": {"start_hz": 400, "end_hz": 800, "duration_ms": 500},
        "gain": {"attack_ms": 50, "decay_ms": 250, "sustain_level": 0.4, "release_ms": 200},
        "noise": {"type": "pink", "mix": 0.1},
        "filter": {"type": "lowpass", "cutoff_hz": 2000},
    },
    "level_up": {
        "oscillator": "triangle",
        "frequency": {"start_hz": 400, "end_hz": 1600, "duration_ms": 600},
        "gain": {"attack_ms": 20, "decay_ms": 200, "sustain_level": 0.5, "release_ms": 380},
        "noise": None,
        "filter": {"type": "lowpass", "cutoff_hz": 4000},
    },
    "achievement": {
        "oscillator": "triangle",
        "frequency": {"start_hz": 600, "end_hz": 1200, "duration_ms": 450},
        "gain": {"attack_ms": 10, "decay_ms": 150, "sustain_level": 0.4, "release_ms": 290},
        "noise": {"type": "pink", "mix": 0.05},
        "filter": {"type": "lowpass", "cutoff_hz": 5000},
    },
    "defeat": {
        "oscillator": "sawtooth",
        "frequency": {"start_hz": 400, "end_hz": 80, "duration_ms": 800},
        "gain": {"attack_ms": 10, "decay_ms": 400, "sustain_level": 0.2, "release_ms": 390},
        "noise": {"type": "pink", "mix": 0.3},
        "filter": {"type": "lowpass", "cutoff_hz": 800},
    },
    "victory_fanfare": {
        "oscillator": "triangle",
        "frequency": {"start_hz": 500, "end_hz": 2000, "duration_ms": 700},
        "gain": {"attack_ms": 15, "decay_ms": 250, "sustain_level": 0.6, "release_ms": 435},
        "noise": None,
        "filter": {"type": "lowpass", "cutoff_hz": 6000},
    },
    "menu_select": {
        "oscillator": "sine",
        "frequency": {"start_hz": 800, "end_hz": 1000, "duration_ms": 60},
        "gain": {"attack_ms": 2, "decay_ms": 30, "sustain_level": 0.0, "release_ms": 28},
        "noise": None,
        "filter": None,
    },
    "menu_back": {
        "oscillator": "sine",
        "frequency": {"start_hz": 1000, "end_hz": 600, "duration_ms": 80},
        "gain": {"attack_ms": 2, "decay_ms": 40, "sustain_level": 0.0, "release_ms": 38},
        "noise": None,
        "filter": None,
    },
    "xp_gain": {
        "oscillator": "triangle",
        "frequency": {"start_hz": 600, "end_hz": 900, "duration_ms": 150},
        "gain": {"attack_ms": 5, "decay_ms": 80, "sustain_level": 0.1, "release_ms": 65},
        "noise": None,
        "filter": {"type": "lowpass", "cutoff_hz": 3500},
    },
    "combo_hit": {
        "oscillator": "square",
        "frequency": {"start_hz": 900, "end_hz": 300, "duration_ms": 100},
        "gain": {"attack_ms": 2, "decay_ms": 60, "sustain_level": 0.0, "release_ms": 38},
        "noise": {"type": "white", "mix": 0.5},
        "filter": {"type": "highpass", "cutoff_hz": 500},
    },
    "boss_appear": {
        "oscillator": "sawtooth",
        "frequency": {"start_hz": 100, "end_hz": 60, "duration_ms": 1200},
        "gain": {"attack_ms": 100, "decay_ms": 600, "sustain_level": 0.7, "release_ms": 500},
        "noise": {"type": "pink", "mix": 0.4},
        "filter": {"type": "lowpass", "cutoff_hz": 500},
    },
}

SFX_TYPES = sorted(SFX_CATALOG.keys())

# ---------------------------------------------------------------------------
# Variation helpers
# ---------------------------------------------------------------------------

# Variation seeds per slot — deterministic but distinct offsets
_VARIATION_SEEDS = [0, 17, 37, 53]


def _apply_variation(spec: dict, variation: int) -> dict:
    """Return a copy of *spec* with slight randomization based on *variation*.

    Variation 0 returns the base spec unchanged.  Variations 1-3 nudge
    frequency endpoints by +/-10 % and timing values by +/-8 %.
    """
    if variation == 0:
        return spec  # canonical, no copy needed

    seed = _VARIATION_SEEDS[variation]
    rng = random.Random(seed)

    def _jitter_freq(val: int | float) -> int:
        factor = 1.0 + rng.uniform(-0.10, 0.10)
        return max(20, int(val * factor))

    def _jitter_time(val: int | float) -> int:
        factor = 1.0 + rng.uniform(-0.08, 0.08)
        return max(1, int(val * factor))

    out = dict(spec)

    # Frequency envelope
    freq = dict(spec["frequency"])
    freq["start_hz"] = _jitter_freq(freq["start_hz"])
    freq["end_hz"] = _jitter_freq(freq["end_hz"])
    freq["duration_ms"] = _jitter_time(freq["duration_ms"])
    out["frequency"] = freq

    # Gain envelope
    gain = dict(spec["gain"])
    gain["attack_ms"] = _jitter_time(gain["attack_ms"])
    gain["decay_ms"] = _jitter_time(gain["decay_ms"])
    gain["release_ms"] = _jitter_time(gain["release_ms"])
    # sustain_level: nudge by +/-5%
    sl = gain["sustain_level"]
    sl = max(0.0, min(1.0, sl + rng.uniform(-0.05, 0.05)))
    gain["sustain_level"] = round(sl, 3)
    out["gain"] = gain

    # Noise mix jitter
    if spec.get("noise") is not None:
        noise = dict(spec["noise"])
        nmix = noise["mix"]
        nmix = max(0.0, min(1.0, nmix + rng.uniform(-0.05, 0.05)))
        noise["mix"] = round(nmix, 3)
        out["noise"] = noise

    # Filter cutoff jitter
    if spec.get("filter") is not None:
        filt = dict(spec["filter"])
        filt["cutoff_hz"] = _jitter_freq(filt["cutoff_hz"])
        out["filter"] = filt

    return out


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def _load_queue() -> list:
    if not QUEUE_FILE.exists():
        return []
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_queue(queue: list) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)


def _next_queue_id(queue: list) -> str:
    return f"sfx-{len(queue) + 1:04d}"


# ---------------------------------------------------------------------------
# Skill entry point
# ---------------------------------------------------------------------------

def run(
    sfx_type: str = "sword_slash",
    variation: int = 0,
) -> dict:
    """Generate Web Audio API SFX synthesis spec(s).

    Parameters
    ----------
    sfx_type : str
        One of the SFX_TYPES keys, or ``"batch"`` to return all specs
        (used by the frontend for preloading).
    variation : int
        0-3.  Slot 0 is the canonical spec; 1-3 apply slight randomization.

    Returns
    -------
    dict
        Standard skill result with ``status``, ``sfx_spec`` (or ``sfx_specs``
        for batch), ``sfx_type``, ``variation``, and ``metrics``.
    """
    sfx_type = sfx_type.lower().strip()
    variation = max(0, min(3, int(variation)))

    # ── Batch mode — return all specs for preloading ─────────────────────
    if sfx_type == "batch":
        all_specs: dict[str, dict] = {}
        for stype in SFX_TYPES:
            all_specs[stype] = _apply_variation(dict(SFX_CATALOG[stype]), variation)

        # Queue the batch request for telemetry
        queue = _load_queue()
        queue_id = _next_queue_id(queue)
        entry = {
            "id": queue_id,
            "sfx_type": "batch",
            "variation": variation,
            "status": "completed",
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        queue.append(entry)
        _save_queue(queue)

        log.info("sfx_generate: batch — %d specs generated (variation=%d)", len(all_specs), variation)

        return {
            "status": "success",
            "summary": f"Batch SFX specs generated ({queue_id}): {len(all_specs)} types, variation {variation}.",
            "sfx_specs": all_specs,
            "sfx_type": "batch",
            "variation": variation,
            "metrics": {
                "queue_id": queue_id,
                "types_available": len(SFX_TYPES),
                "specs_generated": len(all_specs),
            },
            "action_items": [],
            "escalate": False,
        }

    # ── Single SFX mode ──────────────────────────────────────────────────
    if sfx_type not in SFX_CATALOG:
        return {
            "status": "failed",
            "summary": (
                f"Unknown SFX type '{sfx_type}'. "
                f"Available types: {', '.join(SFX_TYPES)}."
            ),
            "sfx_spec": None,
            "sfx_type": sfx_type,
            "variation": variation,
            "metrics": {
                "types_available": len(SFX_TYPES),
                "specs_generated": 0,
            },
            "action_items": [],
            "escalate": False,
        }

    base_spec = dict(SFX_CATALOG[sfx_type])
    spec = _apply_variation(base_spec, variation)

    # Queue the request
    queue = _load_queue()
    queue_id = _next_queue_id(queue)
    entry = {
        "id": queue_id,
        "sfx_type": sfx_type,
        "variation": variation,
        "status": "completed",
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    queue.append(entry)
    _save_queue(queue)

    log.info("sfx_generate: %s (variation=%d) -> %s", sfx_type, variation, queue_id)

    return {
        "status": "success",
        "summary": (
            f"SFX spec generated ({queue_id}): {sfx_type}, variation {variation}. "
            f"Oscillator: {spec['oscillator']}, "
            f"freq {spec['frequency']['start_hz']}->{spec['frequency']['end_hz']} Hz, "
            f"duration {spec['frequency']['duration_ms']} ms."
        ),
        "sfx_spec": spec,
        "sfx_type": sfx_type,
        "variation": variation,
        "metrics": {
            "queue_id": queue_id,
            "types_available": len(SFX_TYPES),
            "specs_generated": 1,
        },
        "action_items": [],
        "escalate": False,
    }
