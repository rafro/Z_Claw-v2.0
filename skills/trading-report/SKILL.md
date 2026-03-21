---
name: trading-report
description: Read today's closed trades from virtual_account.json, calculate daily session stats, save to state/trade-log.json, and compile an executive packet for the trading division chief. Run by division-chief-trading at 18:00 daily.
schedule: daily 18:00
division: trading
runner: division-chief-trading
---

## Trigger
Called by division-chief-trading at 18:00 daily. Also runs on manual invocation.
Do NOT call Claude directly — this skill runs under the local GGUF division orchestrator.

## Prerequisites
- If state file is missing: return `status: partial` packet with note "trading system not yet activated" — do not send Telegram
- If state file exists but has no trades for today: return packet noting "no trades logged today"

## Data Sources

### Primary: Virtual paper trading
```
C:\Users\Tyler\agent-network\state\virtual_account.json
```
Contains a `trade_log` array. Each record:
```json
{
  "order_id": "",
  "type": "entry | exit",
  "strategy_id": "",
  "side": "buy | sell",
  "symbol": "",
  "filled_price": null,
  "qty": null,
  "risk_usd": null,
  "reason": "",
  "pnl": null,
  "r_multiple": null,
  "timestamp": ""
}
```

**Which to use:** `virtual_account.json` is the sole data source. If a live CFD broker
integration is added in future, it will be checked first and virtual_account used as fallback.

## Steps

1. **Load state file**
   - Read appropriate state file (live or dry run)
   - Parse `trade_log` array
   - Filter to records where `timestamp` date matches today (YYYY-MM-DD)
   - Separate into entry records and exit records

2. **Pair trades**
   Match each exit to its entry by `strategy_id` + `symbol` (closest prior entry timestamp).
   Build trade record:
   ```json
   {
     "symbol": "",
     "strategy_id": "",
     "side": "buy | sell",
     "entry_price": null,
     "exit_price": null,
     "entry_time": "",
     "exit_time": "",
     "entry_reason": "",
     "exit_reason": "",
     "qty": null,
     "risk_usd": null,
     "pnl": null,
     "r_multiple": null,
     "result": "win | loss | breakeven"
   }
   ```
   - `r_multiple`: use from exit record if present; else calculate as `pnl / risk_usd`
   - `result`: win if pnl > 0, loss if pnl < 0, breakeven if pnl === 0

3. **Calculate session stats**
   - Total trades (paired exits only — no open positions)
   - Wins, losses, breakevens
   - Win rate: `wins / total * 100`
   - Avg R: mean of all `r_multiple` values
   - Best trade: symbol + r_multiple (highest)
   - Worst trade: symbol + r_multiple (lowest)
   - Total PnL: sum of all `pnl` values

4. **Save to state**
   Read `state/trade-log.json`. Append today's session:
   ```json
   {
     "date": "YYYY-MM-DD",
     "logged_at": "<ISO timestamp>",
     "source": "virtual_paper | cfd_demo",
     "trades": [],
     "stats": {
       "total_trades": 0,
       "wins": 0,
       "losses": 0,
       "win_rate": null,
       "avg_r": null,
       "best_r": null,
       "worst_r": null,
       "total_pnl": null
     }
   }
   ```
   Update rolling stats: total_trades, win_rate, avg_r.

5. **Save to hot cache**
   Write today's session bundle to `divisions/trading/hot/trade-session-{date}.json`
   Division chief will index and eventually archive this.

6. **Return results to division chief**
   Division chief compiles the executive packet and handles Telegram output.

## Executive Packet Contribution
trading-report contributes to division-chief-trading packet:
```json
{
  "metrics": {
    "total_trades": 0,
    "win_rate": null,
    "avg_r": null,
    "total_pnl": null,
    "best_r": null,
    "worst_r": null,
    "source": "virtual_paper | cfd_demo"
  },
  "summary": "Trades: {n} | W/L: {w}/{l} | Win Rate: {%} | Avg R: {r} | PnL: ${pnl}",
  "artifact_refs": [{ "bundle_id": "trade-session-{date}", "location": "hot" }]
}
```

Escalation triggers (set in division chief packet):
- Win rate < 40% on a day with ≥ 5 trades
- Single trade loss exceeds 2× expected max risk

## Error Handling
- Both state files missing: return `status: partial`, note "trading system not yet activated"
- No trades today: return `status: success`, metrics all zero, summary "No trades logged today"
- All open positions, no exits: summary "No closed trades today. {N} positions still open"
- Telegram fallback (handled by J_Claw on packet receipt): save to `reports/trade-report-{date}.md`
- Never fabricate or estimate trade data
