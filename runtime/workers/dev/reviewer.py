"""
CodeReviewer — dev pipeline step 2.
Reviews generated code for correctness, security, and style.
Provider chain: ollama:coder-7b → gemini.
"""

from __future__ import annotations

import logging
from typing import Any

from providers.router import ProviderRouter
from providers.base import ProviderError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior code reviewer. Review the provided code and return a JSON object.
Be strict but fair. Focus on: logic errors, edge cases, security issues, and obvious style problems.

Return ONLY valid JSON in this exact schema:
{
  "verdict": "pass",
  "issues": [],
  "confidence": 0.85,
  "summary": "Brief review summary"
}

verdict values: "pass" | "fail" | "needs_changes"
issues: list of {"severity": "low|medium|high", "description": "..."}
confidence: 0.0–1.0 (how confident you are in your review)"""


class CodeReviewer:

    def run(self, code: str, language: str = "python", spec: str = "") -> dict[str, Any]:
        """
        Review code. Returns:
        {
            "verdict": "pass" | "fail" | "needs_changes",
            "issues": [{"severity": str, "description": str}],
            "confidence": float,
            "summary": str,
            "provider_used": str,
            "status": "success" | "failed",
        }
        """
        if not code.strip():
            return {
                "verdict": "fail",
                "issues": [{"severity": "high", "description": "No code provided"}],
                "confidence": 1.0,
                "summary": "Empty code — nothing to review",
                "provider_used": "deterministic",
                "status": "success",
            }

        router = ProviderRouter()
        provider = router.get_provider("dev-review")

        if provider is None or provider.provider_id == "deterministic":
            # Minimal static checks without LLM
            issues = self._static_checks(code, language)
            verdict = "fail" if any(i["severity"] == "high" for i in issues) else (
                "needs_changes" if issues else "pass"
            )
            return {
                "verdict": verdict,
                "issues": issues,
                "confidence": 0.4,
                "summary": "Static analysis only — no LLM available",
                "provider_used": "deterministic",
                "status": "success",
            }

        user_content = f"Language: {language}\n"
        if spec:
            user_content += f"Original spec: {spec}\n\n"
        user_content += f"Code to review:\n```{language}\n{code}\n```"

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ]

        try:
            result = provider.chat_json(messages, temperature=0.05)
            result["provider_used"] = provider.provider_id
            result["status"] = "success"
            # Clamp confidence to valid range
            result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0.5))))
            return result
        except (ProviderError, Exception) as e:
            log.error("CodeReviewer failed: %s", e)
            return {
                "verdict": "needs_changes",
                "issues": [],
                "confidence": 0.0,
                "summary": f"Review failed: {e}",
                "provider_used": getattr(provider, "provider_id", "unknown"),
                "status": "failed",
            }

    def _static_checks(self, code: str, language: str) -> list[dict]:
        """Minimal rule-based checks, no LLM."""
        issues = []
        if language == "python":
            if "eval(" in code or "exec(" in code:
                issues.append({"severity": "high", "description": "Use of eval()/exec() detected"})
            if "import os" in code and "os.system(" in code:
                issues.append({"severity": "medium", "description": "os.system() usage — prefer subprocess"})
            if "password" in code.lower() and ("=" in code):
                issues.append({"severity": "medium", "description": "Possible hardcoded credential"})
        return issues
