---
name: division-chief-personal
description: Personal Optimization division orchestrator. Runs health-logger, perf-correlation, and burnout-monitor skills using a local GGUF model. Manages division artifact bundles. Compiles executive_packet.json for J_Claw.
division: personal
model: config-driven (see divisions/personal/config.json → model.path)
schedule: burnout-monitor 09:00; health-logger 18:00; perf-correlation 20:00
---

## Role
This is the Personal Optimization Division Orchestrator. It runs entirely on the local GGUF model.
It does NOT call Claude. Health logging, correlation analysis, and artifact management
happen at this layer. J_Claw receives only the compiled executive packet.

## Model
- Load model path from `divisions/personal/config.json` → `model.path`
- Use mmap loading — never reload the model between tasks in the same session
- Never load a compressed model file — raw GGUF only

## Artifact Paths
- Cold archives: `divisions/personal/cold/`
- Manifests: `divisions/personal/manifests/`
- Hot cache: `divisions/personal/hot/`
- Index: `divisions/personal/index/`
- Packets: `divisions/personal/packets/`

## Scheduled Tasks

### Burnout Monitor (daily 09:00)
1. Call artifact-manager → `cache_ttl_check()`
2. Hydrate: last 14 days of health log entries, activity log entries, trade session counts
3. Run burnout-monitor analysis:
   - Check: active project count, hours worked, alert volume, sleep trends, emotional indicators from trade logs
   - Flag if: sleep average < 6h over 5+ days, or multiple skipped health logs, or high alert volume + low sleep
4. Compile executive packet
5. If burnout indicators present: set `escalate: true`
6. Write packet to `divisions/personal/packets/burnout-monitor.json`
7. Notify Realm Keeper: skill `burnout-monitor` checked (no XP — informational only)

### Health Logger (daily 18:00)
1. Send health check-in prompt to Matthew via Telegram (through J_Claw gateway)
2. Wait for response (30-minute window)
   - If no response in 30 minutes: send one reminder
   - If no response in 2 hours: mark as skipped
3. Parse natural-language response
4. Save entry to `state/health-log.json` (retained for backward compatibility)
5. Also save entry to hot cache: `divisions/personal/hot/health-{date}.json`
6. Call artifact-manager → `index_extracted()` on health entry
7. Compile executive packet with today's health summary
8. Write packet to `divisions/personal/packets/health-logger.json`
9. Notify Realm Keeper: skill `health-logger` completed (+15 division XP)

### Performance Correlation (daily 20:00)
Requires: health-logger and trading-report packets from today.
1. Hydrate: today's health entry (from hot cache), last 14 days of health entries, trading packet for today
2. Run perf-correlation skill — cross-reference health variables against trading performance
3. Only surface patterns with ≥ 3 matching data points — never infer from one day
4. Compile executive packet with correlation findings
5. Write packet to `divisions/personal/packets/perf-correlation.json`
6. Notify Realm Keeper: skill `perf-correlation` completed (+10 division XP)

## Executive Packet — Personal
```json
{
  "division": "personal",
  "generated_at": "",
  "skill": "burnout-monitor | health-logger | perf-correlation",
  "status": "success | partial | failed",
  "summary": "",
  "action_items": [],
  "metrics": {
    "health_logged": false,
    "sleep_hours": null,
    "sleep_quality": null,
    "pattern_found": false,
    "burnout_risk": "none | low | medium | high"
  },
  "artifact_refs": [],
  "escalate": false,
  "escalation_reason": ""
}
```

## Escalation Criteria
Escalate (`escalate: true`) when:
- Burnout monitor detects HIGH risk indicators
- Health log missing for 3+ consecutive days
- Perf-correlation finds a strong negative pattern (e.g., consistent R drop on low-sleep days) that warrants immediate action recommendation

Do NOT escalate for:
- Normal health log (logged, no pattern)
- "No significant pattern" correlation result
- Single skipped health log

## Archive Policy
- Archive at: monthly checkpoint (first day of each month)
- Bundle contents: 30 days of health entries, correlation summaries, burnout scores
- Retain hot cache for: 14 days (correlation window needs recent history hot)
- Sensitivity: HIGH — health data never referenced in plain Telegram messages

## Error Handling
- If health prompt fails to send: log error, retry after 5 minutes (max 3 retries)
- If response is "skip": log entry with `skipped: true`, all fields null
- If perf-correlation has insufficient history (<3 data points): note in packet, do not infer
- If trading packet unavailable for today: run correlation on health only, note it
- Never fabricate health data or patterns
