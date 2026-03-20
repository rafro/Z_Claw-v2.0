"""
DevSummarizer — dev pipeline step 4.
Condenses pipeline results into a human-readable summary.
Provider chain: ollama:7b → gemini.
"""

from __future__ import annotations

import logging
from typing import Any

from providers.router import ProviderRouter
from providers.base import ProviderError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are summarizing a code generation pipeline result for a developer.
Be concise and direct. Highlight: what was built, any issues found, and recommended next steps.
Keep to 3–5 sentences. No markdown headers."""


class DevSummarizer:

    def run(
        self,
        spec: str,
        generator_result: dict,
        reviewer_result: dict,
        tester_result: dict,
    ) -> dict[str, Any]:
        """
        Produce a human-readable summary of the pipeline.

        Returns:
        {
            "summary": str,
            "key_issues": [str],
            "recommended_next_steps": [str],
            "overall_confidence": float,
            "provider_used": str,
            "status": "success" | "failed",
        }
        """
        # Compute overall confidence from reviewer + tester
        rev_confidence = float(reviewer_result.get("confidence", 0.5))
        syntax_ok = tester_result.get("syntax_ok", False)
        test_pass = tester_result.get("failed", 0) == 0
        overall_confidence = round(
            rev_confidence * 0.6 + (0.2 if syntax_ok else 0.0) + (0.2 if test_pass else 0.0),
            2,
        )

        issues = [
            i.get("description", "")
            for i in reviewer_result.get("issues", [])
            if i.get("severity") in ("medium", "high")
        ]
        issues += reviewer_result.get("errors", [])

        # Try LLM summary first
        router = ProviderRouter()
        provider = router.get_provider("dev-summarize")

        if provider and provider.provider_id != "deterministic":
            context = (
                f"Spec: {spec}\n"
                f"Generator: {generator_result.get('status')} "
                f"({len(generator_result.get('code', ''))} chars, {generator_result.get('language')})\n"
                f"Reviewer verdict: {reviewer_result.get('verdict')} "
                f"(confidence {rev_confidence:.2f}) — issues: {len(reviewer_result.get('issues', []))}\n"
                f"Tester: syntax={'ok' if syntax_ok else 'ERROR'} "
                f"tests={tester_result.get('passed', 0)}/{tester_result.get('tests_run', 0)} passed\n"
                f"Key issues: {'; '.join(issues[:5]) or 'none'}"
            )
            try:
                summary = provider.chat(
                    [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": context}],
                    temperature=0.2, max_tokens=300,
                )
                provider_used = provider.provider_id
            except ProviderError as e:
                log.warning("DevSummarizer LLM failed, using fallback: %s", e)
                summary = self._fallback_summary(
                    spec, generator_result, reviewer_result, tester_result
                )
                provider_used = "deterministic"
        else:
            summary = self._fallback_summary(spec, generator_result, reviewer_result, tester_result)
            provider_used = "deterministic"

        next_steps = []
        if reviewer_result.get("verdict") in ("fail", "needs_changes"):
            next_steps.append("Address reviewer issues before proceeding")
        if not syntax_ok:
            next_steps.append("Fix syntax errors in generated code")
        if not next_steps:
            next_steps.append("Review code and approve when ready")

        return {
            "summary": summary,
            "key_issues": issues[:5],
            "recommended_next_steps": next_steps,
            "overall_confidence": overall_confidence,
            "provider_used": provider_used,
            "status": "success",
        }

    def _fallback_summary(
        self,
        spec: str,
        gen: dict,
        rev: dict,
        test: dict,
    ) -> str:
        lines = [f"Generated {gen.get('language', 'code')} for: {spec[:60]}."]
        verdict = rev.get("verdict", "unknown")
        lines.append(f"Review verdict: {verdict} ({len(rev.get('issues', []))} issues).")
        if not test.get("syntax_ok"):
            lines.append("Syntax check FAILED.")
        else:
            lines.append("Syntax OK.")
        return " ".join(lines)
