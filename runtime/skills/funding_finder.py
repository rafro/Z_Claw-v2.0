"""
funding-finder skill — Tier 1 LLM (Qwen2.5 7B via Ollama).
Scrapes publicly available grant/funding sources, scores each opportunity
for Matthew's eligibility and fit, deduplicates against seen URLs.
"""

import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from runtime.config import SKILL_MODELS, STATE_DIR, LOGS_DIR, ROOT
from runtime.ollama_client import chat_json, is_available

log = logging.getLogger(__name__)
MODEL   = SKILL_MODELS["funding-finder"]
HOT_DIR = ROOT / "divisions" / "opportunity" / "hot"
SEEN_FILE = STATE_DIR / "funding-seen.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OpenClaw/2.0)"}
TIMEOUT = 20

# ── Sources ───────────────────────────────────────────────────────────────────

SOURCES = [
    {
        "name": "NBIF (NB Innovation Foundation)",
        "url":  "https://nbif.ca",
        "type": "html",
        "coverage": "NB-specific venture capital and startup funding (replaced Accelerate NB)",
    },
    {
        "name": "Opportunities NB",
        "url":  "https://onbcanada.ca",
        "type": "html",
        "coverage": "NB business accelerator and investment programs",
    },
    {
        "name": "NRC IRAP",
        "url":  "https://nrc.canada.ca/en/support-technology-innovation/about-nrc-industrial-research-assistance-program",
        "type": "html",
        "coverage": "R&D funding — NB eligible",
    },
    {
        "name": "Ethereum Foundation Grants",
        "url":  "https://esp.ethereum.foundation",
        "type": "html",
        "coverage": "Web3/Ethereum ecosystem",
    },
    {
        "name": "Gitcoin Grants",
        "url":  "https://grants.gitcoin.co",
        "type": "html",
        "coverage": "Open source / DeFi",
    },
    {
        "name": "Solana Foundation Grants",
        "url":  "https://solana.org/grants",
        "type": "html",
        "coverage": "Solana ecosystem",
    },
    {
        "name": "Y Combinator",
        "url":  "https://www.ycombinator.com/apply",
        "type": "html",
        "coverage": "Global accelerator",
    },
    {
        "name": "BDC Financing",
        "url":  "https://www.bdc.ca/en/financing",
        "type": "html",
        "coverage": "Business funding Canada",
    },
]

# ── Seen-state helpers ────────────────────────────────────────────────────────

def _load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {"seen": [], "last_run": None}
    try:
        with open(SEEN_FILE, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "last_run": None}


def _save_seen(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── Fetching ──────────────────────────────────────────────────────────────────

def _strip_html(text: str, max_len: int = 2000) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>",  " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def _fetch_source(source: dict) -> tuple[Optional[str], Optional[str]]:
    """Fetch a source page. Returns (text_content, error)."""
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return _strip_html(resp.text), None
    except Exception as e:
        return None, f"{source['name']}: {e}"


# ── LLM scoring ───────────────────────────────────────────────────────────────

SCORE_PROMPT = """You are the Opportunity Division orchestrator for J_Claw.
Analyze this funding source page for Matthew — a solo developer/trader in Campbellton, NB, Canada.

Matthew's focus areas: Software/SaaS, AI tools, fintech/trading platforms, DeFi/Web3/blockchain, gaming tools.
He is a solo founder / small team. No established revenue yet (early stage).
Priority: Canadian programs (especially NB), then global Web3/tech programs.

Extract any funding opportunities from the page content and score each one.
Return a JSON array. If no opportunities found, return [].

Each opportunity object:
{
  "name": "",
  "description": "",
  "amount": "",
  "deadline": "",
  "eligibility_notes": "",
  "url": "",
  "scores": {
    "eligibility_fit": 0,
    "effort_required": 0,
    "potential_value": 0
  },
  "composite": 0.0,
  "include": false
}

Scoring (1-10):
- eligibility_fit: Can Matthew actually apply? (10 = perfect fit, 1 = disqualified)
- effort_required: How easy to apply? (10 = trivial, 1 = extremely complex)
- potential_value: How valuable is this program? (10 = life-changing, 1 = minimal)
- composite: average of the three scores
- include: true if composite >= 6.0, else false

Return ONLY valid JSON array. No markdown, no explanation."""


def _score_source(source: dict, page_text: str) -> list:
    """Ask LLM to extract and score opportunities from page content."""
    if not is_available(MODEL):
        log.warning("funding-finder: model unavailable, skipping LLM scoring for %s", source["name"])
        return []

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {
            "role": "user",
            "content": (
                f"Source: {source['name']} ({source['coverage']})\n"
                f"URL: {source['url']}\n\n"
                f"Page content:\n{page_text}"
            ),
        },
    ]
    try:
        result = chat_json(MODEL, messages, temperature=0.05, max_tokens=1024, task_type="funding-finder")
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "opportunities" in result:
            return result["opportunities"]
        return []
    except Exception as e:
        log.error("funding-finder LLM scoring failed for %s: %s", source["name"], e)
        return []


# ── Main entry point ──────────────────────────────────────────────────────────

def run() -> dict:
    """
    Scan all funding sources, score opportunities, return result dict
    for the opportunity orchestrator.
    """
    LOGS_DIR.mkdir(exist_ok=True)
    HOT_DIR.mkdir(parents=True, exist_ok=True)

    seen_state = _load_seen()
    seen_urls  = set(seen_state.get("seen", []))
    today      = date.today().isoformat()

    all_opportunities = []
    source_errors     = []
    new_urls_seen     = []

    for source in SOURCES:
        page_text, err = _fetch_source(source)

        if err:
            source_errors.append(err)
            log.warning("funding-finder fetch failed: %s", err)
            continue

        opportunities = _score_source(source, page_text)

        for opp in opportunities:
            url = opp.get("url") or source["url"]
            # Track all seen URLs (even rejected) to avoid re-processing
            if url not in seen_urls:
                new_urls_seen.append(url)
                seen_urls.add(url)

            # Only surface opportunities that pass the score threshold
            if opp.get("include") and opp.get("composite", 0) >= 6.0:
                opp["source"] = source["name"]
                opp["found_at"] = datetime.now(timezone.utc).isoformat()
                all_opportunities.append(opp)
                log.info(
                    "funding-finder: found opportunity '%s' score=%.1f",
                    opp.get("name", "?"), opp.get("composite", 0)
                )

    # Update seen state
    seen_state["seen"] = list(seen_urls)
    seen_state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_seen(seen_state)

    # Save to hot cache
    hot_path = HOT_DIR / f"funding-{today}.json"
    with open(hot_path, "w", encoding="utf-8") as f:
        json.dump({
            "date": today,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "opportunities": all_opportunities,
            "errors": source_errors,
        }, f, indent=2)

    # Log errors
    if source_errors:
        err_log = LOGS_DIR / "funding-finder-errors.log"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(err_log, "a", encoding="utf-8") as f:
            for e in source_errors:
                f.write(f"[{ts}] {e}\n")

    all_failed = len(source_errors) == len(SOURCES)

    return {
        "opportunities":   all_opportunities,
        "source_errors":   source_errors,
        "all_failed":      all_failed,
        "model_available": is_available(MODEL),
        "counts": {
            "opportunities_found": len(all_opportunities),
            "sources_failed":      len(source_errors),
            "new_urls_seen":       len(new_urls_seen),
        },
    }
