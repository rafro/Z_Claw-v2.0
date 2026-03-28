"""
Voice Director — manages voice line catalog and queues/executes TTS generation.

Maintains pre-written voice lines per commander and line type.  When Coqui TTS
(XTTS v2) is available the skill synthesizes audio immediately; otherwise the
request is saved to state/voice-queue.json for later processing.

TTS backend notes
-----------------
* Coqui XTTS v2 supports CPU inference — required for AMD/DirectML on Windows
  because the library has no DirectML back-end.  Short voice lines (<10 s) are
  fast enough on modern CPUs.
* Voice-cloning reference files live at:
    divisions/production/voice_references/{commander}.wav
  5-30 seconds of clean speech is ideal.
* Generated audio is saved to:
    mobile/assets/generated/voice/{commander}/{queue_id}.wav

Install: pip install TTS
"""

import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from runtime.config import BASE_DIR

log = logging.getLogger(__name__)

QUEUE_FILE    = BASE_DIR / "state" / "voice-queue.json"
VOICE_REF_DIR = BASE_DIR / "divisions" / "production" / "voice_references"
VOICE_OUT_DIR = BASE_DIR / "mobile" / "assets" / "generated" / "voice"

# ---------------------------------------------------------------------------
# Optional TTS import — graceful degradation if library not installed
# ---------------------------------------------------------------------------

try:
    from TTS.api import TTS as _CoquiTTS  # type: ignore
    _TTS_AVAILABLE = True
    log.info("voice_generate: Coqui TTS library loaded successfully.")
except Exception as _tts_import_err:
    _CoquiTTS = None
    _TTS_AVAILABLE = False
    log.info(
        "voice_generate: Coqui TTS not available (%s). Requests will be queued only.",
        _tts_import_err,
    )

# Lazy singleton — created on first synthesis call so startup is not blocked.
_tts_model = None

_XTTS_MODEL_ID   = "tts_models/multilingual/multi-dataset/xtts_v2"
_DEFAULT_LANG    = "en"
_DEFAULT_SPEAKER = "Claribel Dervla"


def _get_tts_model():
    global _tts_model
    if _tts_model is None:
        log.info("voice_generate: loading XTTS v2 model (CPU) — first run may be slow...")
        _tts_model = _CoquiTTS(_XTTS_MODEL_ID, gpu=False)
        log.info("voice_generate: XTTS v2 model ready.")
    return _tts_model


# ---------------------------------------------------------------------------
# Voice Catalog
# ---------------------------------------------------------------------------

VOICE_CATALOG: dict[str, dict[str, list[str]]] = {
    "vael": {
        "greeting": ["The ledger opens. The hunt begins."],
        "attack":   ["Quarry marked.", "Closing in.", "Strike!"],
        "victory":  ["Enemy fallen.", "Ledger complete."],
        "defeat":   ["Cold trail.", "Overextended."],
    },
    "seren": {
        "greeting": ["The pattern is clear."],
        "attack":   ["Verdict incoming.", "Pattern locked."],
        "victory":  ["Silence returns.", "Pattern confirmed."],
        "defeat":   ["Noise overwhelms.", "Signal lost."],
    },
    "kaelen": {
        "greeting": ["The forge doesn't stop."],
        "attack":   ["Building pressure.", "Forging impact!"],
        "victory":  ["System optimized.", "Construct complete."],
        "defeat":   ["Fracture detected.", "Blueprint error."],
    },
    "lyrin": {
        "greeting": ["The flame is tended."],
        "attack":   ["Channeling.", "Light forward!"],
        "victory":  ["Balance restored.", "Healing complete."],
        "defeat":   ["The flame dims.", "Ember fading."],
    },
    "zeth": {
        "greeting": ["The veil holds."],
        "attack":   ["Shadow strike.", "Null engage."],
        "victory":  ["Perimeter secured.", "Silence."],
        "defeat":   ["Breach.", "Retreat."],
    },
    "lyke": {
        "greeting": ["The Forge is lit."],
        "attack":   ["Forging strike!", "Blueprint execute!"],
        "victory":  ["Asset delivered.", "Masterwork."],
        "defeat":   ["Render failed.", "Back to the anvil."],
    },
}

_SUPPORTED_FORMATS = {"wav", "mp3", "ogg", "flac"}


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
    return f"vox-{len(queue) + 1:04d}"


def _select_line(commander: str, line_type: str) -> Optional[str]:
    cmdr = VOICE_CATALOG.get(commander)
    if cmdr is None:
        return None
    lines = cmdr.get(line_type)
    if not lines:
        return None
    return random.choice(lines)


# ---------------------------------------------------------------------------
# Synthesis helpers
# ---------------------------------------------------------------------------

def _ref_wav(commander: str) -> Optional[Path]:
    p = VOICE_REF_DIR / f"{commander}.wav"
    return p if p.exists() else None


def _synthesize(entry: dict) -> Optional[str]:
    if not _TTS_AVAILABLE:
        return None
    try:
        tts       = _get_tts_model()
        commander = entry["commander"]
        text      = entry["text"]
        queue_id  = entry["id"]

        out_dir = VOICE_OUT_DIR / commander
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{queue_id}.wav"

        ref = _ref_wav(commander)
        if ref is not None:
            log.info("voice_generate: synthesizing %s via voice-clone (ref=%s)", queue_id, ref)
            tts.tts_with_vc(
                text=text,
                speaker_wav=str(ref),
                file_path=str(out_path),
            )
        else:
            log.info("voice_generate: synthesizing %s — no ref for '%s', using default speaker", queue_id, commander)
            tts.tts(
                text=text,
                speaker=_DEFAULT_SPEAKER,
                language=_DEFAULT_LANG,
                file_path=str(out_path),
            )

        log.info("voice_generate: wrote %s", out_path)
        return str(out_path)
    except Exception as exc:
        log.error("voice_generate: synthesis failed for %s — %s", entry.get("id"), exc)
        return None


