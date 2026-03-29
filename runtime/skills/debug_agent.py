"""
debug-agent skill — Tier 2 LLM (Qwen2.5 14B) with Tier 1 7B fallback.
On-demand root cause analysis for errors, stack traces, and failing code.
"""

import logging
from pathlib import Path

from runtime.config import SKILL_MODELS, MODEL_14B_HOST, MODEL_7B, OLLAMA_HOST
from runtime.ollama_client import chat_json, is_available

log   = logging.getLogger(__name__)
MODEL = SKILL_MODELS["debug-agent"]

DEBUG_PROMPT = """\
You are the Debug Agent for J_Claw's Dev Automation Division.
Analyze the error and return a JSON object:
{
  "root_cause": "concise explanation of what caused this error",
  "file_location": "file and line number if identifiable, else null",
  "suggested_fix": "specific code change or steps to resolve",
  "confidence": "high | medium | low",
  "additional_context": "any related patterns or secondary issues noticed"
}
Return ONLY valid JSON. No markdown, no explanation.\
"""


def run(error_text: str, context_files: list[str] | None = None) -> dict:
    """
    Analyze an error or stack trace.
    error_text:    raw error/stack trace text from Matthew or a log file.
    context_files: optional list of source file paths to include as context.
    """
    context = error_text.strip()

    # Append source file snippets (first 100 lines each, max 3 files)
    if context_files:
        for fpath in context_files[:3]:
            try:
                lines = Path(fpath).read_text(encoding="utf-8", errors="replace").splitlines()
                snippet = "\n".join(lines[:100])
                context += f"\n\n--- {fpath} (first {min(100, len(lines))} lines) ---\n{snippet}"
            except Exception as e:
                log.warning("debug-agent: could not read %s: %s", fpath, e)

    # Choose model: Tier 2 → Tier 1 fallback
    if is_available(MODEL, host=MODEL_14B_HOST):
        use_model, use_host, tier = MODEL, MODEL_14B_HOST, "tier2"
    elif is_available(MODEL_7B, host=OLLAMA_HOST):
        log.info("debug-agent: 14B unavailable, falling back to 7B")
        use_model, use_host, tier = MODEL_7B, OLLAMA_HOST, "tier1_fallback"
    else:
        return {
            "status":             "partial",
            "root_cause":         "No LLM model available for debug analysis.",
            "file_location":      None,
            "suggested_fix":      "Ensure Ollama is running with a model available.",
            "confidence":         "low",
            "additional_context": "",
            "model_used":         None,
            "tier":               "none",
        }

    messages = [
        {"role": "system", "content": DEBUG_PROMPT},
        {"role": "user",   "content": f"Error to analyze:\n\n{context[:4000]}"},
    ]

    try:
        result = chat_json(use_model, messages, host=use_host,
                           temperature=0.1, max_tokens=600, task_type="debug-agent")
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response type: {type(result)}")
        return {
            "status":             "success",
            "root_cause":         result.get("root_cause", ""),
            "file_location":      result.get("file_location"),
            "suggested_fix":      result.get("suggested_fix", ""),
            "confidence":         result.get("confidence", "medium"),
            "additional_context": result.get("additional_context", ""),
            "model_used":         use_model,
            "tier":               tier,
        }
    except Exception as e:
        log.error("debug-agent LLM failed: %s", e)
        return {
            "status":             "failed",
            "root_cause":         f"LLM analysis failed: {e}",
            "file_location":      None,
            "suggested_fix":      "",
            "confidence":         "low",
            "additional_context": "",
            "model_used":         use_model,
            "tier":               tier,
        }
