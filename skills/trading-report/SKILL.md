---
name: trading-report
description: Read today's closed trades from Alpaca state files, calculate daily session stats, save to state/trade-log.json, and send a structured performance summary to Matthew via Telegram at 6PM.
schedule: daily 18:00
division: trading
---

## Trigger
Runs daily at 18:00 (6PM). Also runs on manual invocation from Matthew.

## Prerequisites
- Alpaca state files must exist at the paths listed below
- Credentials: load `ALPACA_API_KEY` and `ALPACA_API_SECRET` from `C:\Users\Matty\OpenClaw-Orchestrator\.env`
- If BOTH state files are missing: **silently exit — do not send any Telegram message**. The trading system has not been activated yet. This is expected until agent-network is running.
- If state files exist but are empty or have no trades for today: send "No trades logged today." message as normal

## Data Sources

### Live mode (Alpaca paper trading)
```
C:\Users\Matty\agent-network\state\alpaca_paper_state.json
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
  "timestamp": ""
}
```

### Dry run mode
```
C:\Users\Matty\agent-network\state\virtual_account.json
```
Same structure. Exit records also include `r_multiple` field.

**Which to use:** Check if `alpaca_paper_state.json` exists and has entries for today. If yes, use it.
If not found or empty for today, fall back to `virtual_account.json`.

## Steps

1. **Load state file**
   - Read the appropriate state file (live or dry run per above)
   - Parse the `trade_log` array
   - Filter to records where `timestamp` date matches today (YYYY-MM-DD)
   - Separate into entry records (`type === "entry"`) and exit records (`type === "exit"`)

2. **Pair trades**
   Match each exit record to its entry record by `strategy_id` + `symbol` (closest prior entry timestamp).
   For each matched pair, build a trade record:
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
   - `r_multiple`: use directly from exit record if present; otherwise calculate as `pnl / risk_usd`
   - `result`: win if pnl > 0, loss if pnl < 0, breakeven if pnl === 0

3. **Calculate session stats**
   - Total trades (paired exits only — do not count open positions)
   - Wins, losses, breakevens
   - Win rate: `wins / total * 100`
   - Avg R: mean of all `r_multiple` values
   - Best trade: symbol + r_multiple (highest)
   - Worst trade: symbol + r_multiple (lowest)
   - Total PnL: sum of all `pnl` values

4. **Save to state**
   Read `C:\Users\Matty\OpenClaw-Orchestrator\state\trade-log.json`.
   Append today's session record:
   ```json
   {
     "date": "YYYY-MM-DD",
     "logged_at": "<ISO timestamp>",
     "source": "alpaca_paper | dry_run",
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
   Update global rolling stats:
   - `stats.total_trades`: running total
   - `stats.win_rate`: rolling win rate across all sessions
   - `stats.avg_r`: rolling average R across all sessions

5. **Format Telegram summary**
   ```
   J_Claw // Trading Report — {date}

   Trades: {total} | W/L: {wins}/{losses} | Win Rate: {win_rate}%
   Avg R: {avg_r} | Total PnL: ${total_pnl}
   Best: {best_symbol} +{best_r}R | Worst: {worst_symbol} {worst_r}R

   {list each trade: symbol | side | result | R | PnL}

   Source: {alpaca_paper | dry_run}
   ```

6. **Send to Telegram**
   Send the formatted summary to Matthew.
   Also share session stats with Personal Optimization division for perf-correlation.

## Output
- Updated `C:\Users\Matty\OpenClaw-Orchestrator\state\trade-log.json`
- Telegram summary message at 6PM

## Error Handling
- If both state files missing: silently exit — no Telegram message (trading system not yet active)
- If no trades today: send "No trades logged today." — do not skip the send
- If trades exist but no exits (all open): send "No closed trades today. {N} positions still open."
- If Telegram fails: save report to `C:\Users\Matty\OpenClaw-Orchestrator\reports\trade-report-{date}.md` and retry once
- Never fabricate or estimate trade data — only report what the state files contain
