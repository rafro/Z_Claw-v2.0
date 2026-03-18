---
name: daily-briefing
description: Read all division state files, compose a concise executive summary of the day's activity, save to state/briefing.json, and send to Matthew via Telegram at 9PM.
schedule: daily 21:00
division: all
---

## Trigger
Runs daily at 21:00 (9PM). Runs after perf-correlation completes.

## Steps

1. **Read all state files**
   Collect today's data from:
   - `C:\Users\Matty\OpenClaw-Orchestrator\state\orchestrator-state.json` — division statuses, last run times
   - `C:\Users\Matty\OpenClaw-Orchestrator\state\jobs-seen.json` — total_seen, last_run
   - `C:\Users\Matty\OpenClaw-Orchestrator\state\applications.json` — pending_review, applied, interviews
   - `C:\Users\Matty\OpenClaw-Orchestrator\state\trade-log.json` — today's session stats if present
   - `C:\Users\Matty\OpenClaw-Orchestrator\state\health-log.json` — last_logged, today's entry if present
   - `C:\Users\Matty\OpenClaw-Orchestrator\state\activity-log.json` — entries from today only

2. **Compose briefing**
   Write a concise 10–16 line executive summary. Structure:

   ```
   J_Claw // Daily Briefing — {YYYY-MM-DD}
   ━━━━━━━━━━━━━━━━━━━━━━━━

   [ OPPORTUNITY ]
   Jobs scanned: {total_seen} total | {N} new today
   Pipeline: {pending_review} pending review | {applied} applied | {interviews} interviews
   {If Tier A/B found today: "New Tier A/B jobs waiting — check Mission Control"}

   [ TRADING ]
   {If trades today: "Trades: {total} | W/L: {wins}/{losses} | Avg R: {avg_r} | PnL: ${pnl}"}
   {If no trades: "No trades logged today."}

   [ DEV ]
   {If repo-monitor ran: "Repos checked: {N} | {flags} flags raised"}
   {If not: "Repo monitor did not run today."}

   [ PERSONAL ]
   {If health logged: "Health logged ✓ | Sleep: {h}h | Exercise: {type}"}
   {If not logged: "⚠ Health log missing today"}
   {If perf-correlation surfaced a pattern: include it here — 1 line max}

   [ SYSTEM ]
   {Any divisions in error state}
   {Any skills that failed today}

   ━━━━━━━━━━━━━━━━━━━━━━━━
   Next run: 09:00 AM
   ```

   Rules:
   - Do NOT pad with filler. Every line must contain real data.
   - Omit any section that has nothing meaningful to report.
   - If a division ran with no issues and no notable output, one line is enough.
   - Lead with what needs Matthew's attention — action items first.

3. **Save briefing**
   Write to `C:\Users\Matty\OpenClaw-Orchestrator\state\briefing.json`:
   ```json
   {
     "last_generated": "<ISO timestamp>",
     "content": "<full briefing text>",
     "generated_by": "J_Claw"
   }
   ```

4. **Send to Telegram**
   Send the full briefing text to Matthew.

## Output
- Updated `C:\Users\Matty\OpenClaw-Orchestrator\state\briefing.json`
- Telegram message at 9PM

## Error Handling
- If a state file is missing or unreadable: note it in the briefing as "data unavailable" — do not abort
- If Telegram fails: save to `C:\Users\Matty\OpenClaw-Orchestrator\reports\briefing-{date}.md` and retry once
- Never fabricate stats — only report what the state files contain
- If no meaningful activity across any division: send "Quiet day — no significant activity to report." and stop
