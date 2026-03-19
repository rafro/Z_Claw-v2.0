"""
Job fetching tools — pure I/O, no LLM.
Fetches from all active sources, normalizes to standard schema.
"""

import re
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from runtime.config import ADZUNA_APP_ID, ADZUNA_APP_KEY

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OpenClaw/2.0)"}
TIMEOUT = 25


# ── Normalisation helpers ─────────────────────────────────────────────────────

def _strip_html(text: str, max_len: int = 300) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def _job(source: str, job_id: str, title: str, company: str = "",
         location: str = "Remote", remote: bool = True,
         pay_min=None, pay_max=None, pay_type: str = "unspecified",
         description: str = "", url: str = "",
         salary_raw: str = "", tags: str = "") -> dict:
    return {
        "id":                  f"{source}-{job_id}",
        "title":               title.strip(),
        "company":             company.strip(),
        "location":            location or "Remote",
        "remote":              remote,
        "pay_min":             pay_min,
        "pay_max":             pay_max,
        "pay_type":            pay_type,
        "salary_raw":          salary_raw,
        "description_summary": _strip_html(description),
        "url":                 url,
        "source":              source,
        "tags":                tags,
        "fetched_at":          datetime.now(timezone.utc).isoformat(),
        "seen":                False,
        "filtered":            False,
        "tier":                None,
        "resume":              None,
    }


# ── Source 1: We Work Remotely (RSS) ─────────────────────────────────────────

def fetch_wwr() -> tuple[list, Optional[str]]:
    try:
        resp = requests.get(
            "https://weworkremotely.com/remote-jobs.rss",
            headers=HEADERS, timeout=TIMEOUT
        )
        resp.raise_for_status()
        # Parse XML with ElementTree (no extra deps)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.content)
        jobs = []
        for item in root.findall(".//item"):
            link = (item.findtext("link") or "").strip()
            if not link:
                continue
            jobs.append(_job(
                source="wwr", job_id=link,
                title=item.findtext("title") or "",
                description=item.findtext("description") or "",
                url=link,
            ))
        log.info("WWR: %d listings", len(jobs))
        return jobs, None
    except Exception as e:
        log.error("WWR fetch failed: %s", e)
        return [], f"WWR: {e}"


# ── Source 2: Remote OK (REST) ────────────────────────────────────────────────

