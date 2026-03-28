"""
Music Composer — generates music via Meta MusicGen when available locally.

When the transformers library is installed the skill synthesizes audio from
the composition spec's text prompt using facebook/musicgen-medium (~1.5 GB).
On AMD/Windows the model runs on DirectML (torch-directml) if available,
otherwise falls back to CPU.

If the backend is not installed the request is queued to state/music-queue.json
for later processing.

Install:
    pip install transformers accelerate scipy torch-directml
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import BASE_DIR

log = logging.getLogger(__name__)

QUEUE_FILE  = BASE_DIR / "state" / "music-queue.json"
MUSIC_OUT   = BASE_DIR / "mobile" / "assets" / "generated" / "music"

# ---------------------------------------------------------------------------
# Optional backend detection
# ---------------------------------------------------------------------------

_TRANSFORMERS_OK = False
_dml_device      = None

try:
    import transformers as _transformers  # noqa: F401
    _TRANSFORMERS_OK = True
except ImportError:
    pass

if _TRANSFORMERS_OK:
    try:
        import torch_directml as _torch_directml
        _dml_device = _torch_directml.device()
        log.info("music_compose: torch-directml device available.")
    except ImportError:
        log.info("music_compose: torch-directml not found, will use CPU.")

_BACKEND_AVAILABLE = _TRANSFORMERS_OK

# Lazy model singleton
_processor = None
_model     = None


def _load_musicgen():
    global _processor, _model
    if _model is None:
        from transformers import AutoProcessor, MusicgenForConditionalGeneration
        log.info("music_compose: loading facebook/musicgen-medium — first run will download ~1.5 GB...")
        _processor = AutoProcessor.from_pretrained("facebook/musicgen-medium")
        _model     = MusicgenForConditionalGeneration.from_pretrained("facebook/musicgen-medium")
        if _dml_device is not None:
            _model = _model.to(_dml_device)
            log.info("music_compose: model moved to DirectML device.")
        else:
            log.info("music_compose: model running on CPU.")
    return _processor, _model


# ---------------------------------------------------------------------------
# Division music identity
# ---------------------------------------------------------------------------

DIVISION_MUSIC_IDENTITY = {
    "opportunity": {
        "description": "energetic hunting theme, chase motifs, wind instruments, G major",
        "key":         "G major",
        "instruments": ["wind ensemble", "brass stabs", "driving strings"],
        "feel":        "energetic, forward-moving, predatory",
    },
    "trading": {
        "description": "ambient pattern theme, hypnotic, electronic pads, C# minor",
        "key":         "C# minor",
        "instruments": ["electronic pads", "arpeggiated synths", "soft pulses"],
        "feel":        "hypnotic, flowing, calculated",
    },
    "dev_automation": {
        "description": "industrial mechanical, metallic percussion, algorithmic, E minor",
        "key":         "E minor",
        "instruments": ["metallic percussion", "modular synth", "glitch textures"],
        "feel":        "mechanical, precise, algorithmic",
    },
    "personal": {
        "description": "warm organic healing, gentle piano, breathing rhythms, A major",
        "key":         "A major",
        "instruments": ["gentle piano", "soft strings", "breath pads"],
        "feel":        "warm, organic, restorative",
    },
    "op_sec": {
        "description": "tense vigilance, low drones, heartbeat percussion, D# minor",
        "key":         "D# minor",
        "instruments": ["low drones", "heartbeat kicks", "spectral pads"],
        "feel":        "tense, watchful, covert",
    },
    "production": {
        "description": "heroic forge theme, heavy hammer percussion, ascending strings, D major",
        "key":         "D major",
        "instruments": ["heavy percussion", "ascending strings", "anvil hits"],
        "feel":        "heroic, industrious, triumphant",
    },
}

TRACK_TEMPLATES = {
    "main_theme":        {"duration_range": (60, 90),   "loopable": True,  "energy": "medium-high", "notes": "Division signature theme, recognisable motif, loopable"},
    "battle_theme":      {"duration_range": (60, 120),  "loopable": True,  "energy": "high",        "notes": "Intense combat energy, driving rhythm, seamless loop"},
    "boss_battle":       {"duration_range": (90, 150),  "loopable": False, "energy": "very-high",   "notes": "Progressive tension, escalating layers, climactic"},
    "victory_fanfare":   {"duration_range": (10, 30),   "loopable": False, "energy": "high",        "notes": "Triumphant stinger, bright brass, resolving cadence"},
    "defeat_dirge":      {"duration_range": (15, 30),   "loopable": False, "energy": "low",         "notes": "Melancholic, descending lines, fading out"},
    "menu_idle":         {"duration_range": (45, 90),   "loopable": True,  "energy": "low",         "notes": "Ambient, unobtrusive, gentle motion, loopable"},
    "prestige_ceremony": {"duration_range": (30, 60),   "loopable": False, "energy": "medium-high", "notes": "Grandiose, ceremonial brass and timpani, resolving"},
    "chapter_stinger":   {"duration_range": (8, 20),    "loopable": False, "energy": "medium",      "notes": "Ethereal transition hit, shimmering, brief"},
}

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
    QUEUE_FILE.parent.mkdir(exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)


# ---------------------------------------------------------------------------
# Spec builder
# ---------------------------------------------------------------------------

def _build_spec(track_type, division, mood, tempo_bpm, duration_seconds, loop, style_prompt):
    identity = DIVISION_MUSIC_IDENTITY.get(division, DIVISION_MUSIC_IDENTITY["production"])
    template = TRACK_TEMPLATES.get(track_type, TRACK_TEMPLATES["main_theme"])
    dur_min, dur_max   = template["duration_range"]
    clamped_duration   = max(dur_min, min(dur_max, duration_seconds))
    effective_loop     = loop if loop is not None else template["loopable"]
    prompt_parts       = [identity["description"], f"{mood} mood" if mood else "", f"{tempo_bpm} BPM", template["notes"], style_prompt]
    full_prompt        = ", ".join(p for p in prompt_parts if p)
    return {
        "track_type":     track_type,
        "division":       division,
        "prompt":         full_prompt,
        "key":            identity["key"],
        "instruments":    identity["instruments"],
        "feel":           identity["feel"],
        "tempo_bpm":      tempo_bpm,
        "duration_s":     clamped_duration,
        "duration_range": list(template["duration_range"]),
        "loopable":       effective_loop,
        "energy":         template["energy"],
        "mood":           mood,
        "style_prompt":   style_prompt,
    }


# ---------------------------------------------------------------------------
# Generation helpers
# ---------------------------------------------------------------------------

def _generate_audio(entry: dict) -> str | None:
    """Generate audio for one queue entry. Returns output path or None on failure."""
    try:
        import torch
        import numpy as np

        processor, model = _load_musicgen()
        spec     = entry["spec"]
        prompt   = spec["prompt"]
        duration = spec["duration_s"]

        inputs = processor(text=[prompt], padding=True, return_tensors="pt")
        if _dml_device is not None:
            inputs = {k: v.to(_dml_device) for k, v in inputs.items()}

        # MusicGen generates ~50 tokens per second of audio
        max_new_tokens = int(duration * 50)
        with torch.no_grad():
            audio_values = model.generate(**inputs, max_new_tokens=max_new_tokens)

        # audio_values shape: [batch, channels, samples]
        audio_np = audio_values[0, 0].cpu().numpy()

        # Normalise to int16
        audio_norm = audio_np / (np.abs(audio_np).max() + 1e-8)
        audio_int16 = (audio_norm * 32767).astype(np.int16)

        sampling_rate = model.config.audio_encoder.sampling_rate

        division  = spec.get("division", "production")
        queue_id  = entry["id"]
        out_dir   = MUSIC_OUT / division
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path  = out_dir / f"{queue_id}.wav"

        import scipy.io.wavfile
        scipy.io.wavfile.write(str(out_path), sampling_rate, audio_int16)
        log.info("music_compose: wrote %s", out_path)
        return str(out_path)

    except Exception as exc:
        log.error("music_compose: generation failed for %s — %s", entry.get("id"), exc)
        return None


def _process_queued_entries(queue: list) -> dict:
    """Flush all 'queued' entries through MusicGen. Mutates queue in place."""
    succeeded = failed = 0
    paths: list[str] = []
    for entry in queue:
        if entry.get("status") != "queued":
            continue
        out_path = _generate_audio(entry)
        if out_path:
            entry["status"]       = "completed"
            entry["output_path"]  = out_path
            entry["completed_at"] = datetime.now(timezone.utc).isoformat()
            succeeded += 1
            paths.append(out_path)
        else:
            entry["status"]    = "failed"
            entry["failed_at"] = datetime.now(timezone.utc).isoformat()
            failed += 1
    return {"succeeded": succeeded, "failed": failed, "paths": paths}


# ---------------------------------------------------------------------------
# Skill entry point
# ---------------------------------------------------------------------------

def run(
    track_type: str = "main_theme",
    division: str = "production",
    mood: str = "epic",
    tempo_bpm: int = 120,
    duration_seconds: int = 60,
    loop: bool = True,
    style_prompt: str = "",
) -> dict:
    if track_type not in TRACK_TEMPLATES:
        valid = ", ".join(sorted(TRACK_TEMPLATES))
        return {"status": "failed", "summary": f"Unknown track_type '{track_type}'. Valid types: {valid}", "metrics": {}, "action_items": [], "escalate": False}

    if division not in DIVISION_MUSIC_IDENTITY:
        valid = ", ".join(sorted(DIVISION_MUSIC_IDENTITY))
        return {"status": "failed", "summary": f"Unknown division '{division}'. Valid divisions: {valid}", "metrics": {}, "action_items": [], "escalate": False}

    spec  = _build_spec(track_type, division, mood, tempo_bpm, duration_seconds, loop, style_prompt)
    queue = _load_queue()

    # First flush any pre-existing queued entries
    if _BACKEND_AVAILABLE and queue:
        pre_result = _process_queued_entries(queue)
        if pre_result["succeeded"] + pre_result["failed"] > 0:
            _save_queue(queue)

    # Append new request
    queue_id = f"mus-{len(queue)+1:04d}"
    entry = {
        "id":        queue_id,
        "spec":      spec,
        "status":    "queued",
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    queue.append(entry)

    # Attempt immediate generation
    if _BACKEND_AVAILABLE:
        out_path = _generate_audio(entry)
        if out_path:
            entry["status"]       = "completed"
            entry["output_path"]  = out_path
            entry["completed_at"] = datetime.now(timezone.utc).isoformat()
            _save_queue(queue)
            log.info("music_compose: queued and generated %s [%s/%s] %sBPM %ss", queue_id, division, track_type, tempo_bpm, spec["duration_s"])
            return {
                "status":  "success",
                "summary": f"Music generated ({queue_id}): {division}/{track_type}, {mood} mood, {tempo_bpm} BPM, {spec['duration_s']}s. Output: {out_path}",
                "metrics": {"queue_id": queue_id, "spec": spec, "output_path": out_path, "queue_depth": len(queue)},
                "action_items": [],
                "escalate": False,
            }
        else:
            entry["status"]    = "failed"
            entry["failed_at"] = datetime.now(timezone.utc).isoformat()
            _save_queue(queue)
            return {
                "status":  "failed",
                "summary": f"Music generation failed ({queue_id}). Check logs for MusicGen error.",
                "metrics": {"queue_id": queue_id, "spec": spec, "queue_depth": len(queue)},
                "action_items": [{"priority": "medium", "description": "MusicGen generation failed — check logs.", "requires_matthew": True}],
                "escalate": False,
            }

    _save_queue(queue)
    log.info("music_compose: queued %s [%s/%s] %sBPM %ss -> %s (backend not active)", queue_id, division, track_type, tempo_bpm, spec["duration_s"], mood)
    return {
        "status":  "partial",
        "summary": (
            f"Music request queued ({queue_id}): {division}/{track_type}, {mood} mood, {tempo_bpm} BPM, {spec['duration_s']}s"
            f"{', loopable' if spec['loopable'] else ''}. "
            "Backend not active — install: pip install transformers accelerate scipy torch-directml"
        ),
        "metrics": {"queue_id": queue_id, "spec": spec, "queue_depth": len(queue)},
        "action_items": [{"priority": "low", "description": "Install MusicGen backend: pip install transformers accelerate scipy torch-directml", "requires_matthew": True}],
        "escalate": False,
    }
