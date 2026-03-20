"""
CodeGenerator — dev pipeline step 1.
Takes a spec, returns generated code using the configured provider.
Provider chain: ollama:coder-7b → gemini (never Claude by default).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from providers.router import ProviderRouter
from providers.base import ProviderError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert software engineer generating production-quality code.
Generate clean, well-structured code based on the specification.
Return ONLY the code with inline comments where logic is non-obvious.
Do not include markdown code fences or explanation — just the raw code."""


class CodeGenerator:

    def run(
        self,
        description: str,
        language: str = "python",
        existing_code: str = "",
        context: str = "",
    ) -> dict[str, Any]:
        """
        Generate code from a spec.

        Returns:
            {
                "code": str,
                "language": str,
                "spec_ref": str,
                "provider_used": str,
                "status": "success" | "failed",
                "error": str,
            }
        """
        router = ProviderRouter()
        provider = router.get_provider("dev-generate")

        if provider is None or provider.provider_id == "deterministic":
            return {
                "code": "",
                "language": language,
                "spec_ref": description[:80],
                "provider_used": "none",
                "status": "failed",
                "error": "No LLM provider available for code generation",
            }

        user_content = f"Language: {language}\n\nTask: {description}"
        if existing_code:
            user_content += f"\n\nExisting code to modify/extend:\n```\n{existing_code}\n```"
        if context:
            user_content += f"\n\nAdditional context:\n{context}"

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ]

        try:
            code = provider.chat(messages, temperature=0.05, max_tokens=4096)
            log.info("CodeGenerator: generated %d chars via %s", len(code), provider.provider_id)
            return {
                "code": code,
                "language": language,
                "spec_ref": description[:80],
                "provider_used": provider.provider_id,
                "status": "success",
                "error": "",
            }
        except ProviderError as e:
            log.error("CodeGenerator failed: %s", e)
            return {
                "code": "",
                "language": language,
                "spec_ref": description[:80],
                "provider_used": getattr(provider, "provider_id", "unknown"),
                "status": "failed",
                "error": str(e),
            }