def fetch_remoteok() -> tuple[list, Optional[str]]:
    try:
        resp = requests.get(
            "https://remoteok.com/api",
            headers=HEADERS, timeout=TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for item in data[1:]:   # first element is metadata
            jid = str(item.get("id", ""))
            if not jid:
                continue
            pay_min = pay_max = None
            pay_type = "unspecified"
            if item.get("salary_min") and int(item["salary_min"]) > 0:
                pay_min = int(item["salary_min"])
                pay_max = int(item.get("salary_max") or 0) or None
                pay_type = "salary"
            jobs.append(_job(
                source="remoteok", job_id=jid,
                title=item.get("position", ""),
                company=item.get("company", ""),
                pay_min=pay_min, pay_max=pay_max, pay_type=pay_type,
                description=item.get("description", ""),
                url=item.get("url", ""),
                tags=",".join(item.get("tags") or []),
            ))
        log.info("RemoteOK: %d listings", len(jobs))
        return jobs, None
    except Exception as e:
        log.error("RemoteOK fetch failed: %s", e)
        return [], f"RemoteOK: {e}"


# ── Source 3: Remotive (REST) ─────────────────────────────────────────────────

def fetch_remotive(rate_limited: bool = False) -> tuple[list, Optional[str]]:
    if rate_limited:
        log.info("Remotive: skipped (rate limited this session)")
        return [], None
    try:
        resp = requests.get(
            "https://remotive.com/api/remote-jobs",
            headers=HEADERS, timeout=TIMEOUT
        )
        if resp.status_code == 429:
            log.warning("Remotive: rate limited")
            return [], "Remotive: rate limited"
        resp.raise_for_status()
        jobs_data = resp.json().get("jobs", [])
        jobs = []
        for item in jobs_data:
            salary_raw = str(item.get("salary") or "").strip()
            pay_type = "salary" if salary_raw else "unspecified"
            jobs.append(_job(
                source="remotive", job_id=str(item.get("id", "")),
                title=item.get("title", ""),
                company=item.get("company_name", ""),
                location=item.get("candidate_required_location", "Worldwide"),
                pay_type=pay_type, salary_raw=salary_raw,
                description=item.get("description", ""),
                url=item.get("url", ""),
                tags=",".join(item.get("tags") or []),
            ))
        log.info("Remotive: %d listings", len(jobs))
        return jobs, None
    except Exception as e:
        log.error("Remotive fetch failed: %s", e)
        return [], f"Remotive: {e}"


# ── Source 4: Adzuna (3 queries, US endpoint) ─────────────────────────────────

ADZUNA_QUERIES = [
    {"what": "blockchain OR solidity OR web3 OR defi OR AI developer",
     "salary_min": 60000, "rpp": 50},
    {"what": "software developer OR engineer OR technical analyst",
     "salary_min": 100000, "rpp": 50},
    {"what": "telecom sales OR customer support OR technical support",
     "salary_min": 35000, "rpp": 20},
]


def fetch_adzuna() -> tuple[list, list[str]]:
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        log.warning("Adzuna: credentials not configured")
        return [], ["Adzuna: credentials not configured"]

    jobs = []
    errors = []
    rate_limited = False

    for q in ADZUNA_QUERIES:
        if rate_limited:
            break
        try:
            params = {
                "app_id": ADZUNA_APP_ID,
                "app_key": ADZUNA_APP_KEY,
                "what": q["what"],
                "where": "remote",
                "salary_min": q["salary_min"],
                "results_per_page": q["rpp"],
                "sort_by": "date",
            }
            resp = requests.get(
                "http://api.adzuna.com/v1/api/jobs/us/search/1",
                params=params, headers=HEADERS, timeout=TIMEOUT
            )
            if resp.status_code == 429:
                rate_limited = True
                errors.append("Adzuna: rate limited")
                log.warning("Adzuna: rate limited — stopping queries")
                break
            resp.raise_for_status()
            for item in resp.json().get("results", []):
                jid = str(item.get("id", ""))
                if not jid:
                    continue
                pay_min = pay_max = None
                pay_type = "unspecified"
                if item.get("salary_min") and float(item["salary_min"]) > 0:
                    pay_min = int(float(item["salary_min"]))
                    pay_type = "salary"
                if item.get("salary_max") and float(item["salary_max"]) > 0:
                    pay_max = int(float(item["salary_max"]))
                loc = ""
                if item.get("location"):
                    loc = item["location"].get("display_name", "")
                co = ""
                if item.get("company"):
                    co = item["company"].get("display_name", "")
                jobs.append(_job(
                    source="adzuna", job_id=jid,
                    title=item.get("title", ""),
                    company=co, location=loc or "Remote",
                    pay_min=pay_min, pay_max=pay_max, pay_type=pay_type,
                    description=item.get("description", ""),
                    url=item.get("redirect_url", ""),
                ))
            time.sleep(0.6)  # be a good API citizen
        except Exception as e:
            errors.append(f"Adzuna query '{q['what']}': {e}")
            log.error("Adzuna query failed: %s", e)

    log.info("Adzuna: %d listings", len(jobs))
    return jobs, errors


# ── Main fetch entry point ────────────────────────────────────────────────────

def fetch_all_jobs() -> tuple[list, dict]:
    """
    Fetch from all active sources. Returns (all_listings, source_status).
    source_status maps source name → 'ok' | 'failed' | 'rate_limited'.
    """
    all_jobs = []
    status = {}
    errors_log = []

    wwr_jobs, wwr_err = fetch_wwr()
    all_jobs.extend(wwr_jobs)
    status["WeWorkRemotely"] = "failed" if wwr_err else "ok"
    if wwr_err:
        errors_log.append(wwr_err)

    rok_jobs, rok_err = fetch_remoteok()
    all_jobs.extend(rok_jobs)
    status["RemoteOK"] = "failed" if rok_err else "ok"
    if rok_err:
        errors_log.append(rok_err)

    rem_jobs, rem_err = fetch_remotive()
    all_jobs.extend(rem_jobs)
    if rem_err == "Remotive: rate limited":
        status["Remotive"] = "rate_limited"
    elif rem_err:
        status["Remotive"] = "failed"
        errors_log.append(rem_err)
    else:
        status["Remotive"] = "ok"

    adz_jobs, adz_errs = fetch_adzuna()
    all_jobs.extend(adz_jobs)
    if any("rate limited" in e for e in adz_errs):
        status["Adzuna"] = "rate_limited"
    elif adz_errs:
        status["Adzuna"] = "partial"
    else:
        status["Adzuna"] = "ok"
    errors_log.extend(adz_errs)

    return all_jobs, status, errors_log


def deduplicate(jobs: list, seen_ids: set) -> list:
    """Filter out jobs already in seen_ids. Returns only new jobs."""
    new = [j for j in jobs if j["id"] not in seen_ids]
    log.info("Dedup: %d total → %d new", len(jobs), len(new))
    return new
