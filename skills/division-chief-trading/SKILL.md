---
name: division-chief-trading
description: Trading division orchestrator. Runs market scans, backtester, and trading-report skills using a local GGUF model. Manages division artifact bundles. Compiles and delivers executive_packet.json to J_Claw.
division: trading
model: config-driven (see divisions/trading/config.json → model.path)
schedule: every 1 hour during market hours; daily 18:00 for report
---

## Role
This is the Trading Division Orchestrator. It runs entirely on the local GGUF model.
It does NOT call Claude. All raw data processing, summarization, and artifact management
happen at this layer. J_Claw receives only the compiled executive packet.

## Model
- Load model path from `divisions/trading/config.json` → `model.path`
- Use mmap loading — never reload the model between tasks in the same session
- Never load a compressed model file — raw GGUF only

## Artifact Paths
- Cold archives: `divisions/trading/cold/`
- Manifests: `divisions/trading/manifests/`
- Hot cache: `divisions/trading/hot/`
- Index: `divisions/trading/index/`
- Packets: `divisions/trading/packets/`

## Scheduled Tasks

### Market Scan (every 1 hour, market hours)
1. Call artifact-manager → `cache_ttl_check()` to evict stale hot files
2. Read manifest for latest market snapshot bundle (if exists)
3. Hydrate only: strategy configs, current watchlist, last session stats
4. Run market-scan skill with hydrated context
5. Append scan result to hot index
6. If actionable signal found: set `escalate: true` in packet
7. Compile and write executive packet to `divisions/trading/packets/market-scan.json`
8. Notify Realm Keeper: skill `market-scan` completed (triggers XP grant)

### Trading Report (daily 18:00)
1. Call artifact-manager → `cache_ttl_check()`
2. Run trading-report skill — reads Alpaca state files, calculates session stats
3. Save today's trade bundle to `divisions/trading/hot/`
4. Call artifact-manager → `index_extracted()` on trade bundle
5. Compile executive packet with session metrics
6. At checkpoint (18:00 run): call artifact-manager → `archive()` to move hot → cold
7. Write executive packet to `divisions/trading/packets/trading-report.json`
8. Notify Realm Keeper: skill `trading-report` completed

### Backtester (daily 18:00, after trading-report)
1. Hydrate relevant strategy configs and historical data from cold
2. Run backtester skill
3. Save backtest output bundle to hot
4. Compile executive packet with backtest summary
5. Write to `divisions/trading/packets/backtester.json`

## Executive Packet — Trading
```json
{
  "division": "trading",
  "generated_at": "",
  "skill": "market-scan | trading-report | backtester",
  "status": "success | partial | failed",
  "summary": "",
  "action_items": [],
  "metrics": {
    "total_trades": 0,
    "win_rate": null,
    "avg_r": null,
    "total_pnl": null,
    "signals_found": 0
  },
  "artifact_refs": [],
  "escalate": false,
  "escalation_reason": ""
}
```

## Escalation Criteria
Escalate (`escalate: true`) when:
- Win rate drops below 40% for the session
- A trade exceeds 2× expected max loss
- Alpaca API returns auth error or connectivity failure
- Market scan detects a signal with confidence > 85% (high-priority alert)
- Backtester shows strategy drawdown exceeding defined threshold

Do NOT escalate for:
- No trades today (normal — send in briefing)
- Partial data (note in packet, continue)
- Low-confidence signals

## Archive Policy
- Archive at: end of each trading day (18:00 checkpoint)
- Bundle contents: closed trade records, session stats, strategy state snapshot
- Retain hot cache for: 24 hours after archiving (for perf-correlation access)
- Never archive open positions — wait for session close

## Error Handling
- If Alpaca state files missing: compile packet with `status: partial`, note in summary
- If model fails to load: escalate immediately, halt division tasks
- If hot cache exceeds max_hot_mb: evict oldest non-pinned files before hydrating
- Never silently fail — all errors surface in packet summary
