"""
repo-monitor skill — Tier 2 LLM (Qwen2.5 14B) with Tier 3 API fallback.
Scans GitHub repos via gh CLI, flags issues, produces structured findings.
"""

import json
import subprocess
import logging
from datetime import datetime, timezone, date
from pathlib import Path

from runtime.config import SKILL_MODELS, MODEL_14B_HOST, ROOT
from runtime.ollama_client import chat_json, is_available

log = logging.getLogger(__name__)
MODEL  = SKILL_MODELS["repo-monitor"]
HOT_DIR = ROOT / "divisions" / "dev-automation" / "hot"

SEVERITY = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _gh(*args) -> tuple[str, bool]:
    """Run a gh CLI command. Returns (output, success)."""
    try:
        result = subprocess.run(
            ["gh"] + list(args),
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return result.stderr.strip(), False
        return result.stdout.strip(), True
    except FileNotFoundError:
        return "gh CLI not found", False
    except subprocess.TimeoutExpired:
        return "gh command timed out", False
    except Exception as e:
        return str(e), False


def check_auth() -> bool:
    _, ok = _gh("auth", "status")
    return ok


def list_repos() -> list:
    out, ok = _gh("repo", "list", "--limit", "50",
                  "--json", "name,url,updatedAt,pushedAt")
    if not ok:
        log.error("gh repo list failed: %s", out)
        return []
    try:
        return json.loads(out)
    except Exception:
        return []


def _scan_repo(repo_name: str) -> list:
    """Run checks on a single repo. Returns list of flag dicts."""
    flags = []
    owner_repo = repo_name if "/" in repo_name else repo_name

    # a. Missing README
    out, ok = _gh("api", f"repos/{owner_repo}/contents/README.md")
    if not ok and "404" in out:
        flags.append({
            "severity": "MEDIUM",
            "type":     "missing_readme",
            "repo":     owner_repo,
            "detail":   "No README.md at repo root",
        })

    # b. Stale branches (>14 days, not main/master)
    out, ok = _gh("api", f"repos/{owner_repo}/branches", "--paginate")
    if ok:
        try:
            branches = json.loads(out)
            for branch in branches:
                name = branch.get("name", "")
                if name in ("main", "master", "develop"):
                    continue
                # We'd need commit date — skip for MVP, flag all non-main branches
                flags.append({
                    "severity": "LOW",
                    "type":     "stale_branch",
                    "repo":     owner_repo,
                    "detail":   f"Branch '{name}' exists — verify if still needed",
                })
        except Exception:
            pass

    # c. TODO/FIXME scan via git tree (limited — sample source files)
    out, ok = _gh("api", f"repos/{owner_repo}/git/trees/HEAD?recursive=1")
    if ok:
        try:
            tree = json.loads(out).get("tree", [])
            source_files = [
                f["path"] for f in tree
                if f.get("type") == "blob"
                and any(f["path"].endswith(ext) for ext in
                        (".py", ".js", ".ts", ".sol", ".go", ".rs"))
                and not any(p in f["path"] for p in
                            ("node_modules", ".git", "dist", "build"))
            ]
            # Spot-check up to 10 files
            for fpath in source_files[:10]:
                out2, ok2 = _gh("api", f"repos/{owner_repo}/contents/{fpath}")
                if ok2:
                    try:
                        import base64
                        content_data = json.loads(out2)
                        content = base64.b64decode(
                            content_data.get("content", "")
                        ).decode("utf-8", errors="replace")
                        lines = content.splitlines()
                        if len(lines) > 500:
                            flags.append({
                                "severity": "MEDIUM",
                                "type":     "large_file",
                                "repo":     owner_repo,
                                "detail":   f"{fpath} — {len(lines)} lines (>500)",
                            })
                        for i, line in enumerate(lines, 1):
                            upper = line.upper()
                            if any(kw in upper for kw in ("TODO", "FIXME", "HACK", "XXX")):
                                flags.append({
                                    "severity": "LOW",
                                    "type":     "todo",
                                    "repo":     owner_repo,
                                    "detail":   f"{fpath}:{i} — {line.strip()[:80]}",
                                })
                    except Exception:
                        pass
        except Exception:
            pass

    return flags


def _llm_analyze(repos: list, all_flags: list) -> dict:
    """Use LLM (Tier 2/14B) to synthesize architectural observations."""
    if not is_available(MODEL, host=MODEL_14B_HOST):
        # Try 7B local fallback on 3060 Ti
        from runtime.config import MODEL_7B, OLLAMA_HOST
        if is_available(MODEL_7B, host=OLLAMA_HOST):
            log.info("repo-monitor: 14B unavailable, falling back to 7B")
            use_model, use_host = MODEL_7B, OLLAMA_HOST
        else:
            log.warning("repo-monitor: no local model available, returning raw flags")
            high = [f for f in all_flags if f["severity"] == "HIGH"]
            med  = [f for f in all_flags if f["severity"] == "MEDIUM"]
            low  = [f for f in all_flags if f["severity"] == "LOW"]
            return {
                "summary":  f"{len(all_flags)} flags: {len(high)} HIGH, {len(med)} MEDIUM, {len(low)} LOW",
                "high_priority": high,
                "recommendations": [],
            }
    else:
        use_model, use_host = MODEL, MODEL_14B_HOST

    flag_text = "\n".join(
        f"[{f['severity']}] {f['repo']} — {f['type']}: {f['detail']}"
        for f in all_flags[:30]
    )
    repo_names = ", ".join(r.get("name", "") for r in repos[:10])

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Dev Automation Division orchestrator for J_Claw. "
                "Analyze the repo scan results and return JSON with:"
                "\n- summary: 1-2 sentence overview"
                "\n- high_priority: array of the most critical flags (max 5)"
                "\n- recommendations: array of actionable items (max 3, be specific)"
                "\nOnly include high_priority items that genuinely need attention. "
                "Return valid JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Repos scanned: {repo_names}\n"
                f"Total flags: {len(all_flags)}\n\n"
                f"Flags:\n{flag_text or 'None found'}"
            ),
        },
    ]

    try:
        return chat_json(use_model, messages, host=use_host,
                         temperature=0.1, max_tokens=600)
    except Exception as e:
        log.error("repo-monitor LLM analysis failed: %s", e)
        high = [f for f in all_flags if f["severity"] == "HIGH"]
        med  = [f for f in all_flags if f["severity"] == "MEDIUM"]
        low  = [f for f in all_flags if f["severity"] == "LOW"]
        return {
            "summary":       f"{len(all_flags)} flags: {len(high)} H, {len(med)} M, {len(low)} L",
            "high_priority": high[:5],
            "recommendations": [],
        }


