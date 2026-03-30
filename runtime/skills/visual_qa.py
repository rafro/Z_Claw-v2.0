"""
visual-qa skill — Screenshot capture and vision-model analysis.
Captures screenshots during game execution, sends to vision-capable LLM
(Claude API or Gemini) for analysis. Fully optional — degrades gracefully
if no vision provider is available.
Tier 4 (Claude/Gemini vision — cloud API required).
"""

import base64
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"
SCREENSHOTS_DIR = GAMEDEV_DIR / "screenshots"

# Vision analysis prompts
VISION_PROMPTS = [
    "Describe what you see in this game screenshot.",
    (
        "Are there any visual issues: overlapping sprites, misaligned UI, "
        "rendering artifacts, color problems?"
    ),
    "Does the visual style appear consistent?",
    "Rate the visual quality 1-10 and explain.",
]

COMBINED_PROMPT = (
    "Analyze this game screenshot. Provide a JSON response with these fields:\n"
    '- "description": Brief description of what you see.\n'
    '- "issues": List of visual issues found (overlapping sprites, misaligned UI, '
    "rendering artifacts, color problems, inconsistent style). Empty list if none.\n"
    '- "score": Integer 1-10 rating of visual quality.\n'
    '- "score_explanation": One sentence explaining the score.\n\n'
    "Respond ONLY with valid JSON, no markdown fences."
)


# ---------------------------------------------------------------------------
# Vision provider helpers
# ---------------------------------------------------------------------------

def _get_vision_provider() -> tuple[str | None, str | None]:
    """
    Determine which vision API is available.
    Returns (provider_name, api_key) or (None, None).
    """
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if anthropic_key:
        return "claude", anthropic_key

    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        return "gemini", gemini_key

    return None, None


def _load_image_base64(path: Path) -> str | None:
    """Load an image file and return its base64-encoded content."""
    try:
        data = path.read_bytes()
        return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        log.warning("Failed to read image %s: %s", path, e)
        return None


def _detect_media_type(path: Path) -> str:
    """Detect image media type from file extension."""
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(suffix, "image/png")


def _call_claude_vision(api_key: str, base64_image: str, media_type: str, prompt: str) -> dict:
    """Send an image to Claude vision API and return parsed response."""
    import requests

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64_image,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        # Extract text from Claude response
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        return _parse_vision_response(text)

    except Exception as e:
        log.error("Claude vision API call failed: %s", e)
        return {"description": f"API error: {e}", "issues": [], "score": 0, "score_explanation": "API call failed."}


def _call_gemini_vision(api_key: str, base64_image: str, media_type: str, prompt: str) -> dict:
    """Send an image to Gemini vision API and return parsed response."""
    import requests

    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
            headers={"content-type": "application/json"},
            json={
                "contents": [{
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": media_type,
                                "data": base64_image,
                            },
                        },
                        {"text": prompt},
                    ],
                }],
                "generationConfig": {"maxOutputTokens": 500},
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        # Extract text from Gemini response
        text = ""
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            for part in parts:
                text += part.get("text", "")

        return _parse_vision_response(text)

    except Exception as e:
        log.error("Gemini vision API call failed: %s", e)
        return {"description": f"API error: {e}", "issues": [], "score": 0, "score_explanation": "API call failed."}


def _parse_vision_response(text: str) -> dict:
    """Parse the vision model's text response into structured data."""
    text = text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)

    # Try JSON parse
    try:
        parsed = json.loads(text)
        return {
            "description": parsed.get("description", ""),
            "issues": parsed.get("issues", []),
            "score": int(parsed.get("score", 5)),
            "score_explanation": parsed.get("score_explanation", ""),
        }
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to extract JSON from text
    import re
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            return {
                "description": parsed.get("description", ""),
                "issues": parsed.get("issues", []),
                "score": int(parsed.get("score", 5)),
                "score_explanation": parsed.get("score_explanation", ""),
            }
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: treat entire response as description
    # Try to extract a score from the text
    score = 5
    score_match = re.search(r"(\d+)\s*/\s*10", text)
    if score_match:
        score = min(10, max(1, int(score_match.group(1))))

    return {
        "description": text[:500],
        "issues": [],
        "score": score,
        "score_explanation": "Score extracted from unstructured response.",
    }


# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------

def _find_existing_screenshots(screenshots_dir: Path, max_count: int) -> list[Path]:
    """Find existing screenshot images in the screenshots directory."""
    if not screenshots_dir.exists():
        return []
    extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
    files = sorted(
        [f for f in screenshots_dir.iterdir() if f.suffix.lower() in extensions],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:max_count]


