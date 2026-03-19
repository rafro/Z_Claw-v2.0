---
name: repo-monitor
description: Scan Matthew's GitHub repositories every 3 hours using the gh CLI. Flag TODOs, FIXMEs, stale branches, frequent edits, missing READMEs, and potential issues. Compile results for division-chief-dev-automation. Send digest summary via J_Claw at 3PM.
schedule: every 3 hours
division: dev-automation
runner: division-chief-dev-automation
---

## Trigger
Called by division-chief-dev-automation every 3 hours.
Do NOT call Claude directly — this skill runs under the local GGUF division orchestrator.
The 15:00 run triggers Telegram send (handled by J_Claw on packet receipt when `send_digest: true`).

## Prerequisites
- GitHub CLI (`gh`) must be authenticated — run `gh auth status` to verify
- If not authenticated: return failure packet — division chief escalates immediately

## Steps

1. **Verify auth**
   ```bash
   gh auth status
   ```
   If not authenticated: return `status: failed`, set `escalate: true` in packet contribution.

2. **List repositories**
   ```bash
   gh repo list --limit 100 --json name,url,updatedAt,defaultBranchRef
   ```

3. **For each repository, run checks:**

   **a. TODO / FIXME scan**
   ```bash
   gh api repos/{owner}/{repo}/git/trees/HEAD?recursive=1
   # fetch and grep source files for TODO, FIXME, HACK, XXX
   ```
   Record: file path, line number, comment text

   **b. Stale branch check**
   ```bash
   gh api repos/{owner}/{repo}/branches
   ```
   Flag branches with last commit older than 14 days that are not `main` or `master`

   **c. Commit frequency**
   Check commits in last 7 days. Flag repos with 0 commits as potentially stale.

   **d. Missing README**
   Check if README.md exists at repo root. Flag if missing.

   **e. Architectural issues** (heuristic flags)
   - Files over 500 lines
   - Repeated similar filenames suggesting duplication
   - Backup files present (.bak, copy*, _old*)

4. **Classify flags by severity**
   - HIGH: exposed credentials, security issues, broken builds
   - MEDIUM: missing READMEs, files > 500 lines, backup files, large artifacts
   - LOW: stale branches, zero-commit repos, TODOs in non-critical files

5. **Compile digest**
   Structure:
   ```
   ## Repo Monitor Digest — {date}

   ### {repo-name}
   - TODOs: {count} found → {file}:{line}
   - Stale branches: {branch names}
   - Last commit: {date}
   - README: MISSING | OK
   - Flags: {list of issues}
   ```

6. **Save digest to hot cache**
   Write to `divisions/dev-automation/hot/repo-digest-{date}.json`
   Also write markdown version to `reports/repo-digest-{YYYY-MM-DD}.md` (backward compat)

7. **Return results to division chief**
   Division chief compiles executive packet.

## Executive Packet Contribution
repo-monitor contributes to division-chief-dev-automation packet:
```json
{
  "metrics": {
    "repos_scanned": 0,
    "flags_high": 0,
    "flags_medium": 0,
    "flags_low": 0,
    "send_digest": false
  },
  "summary": "{N} repos scanned | {h} HIGH, {m} MEDIUM, {l} LOW flags",
  "action_items": []
}
```

`send_digest` is set to `true` on the 15:00 run only.
HIGH-severity flags are added as `priority: "high"` action items.
MEDIUM/LOW flags are bundled into the digest — not individual action items.

## Error Handling
- If `gh` auth fails: return `status: failed`, `escalate: true`, reason: "gh not authenticated"
- If single repo scan fails: log error, continue with others — note failure in summary
- If all repos fail: return `status: failed` — division chief escalates
- Never silently fail