def run() -> dict:
    # Auth check
    if not check_auth():
        return {
            "status":  "failed",
            "escalate": True,
            "escalation_reason": "gh CLI not authenticated",
            "flags": [], "analysis": {},
            "repos_checked": 0,
        }

    repos = list_repos()
    if not repos:
        return {
            "status":  "partial",
            "escalate": False,
            "flags": [], "analysis": {"summary": "No repos found or gh list failed"},
            "repos_checked": 0,
        }

    all_flags = []
    for repo in repos[:10]:   # cap at 10 repos to control runtime
        name = repo.get("name", "")
        if not name:
            continue
        # Get owner from gh auth
        whoami_out, _ = _gh("api", "user", "--jq", ".login")
        owner = whoami_out.strip() or "unknown"
        flags = _scan_repo(f"{owner}/{name}")
        all_flags.extend(flags)

    # LLM synthesis
    analysis = _llm_analyze(repos, all_flags)

    # Save to hot cache
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    hot_path = HOT_DIR / f"repo-scan-{today}.json"
    import json as _json
    with open(hot_path, "w", encoding="utf-8") as f:
        _json.dump({
            "date": today,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "repos_checked": len(repos),
            "flags": all_flags,
            "analysis": analysis,
        }, f, indent=2)

    high_count = sum(1 for f in all_flags if f["severity"] == "HIGH")
    return {
        "status":    "success",
        "flags":     all_flags,
        "analysis":  analysis,
        "repos_checked": len(repos),
        "flag_counts": {
            "high":   high_count,
            "medium": sum(1 for f in all_flags if f["severity"] == "MEDIUM"),
            "low":    sum(1 for f in all_flags if f["severity"] == "LOW"),
        },
        "escalate":          high_count > 0,
        "escalation_reason": f"{high_count} HIGH severity flags found" if high_count else "",
    }
