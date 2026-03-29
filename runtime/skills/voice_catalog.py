"""
Voice Catalog — Tier 0 (pure Python) skill that inventories the state of
voice references, catalog lines, and synthesized output across all commanders.

No external dependencies.  Scans the filesystem and cross-references against
the VOICE_CATALOG defined in voice_generate.py.
"""

import logging
from pathlib import Path

from runtime.config import BASE_DIR
from runtime.skills.voice_generate import VOICE_CATALOG, VOICE_REF_DIR, VOICE_OUT_DIR

log = logging.getLogger(__name__)

# The six canonical commanders
COMMANDERS: dict[str, dict[str, str]] = {
    "vael":   {"division": "opportunity",     "voice_character": "Young ranger, alert"},
    "seren":  {"division": "trading",         "voice_character": "Calm oracle"},
    "kaelen": {"division": "dev-automation",  "voice_character": "Gruff forge knight"},
    "lyrin":  {"division": "personal",        "voice_character": "Gentle cleric"},
    "zeth":   {"division": "op-sec",          "voice_character": "Shadow assassin"},
    "lyke":   {"division": "production",      "voice_character": "Master artificer"},
}


def _count_catalog_lines(commander: str) -> int:
    """Count total voice lines defined in VOICE_CATALOG for a commander."""
    cmdr_lines = VOICE_CATALOG.get(commander, {})
    return sum(len(lines) for lines in cmdr_lines.values())


def _count_synthesized(commander: str) -> int:
    """Count already-generated WAV files in mobile/assets/generated/voice/{commander}/."""
    out_dir = VOICE_OUT_DIR / commander
    if not out_dir.exists():
        return 0
    return sum(1 for f in out_dir.iterdir() if f.is_file() and f.suffix.lower() == ".wav")


def _scan_synthesized_files(commander: str) -> list[str]:
    """Return relative paths of synthesized files for a commander."""
    out_dir = VOICE_OUT_DIR / commander
    if not out_dir.exists():
        return []
    files = []
    for f in sorted(out_dir.iterdir()):
        if f.is_file() and f.suffix.lower() == ".wav":
            files.append(str(f.relative_to(BASE_DIR)).replace("\\", "/"))
    return files


def run() -> dict:
    """Voice Catalog skill entry point — returns comprehensive voice system status."""
    commanders_detail: dict[str, dict] = {}
    total_lines = 0
    total_synthesized = 0
    with_reference = 0
    missing_references: list[str] = []

    for name, meta in COMMANDERS.items():
        ref_path = VOICE_REF_DIR / f"{name}.wav"
        has_ref = ref_path.exists()
        lines_available = _count_catalog_lines(name)
        lines_synthesized = _count_synthesized(name)
        synth_files = _scan_synthesized_files(name)

        if has_ref:
            with_reference += 1
        else:
            missing_references.append(name)

        total_lines += lines_available
        total_synthesized += lines_synthesized

        commanders_detail[name] = {
            "division":         meta["division"],
            "voice_character":  meta["voice_character"],
            "has_reference":    has_ref,
            "reference_file":   f"divisions/production/voice_references/{name}.wav" if has_ref else None,
            "lines_available":  lines_available,
            "lines_synthesized": lines_synthesized,
            "synthesized_files": synth_files,
        }

    coverage_pct = round((total_synthesized / total_lines * 100), 1) if total_lines > 0 else 0.0

    metrics = {
        "total_commanders":   len(COMMANDERS),
        "with_reference":     with_reference,
        "total_lines":        total_lines,
        "synthesized_lines":  total_synthesized,
        "coverage_pct":       coverage_pct,
    }

    # Build summary
    ref_status = f"{with_reference}/{len(COMMANDERS)} commanders have voice references"
    synth_status = f"{total_synthesized}/{total_lines} lines synthesized ({coverage_pct}% coverage)"
    missing_note = ""
    if missing_references:
        missing_note = f" Missing references: {', '.join(missing_references)}."

    summary = f"Voice catalog scanned. {ref_status}. {synth_status}.{missing_note}"

    log.info("voice_catalog: %s", summary)

    return {
        "status":       "success",
        "summary":      summary,
        "commanders":   commanders_detail,
        "metrics":      metrics,
        "action_items": [{
            "priority":        "low",
            "description":     f"Record voice references for: {', '.join(missing_references)}",
            "requires_matthew": True,
        }] if missing_references else [],
        "escalate":     False,
    }
