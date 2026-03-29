"""
refactor-scan skill — Tier 2 LLM (Qwen2.5 14B) with Tier 1 7B fallback.
Weekly scan of the OpenClaw Python runtime for refactoring opportunities.
Flags oversized functions, duplicate logic, naming issues, and anti-patterns.
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from runtime.config import SKILL_MODELS, MODEL_14B_HOST, MODEL_7B, OLLAMA_HOST, ROOT
from runtime.ollama_client import chat_json, is_available

log     = logging.getLogger(__name__)
MODEL   = SKILL_MODELS["refactor-scan"]
HOT_DIR = ROOT / "divisions" / "dev-automation" / "hot"

SCAN_DIRS   = ["runtime"]
EXTENSIONS  = {".py", ".js", ".ts"}
MAX_FILES   = 15
LARGE_FN    = 50    # lines — flag functions larger than this


def _collect_files() -> list[Path]:
    files = []
    for d in SCAN_DIRS:
        p = ROOT / d
        if p.exists():
            for f in p.rglob("*"):
                if f.suffix in EXTENSIONS and f.is_file() and "__pycache__" not in str(f):
                    files.append(f)
    # Prioritize largest files
    return sorted(files, key=lambda f: f.stat().st_size, reverse=True)[:MAX_FILES]


def _file_summary(f: Path) -> dict:
    """Extract structural summary (large blocks) from a source file."""
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {}

    total = len(lines)
    large_blocks = []
    current_name = None
    current_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("def ", "async def ", "class ", "function ", "const ", "export function ")):
            if current_name:
                length = i - current_start
                if length > LARGE_FN:
                    large_blocks.append(f"{current_name[:50]} ({length} lines)")
            current_name  = stripped[:60]
            current_start = i

    if current_name:
        length = total - current_start
        if length > LARGE_FN:
            large_blocks.append(f"{current_name[:50]} ({length} lines)")

    return {
        "path":         str(f.relative_to(ROOT)),
        "total_lines":  total,
        "large_blocks": large_blocks[:5],
    }


def run() -> dict:
    HOT_DIR.mkdir(parents=True, exist_ok=True)

    files     = _collect_files()
    summaries = [s for s in (_file_summary(f) for f in files) if s]

    if is_available(MODEL, host=MODEL_14B_HOST):
        use_model, use_host = MODEL, MODEL_14B_HOST
    elif is_available(MODEL_7B, host=OLLAMA_HOST):
        use_model, use_host = MODEL_7B, OLLAMA_HOST
    else:
        return {
            "status":       "partial",
            "summary":      "No model available for refactor analysis.",
            "findings":     [],
            "files_scanned": len(summaries),
            "model_used":   None,
        }

    file_text = "\n".join(
        f"- {s['path']} ({s['total_lines']} lines)"
        + (f" | Large: {', '.join(s['large_blocks'])}" if s["large_blocks"] else "")
        for s in summaries
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Dev Automation refactor scanner for J_Claw. "
                "Analyze these source files and identify refactoring opportunities. "
                'Return JSON: {"summary": "1-2 sentences", "findings": ['
                '{"file": "", "severity": "high|medium|low", '
                '"type": "duplicate_logic|oversized_function|naming|pattern|other", '
                '"detail": "", "suggestion": ""}]} '
                "Max 8 findings. Return valid JSON only."
            ),
        },
        {
            "role": "user",
            "content": f"Files ({len(summaries)}):\n{file_text}",
        },
    ]

    try:
        result   = chat_json(use_model, messages, host=use_host, temperature=0.1, max_tokens=800, task_type="refactor-scan")
        findings = result.get("findings", []) if isinstance(result, dict) else []
        summary  = result.get("summary", f"Scanned {len(summaries)} files.") if isinstance(result, dict) else f"Scanned {len(summaries)} files."

        today = date.today().isoformat()
        with open(HOT_DIR / f"refactor-scan-{today}.json", "w", encoding="utf-8") as fh:
            json.dump({
                "date":         today,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "summary":      summary,
                "findings":     findings,
            }, fh, indent=2)

        return {
            "status":        "success",
            "summary":       summary,
            "findings":      findings,
            "files_scanned": len(summaries),
            "model_used":    use_model,
        }
    except Exception as e:
        log.error("refactor-scan LLM failed: %s", e)
        return {
            "status":        "failed",
            "summary":       f"Refactor scan failed: {e}",
            "findings":      [],
            "files_scanned": len(summaries),
            "model_used":    use_model,
        }