def _generate_screenshot_harness(target: str) -> Path | None:
    """
    Generate a harness that captures screenshots during a short game run.
    Returns the harness path, or None if the game directory doesn't exist.
    """
    target_dir = PROJECT_DIR / target
    if not target_dir.exists():
        return None

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    harness_code = textwrap.dedent(f"""\
        \"\"\"Auto-generated screenshot capture harness.\"\"\"
        import os
        import sys
        import time

        sys.path.insert(0, {str(target_dir)!r})

        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

        import pygame

        DURATION = 5
        SCREENSHOT_DIR = {str(SCREENSHOTS_DIR)!r}
        SCREENSHOT_INTERVAL = 1.5  # seconds between captures
        MAX_SCREENSHOTS = 5

        def main():
            pygame.init()
            try:
                screen = pygame.display.set_mode((800, 600))
            except Exception:
                print("Could not create display")
                return

            clock = pygame.time.Clock()
            start = time.time()
            last_capture = 0
            captures = 0

            # Fill screen with a test pattern so we at least get something
            screen.fill((30, 30, 50))
            pygame.display.flip()

            while time.time() - start < DURATION and captures < MAX_SCREENSHOTS:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        break

                elapsed = time.time() - start
                if elapsed - last_capture >= SCREENSHOT_INTERVAL:
                    path = os.path.join(SCREENSHOT_DIR, f"screenshot_{{captures}}.png")
                    try:
                        pygame.image.save(screen, path)
                        captures += 1
                        print(f"Captured: {{path}}")
                    except Exception as e:
                        print(f"Capture failed: {{e}}", file=sys.stderr)
                    last_capture = elapsed

                clock.tick(30)

            pygame.quit()
            print(f"Captured {{captures}} screenshot(s)")

        if __name__ == "__main__":
            main()
    """)

    tests_dir = target_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    harness_path = tests_dir / "screenshot_capture_harness.py"
    harness_path.write_text(harness_code, encoding="utf-8")
    return harness_path


def _capture_screenshots(target: str, max_count: int) -> list[Path]:
    """
    Attempt to capture screenshots by running a screenshot harness.
    Returns list of captured screenshot paths.
    """
    harness_path = _generate_screenshot_harness(target)
    if not harness_path:
        return []

    try:
        result = subprocess.run(
            [sys.executable, str(harness_path)],
            cwd=str(PROJECT_DIR / target),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            log.warning("Screenshot harness exited with code %d: %s", result.returncode, result.stderr)
    except subprocess.TimeoutExpired:
        log.warning("Screenshot harness timed out")
    except Exception as e:
        log.warning("Screenshot capture failed: %s", e)

    # Collect whatever screenshots were produced
    return _find_existing_screenshots(SCREENSHOTS_DIR, max_count)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(**kwargs) -> dict:
    """
    Capture and analyze game screenshots using a vision-capable LLM.

    kwargs:
        target (str):           Target engine — "pygame" (default).
        screenshots_dir (str):  Override directory to find screenshots in.
        max_screenshots (int):  Maximum number of screenshots to analyze (default 3).
    """
    target = kwargs.get("target", "pygame")
    custom_dir = kwargs.get("screenshots_dir")
    max_screenshots = kwargs.get("max_screenshots", 3)

    # --- Phase 1: Check for vision provider ---
    provider, api_key = _get_vision_provider()

    if provider is None:
        return {
            "status": "skipped",
            "summary": (
                "Visual QA skipped — no vision provider available. "
                "Set ANTHROPIC_API_KEY or GEMINI_API_KEY."
            ),
            "findings": [],
            "metrics": {
                "screenshots_analyzed": 0,
                "avg_score": 0.0,
                "issues_found": 0,
                "provider": None,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # --- Phase 2: Find or capture screenshots ---
    search_dir = Path(custom_dir) if custom_dir else SCREENSHOTS_DIR
    screenshots = _find_existing_screenshots(search_dir, max_screenshots)

    if not screenshots:
        # Try to capture screenshots
        log.info("No existing screenshots found, attempting capture for %s", target)
        screenshots = _capture_screenshots(target, max_screenshots)

    if not screenshots:
        return {
            "status": "partial",
            "summary": (
                f"Visual QA: no screenshots available for {target}. "
                "Run auto-playtest first or place screenshots in state/gamedev/screenshots/."
            ),
            "findings": [],
            "metrics": {
                "screenshots_analyzed": 0,
                "avg_score": 0.0,
                "issues_found": 0,
                "provider": provider,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [
                "Run auto-playtest to generate gameplay, then re-run visual-qa.",
                "Or manually place screenshots in state/gamedev/screenshots/.",
            ],
        }

    # --- Phase 3: Vision analysis ---
    findings = []
    total_score = 0
    total_issues = 0

    for screenshot_path in screenshots:
        base64_image = _load_image_base64(screenshot_path)
        if base64_image is None:
            continue

        media_type = _detect_media_type(screenshot_path)

        # Call the appropriate vision API
        if provider == "claude":
            result = _call_claude_vision(api_key, base64_image, media_type, COMBINED_PROMPT)
        else:
            result = _call_gemini_vision(api_key, base64_image, media_type, COMBINED_PROMPT)

        finding = {
            "screenshot": str(screenshot_path),
            "score": result.get("score", 0),
            "issues": result.get("issues", []),
            "description": result.get("description", ""),
            "score_explanation": result.get("score_explanation", ""),
        }
        findings.append(finding)
        total_score += finding["score"]
        total_issues += len(finding["issues"])

    analyzed = len(findings)
    avg_score = round(total_score / analyzed, 1) if analyzed > 0 else 0.0
    escalate = avg_score < 4 and analyzed > 0

    # Build action items from issues
    action_items = []
    for f in findings:
        for issue in f["issues"]:
            action_items.append(f"Visual issue in {Path(f['screenshot']).name}: {issue}")

    summary = (
        f"Visual QA: {analyzed} screenshot(s) analyzed. "
        f"Score: {avg_score}/10. "
        f"{total_issues} issue(s) found."
    )

    return {
        "status": "success" if analyzed > 0 else "partial",
        "summary": summary,
        "findings": findings,
        "metrics": {
            "screenshots_analyzed": analyzed,
            "avg_score": avg_score,
            "issues_found": total_issues,
            "provider": provider,
        },
        "escalate": escalate,
        "escalation_reason": (
            f"Visual quality critically low: avg score {avg_score}/10."
            if escalate else ""
        ),
        "action_items": action_items,
    }
