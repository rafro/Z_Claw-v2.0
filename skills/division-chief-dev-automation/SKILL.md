---
name: division-chief-dev-automation
description: Dev Automation division orchestrator. Runs repo-monitor, debug, refactor, doc, and security skills using a local GGUF model. Manages division artifact bundles. Compiles executive_packet.json for J_Claw.
division: dev-automation
model: config-driven (see divisions/dev-automation/config.json → model.path)
schedule: repo-monitor every 3 hours; weekly scans per schedule; debug on-demand
---

## Role
This is the Dev Automation Division Orchestrator. It runs entirely on the local GGUF model.
It does NOT call Claude. Repo scanning, code analysis, and artifact management
happen at this layer. J_Claw receives only the compiled executive packet.

## Model
- Load model path from `divisions/dev-automation/config.json` → `model.path`
- Use mmap loading — never reload the model between tasks in the same session
- Never load a compressed model file — raw GGUF only

## Artifact Paths
- Cold archives: `divisions/dev-automation/cold/`
- Manifests: `divisions/dev-automation/manifests/`
- Hot cache: `divisions/dev-automation/hot/`
- Index: `divisions/dev-automation/index/`
- Packets: `divisions/dev-automation/packets/`

## Scheduled Tasks

### Repo Monitor (every 3 hours)
1. Call artifact-manager → `cache_ttl_check()`
2. Verify `gh` CLI is authenticated — if not, escalate and abort
3. Run repo-monitor skill → scans repos, flags issues
4. Save scan results to hot cache
5. Call artifact-manager → `index_extracted()` on scan bundle
6. Classify flags by severity: HIGH / MEDIUM / LOW
7. Compile executive packet
8. At 15:00 run: set `send_digest: true` (J_Claw sends dev digest to Telegram)
9. Write packet to `divisions/dev-automation/packets/repo-monitor.json`
10. Notify Realm Keeper: skill `repo-monitor` completed

### Debug Agent (on-demand, triggered by error log)
1. Receive error log path as input
2. Hydrate: relevant source files, recent commit history summary, error context
3. Run debug analysis
4. Compile packet with: root cause, file location, suggested fix
5. Set `escalate: true` (debug always surfaces to J_Claw immediately)
6. Write packet to `divisions/dev-automation/packets/debug-agent.json`

### Refactor Scan (weekly, Monday)
1. Hydrate recent repo snapshots from cold
2. Run refactor-scan skill → flag duplicated logic, oversized functions, inefficient patterns
3. Compile executive packet with ranked refactor candidates
4. Write to `divisions/dev-automation/packets/refactor-scan.json`

### Documentation Update (weekly, Wednesday)
1. Hydrate repo snapshots and existing doc bundles from cold
2. Run doc-update skill → flag missing/stale READMEs, API docs, architecture docs
3. Compile executive packet with doc gaps
4. Write to `divisions/dev-automation/packets/doc-update.json`

### Security Scan (weekly, Friday)
1. Hydrate dependency manifests and config files from cold
2. Run security-scan skill → flag vulnerabilities, outdated packages, exposed credentials
3. Compile executive packet
4. If HIGH-severity vulnerability found: set `escalate: true`
5. Write to `divisions/dev-automation/packets/security-scan.json`

## Executive Packet — Dev Automation
```json
{
  "division": "dev-automation",
  "generated_at": "",
  "skill": "repo-monitor | debug-agent | refactor-scan | doc-update | security-scan",
  "status": "success | partial | failed",
  "summary": "",
  "action_items": [],
  "metrics": {
    "repos_scanned": 0,
    "flags_high": 0,
    "flags_medium": 0,
    "flags_low": 0,
    "send_digest": false
  },
  "artifact_refs": [],
  "escalate": false,
  "escalation_reason": ""
}
```

## Escalation Criteria
Escalate (`escalate: true`) when:
- `gh` CLI not authenticated (blocks all repo scans)
- DEBUG agent activated (always escalates)
- Security scan finds HIGH-severity vulnerability
- A repo scan finds exposed credentials or API keys in source

Do NOT escalate for:
- Routine LOW/MEDIUM flags (bundle in digest)
- Missing READMEs (include in doc-update packet)
- Stale branches over 14 days (include in repo-monitor packet)

## Archive Policy
- Archive at: weekly checkpoint (Sunday 23:00)
- Bundle contents: repo scan digests, refactor reports, security scan results
- Retain hot cache for: 48 hours
- Never archive raw repo source code — only scan summaries and flag reports

## Error Handling
- If `gh` auth fails: escalate immediately — all repo tasks depend on it
- If a single repo scan fails: log error, continue with other repos, note in packet
- If all repos fail: escalate with error details
- Never silently fail — all errors surface in packet summary
