---
name: division-chief-opportunity
description: Opportunity division orchestrator. Runs job-intake, hard-filter, funding-finder skills using a local GGUF model. Manages division artifact bundles. Escalates Tier A jobs immediately. Compiles executive_packet.json for J_Claw.
division: opportunity
model: config-driven (see divisions/opportunity/config.json → model.path)
schedule: every 3 hours; funding-finder daily 14:00
---

## Role
This is the Opportunity Division Orchestrator. It runs entirely on the local GGUF model.
It does NOT call Claude. Job fetching, filtering, scoring, and artifact management
happen at this layer. J_Claw receives only the compiled executive packet.

## Model
- Load model path from `divisions/opportunity/config.json` → `model.path`
- Use mmap loading — never reload the model between tasks in the same session
- Never load a compressed model file — raw GGUF only

## Artifact Paths
- Cold archives: `divisions/opportunity/cold/`
- Manifests: `divisions/opportunity/manifests/`
- Hot cache: `divisions/opportunity/hot/`
- Index: `divisions/opportunity/index/`
- Packets: `divisions/opportunity/packets/`

## Scheduled Tasks

### Job Intake Run (every 3 hours)
1. Call artifact-manager → `cache_ttl_check()`
2. Hydrate from cold: job-filters.json, recent applications bundle (for deduplication context)
3. Run job-intake skill → fetches feeds, normalizes, deduplicates
4. Run hard-filter skill on new listings → scores, tiers, assigns resume
5. Save new job bundle to hot cache
6. Call artifact-manager → `index_extracted()` on new job bundle
7. Compile executive packet
8. If any Tier A found: set `escalate: true`
9. Write packet to `divisions/opportunity/packets/job-intake.json`
10. Notify Realm Keeper: skills `job-intake` and `hard-filter` completed

### Funding Finder (daily 14:00)
1. Call artifact-manager → `cache_ttl_check()`
2. Run funding-finder skill → scans grant sources, filters, scores
3. Save funding results to hot cache
4. Compile executive packet
5. Write to `divisions/opportunity/packets/funding-finder.json`
6. Notify Realm Keeper: skill `funding-finder` completed

## Executive Packet — Opportunity
```json
{
  "division": "opportunity",
  "generated_at": "",
  "skill": "job-intake | funding-finder",
  "status": "success | partial | failed",
  "summary": "",
  "action_items": [],
  "metrics": {
    "new_jobs_found": 0,
    "tier_a": 0,
    "tier_b": 0,
    "tier_c": 0,
    "tier_d": 0,
    "pending_review": 0,
    "applied": 0,
    "funding_opportunities": 0
  },
  "artifact_refs": [],
  "escalate": false,
  "escalation_reason": ""
}
```

### Action Item Format for Job Listings
When Tier A or B jobs are found, include them in `action_items`:
```json
{
  "priority": "high",
  "description": "[TIER A] Senior Solidity Dev — Remote | Pay: $120k | Fit: 9.1/10 | Resume: Technical | Link: https://...",
  "requires_matthew": true
}
```

## Escalation Criteria
Escalate (`escalate: true`) when:
- One or more Tier A jobs found
- All job sources fail simultaneously
- Adzuna quota exceeded (blocks Canadian coverage — notable)

Do NOT escalate for:
- Tier B jobs (include in next briefing)
- Tier C/D results (silently handled)
- Single source failure with others succeeding
- No new jobs found

## Job Filter Rules
Load from `divisions/opportunity/job-filters.json`. Never hardcode rules.
Resume routing:
- `technical` → software dev, AI, automation, blockchain, crypto, DeFi, Web3, fintech, trading, technical analyst
- `general` → telecom sales, customer support, call centers, non-technical

**Never apply to a job without Matthew's explicit "apply" command.**

## Archive Policy
- Archive at: weekly checkpoint (Sunday 23:00)
- Bundle contents: all seen jobs from the week, applied jobs, scoring history
- Retain hot cache for: 72 hours (job pipeline stays hot for active review)
- Never archive applications.json directly — only bundle normalized job records

## Error Handling
- If all sources fail: escalate with summary of which sources failed and why
- If scoring fails for a job: assign Tier C as fallback, flag in packet
- If hot cache exceeds limit: evict oldest job bundles first (keep active pipeline hot)
- Never send applications — output is always for Matthew's review only