def _process_queue() -> dict:
    queue     = _load_queue()
    processed = succeeded = failed = 0
    paths: list[str] = []

    for entry in queue:
        if entry.get("status") != "queued":
            continue
        processed += 1
        out_path = _synthesize(entry)
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

    if processed > 0:
        _save_queue(queue)

    return {"processed": processed, "succeeded": succeeded, "failed": failed, "paths": paths}


# ---------------------------------------------------------------------------
# Skill entry point
# ---------------------------------------------------------------------------

def run(
    commander: str = "vael",
    line_type: str = "greeting",
    emotion: str = "confident",
    text: str = "",
    voice_id: Optional[str] = None,
    output_format: str = "wav",
) -> dict:
    commander     = commander.lower().strip()
    line_type     = line_type.lower().strip()
    emotion       = emotion.lower().strip()
    output_format = output_format.lower().strip()

    if output_format not in _SUPPORTED_FORMATS:
        return {
            "status":  "failed",
            "summary": f"Unsupported output format '{output_format}'. Supported: {', '.join(sorted(_SUPPORTED_FORMATS))}.",
            "metrics": {}, "action_items": [], "escalate": False,
        }

    if text.strip():
        resolved_text = text.strip()
        source = "custom"
    else:
        resolved_text = _select_line(commander, line_type)
        source = "catalog"
        if resolved_text is None:
            available = sorted(VOICE_CATALOG.keys())
            return {
                "status":  "failed",
                "summary": (
                    f"No catalog entry for commander='{commander}', line_type='{line_type}'. "
                    f"Available commanders: {', '.join(available)}."
                ),
                "metrics": {"commander": commander, "line_type": line_type, "available_commanders": available},
                "action_items": [{"priority": "low", "description": f"Add '{line_type}' lines for commander '{commander}' to VOICE_CATALOG.", "requires_matthew": False}],
                "escalate": False,
            }

    queue    = _load_queue()
    queue_id = _next_queue_id(queue)
    entry = {
        "id":            queue_id,
        "commander":     commander,
        "line_type":     line_type,
        "emotion":       emotion,
        "text":          resolved_text,
        "voice_id":      voice_id,
        "output_format": output_format,
        "status":        "queued",
        "queued_at":     datetime.now(timezone.utc).isoformat(),
    }
    queue.append(entry)
    _save_queue(queue)

    log.info("voice_generate: queued %s — %s/%s [%s] \"%s\"", queue_id, commander, line_type, emotion, resolved_text)

    if _TTS_AVAILABLE:
        synthesis_result = _process_queue()
        queue      = _load_queue()
        this_entry = next((e for e in queue if e["id"] == queue_id), entry)
        succeeded  = synthesis_result["succeeded"]
        failed     = synthesis_result["failed"]
        output_path = this_entry.get("output_path")

        if this_entry.get("status") == "completed" and output_path:
            ref_used   = _ref_wav(commander) is not None
            clone_note = "Voice-cloned from reference file." if ref_used else f"Default speaker used (no reference WAV for '{commander}')."
            return {
                "status":  "success",
                "summary": (
                    f"Voice line synthesized ({queue_id}). Commander: {commander}, type: {line_type}. "
                    f"Text ({source}): \"{resolved_text}\". {clone_note} Output: {output_path}."
                ),
                "metrics": {
                    "queue_id": queue_id, "text": resolved_text, "commander": commander,
                    "line_type": line_type, "emotion": emotion, "source": source,
                    "output_format": output_format, "output_path": output_path,
                    "voice_cloned": ref_used, "batch_succeeded": succeeded, "batch_failed": failed,
                    "queue_depth": len(queue),
                },
                "action_items": [], "escalate": False,
            }
        else:
            return {
                "status":  "failed",
                "summary": f"Voice line queued ({queue_id}) but synthesis failed. Check logs for TTS error details.",
                "metrics": {
                    "queue_id": queue_id, "text": resolved_text, "commander": commander,
                    "line_type": line_type, "emotion": emotion, "source": source,
                    "batch_succeeded": succeeded, "batch_failed": failed, "queue_depth": len(queue),
                },
                "action_items": [{"priority": "medium", "description": "TTS synthesis failed — inspect logs for XTTS error.", "requires_matthew": True}],
                "escalate": False,
            }

    return {
        "status":  "partial",
        "summary": (
            f"Voice line queued ({queue_id}). Commander: {commander}, type: {line_type}, emotion: {emotion}. "
            f"Text ({source}): \"{resolved_text}\". TTS backend not active — install with: pip install TTS"
        ),
        "metrics": {
            "queue_id": queue_id, "text": resolved_text, "commander": commander,
            "line_type": line_type, "emotion": emotion, "source": source,
            "output_format": output_format, "queue_depth": len(queue),
        },
        "action_items": [{"priority": "low", "description": "Install Coqui TTS: pip install TTS", "requires_matthew": True}],
        "escalate": False,
    }
