---
name: perf-correlation
description: Cross-reference today's health log against trading performance to surface meaningful patterns between lifestyle variables and execution quality. Runs daily at 8PM after health-logger and trading-report.
schedule: daily 20:00
division: personal
---

## Trigger
Runs daily at 20:00 (8PM). Requires health-logger and trading-report to have run first.
If either state file has no entry for today: note it and output what's available.

## Steps

1. **Load today's data**

   From `C:\Users\Matty\OpenClaw-Orchestrator\state\health-log.json`:
   - Find today's entry (match by `date` field = today's YYYY-MM-DD)
   - Extract: sleep_hours, sleep_quality, adderall_dose, adderall_time, exercise_type, exercise_duration_min, hydration, food

   From `C:\Users\Matty\OpenClaw-Orchestrator\state\trade-log.json`:
   - Find today's session (match by `date` field)
   - Extract: total_trades, win_rate, avg_r, total_pnl, trades array

2. **Load historical data (rolling 14 days)**
   Read the last 14 entries from both files to establish baseline patterns.
   Only surface a pattern if it appears consistently across ≥3 data points — never infer from one day.

3. **Run correlations**
   Check these specific relationships:

   | Health Variable | Trading Variable | Pattern to detect |
   |---|---|---|
   | sleep_hours < 6 | win_rate | Lower win rate after poor sleep? |
   | sleep_quality ≤ 4 | avg_r | R multiples compressed on low-quality sleep days? |
   | adderall_time (late vs early) | total_trades | Overtrading when Adderall taken late? |
   | adderall_dose | win_rate | Dose correlation with performance? |
   | exercise_duration_min > 0 | avg_r | Better R on exercise days? |
   | hydration (low) | win_rate | Dehydration impact on decisions? |
   | no food / late food | total_trades | Hunger-driven overtrading? |

4. **Filter results**
   - Only report patterns with clear directional signal (≥3 matching data points)
   - Skip any variable with fewer than 3 historical data points
   - Do not report obvious or generic correlations — only ones specific to Matthew's data
   - If no meaningful pattern: explicitly state "No significant pattern detected today — need more data."

5. **Format output**
   ```
   J_Claw // Performance Correlation — {date}

   TODAY
   Sleep: {h}h / quality {q}/10 | Adderall: {dose} @ {time}
   Exercise: {type} {duration}min | Hydration: {level}
   Trades: {n} | Win rate: {%} | Avg R: {r}

   PATTERN (if found)
   {1-2 lines max — specific, data-backed, actionable}
   Example: "On days with <6h sleep (4 of last 14), avg R drops to {x} vs {y} baseline."

   RECOMMENDATION (only if pattern is strong)
   {1 line — concrete action, not generic advice}
   Example: "Consider no-trade rule on <6h sleep days."
   ```

6. **Save to state**
   Append correlation result to `C:\Users\Matty\OpenClaw-Orchestrator\state\health-log.json`
   under today's entry as:
   ```json
   "correlation": {
     "pattern_found": true/false,
     "pattern": "...",
     "recommendation": "..."
   }
   ```

7. **Send to Telegram**
   Send the formatted output to Matthew.
   If no pattern found: still send the TODAY section — it confirms the system is running.

8. **Pass to daily-briefing**
   The briefing skill will read the correlation result from health-log.json automatically.

## Output
- Updated `C:\Users\Matty\OpenClaw-Orchestrator\state\health-log.json` (correlation field on today's entry)
- Telegram message at 8PM

## Error Handling
- If health-log has no entry for today: send "Health log not found for today — run health-logger first."
- If trade-log has no entry for today: send correlation of health data only, note "No trades today"
- If fewer than 3 historical data points exist: note "Insufficient history for pattern detection" — do not infer
- Never fabricate patterns or extrapolate beyond the data
