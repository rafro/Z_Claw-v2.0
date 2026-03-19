---
name: daily-briefing
description: Read executive packets from all four divisions, compose a concise executive summary of the day's activity, save to state/briefing.json, and send to Matthew via Telegram at 9PM. Run by J_Claw directly — the only skill J_Claw executes itself.
schedule: daily 21:00
division: all
runner: J_Claw
---

## Trigger
Runs daily at 21:00 (9PM). Runs after perf-correlation packet is available.
This is the ONE skill J_Claw executes directly. It reads division packets — not raw state files.

## Input — Division Packets

Read the most recent executive packet from each division:
- `divisions/trading/packets/trading-report.json`
- `divisions/opportunity/packets/job-intake.json`
- `divisions/dev-automation/packets/repo-monitor.json`
- `divisions/personal/packets/health-logger.json`
- `divisions/personal/packets/perf-correlation.json`

Also read: `state/jclaw-stats.json` for current rank/level (read-only).

If a packet is missing or older than 25 hours: note it as "data unavailable" — do not abort.
Do NOT read raw state files (jobs-seen.json, health-log.json, trade-log.json, etc.).

## Steps

1. **Load all division packets**
   Read each packet file listed above. Parse JSON. Note any missing or stale packets.

2. **Compose briefing**
   Write a concise 10–16 line executive summary:

   ```
   J_Claw // Daily Briefing — {YYYY-MM-DD}
   ━━━━━━━━━━━━━━━━━━━━━━━━

   [ OPPORTUNITY ]
   Jobs scanned: {total_seen} total | {N} new today
   Pipeline: {pending_review} pending review | {applied} applied | {interviews} interviews
   {If Tier A/B found today: "New Tier A/B jobs waiting — check Mission Control"}
   {If funding opportunities: "Funding: {N} new opportunities found"}

   [ TRADING ]
   {If trades today: "Trades: {total} | W/L: {wins}/{losses} | Avg R: {avg_r} | PnL: ${pnl}"}
   {If no trades: "No trades logged today."}

   [ DEV ]
   {If repo-monitor ran: "Repos checked: {N} | {h} HIGH, {m} MEDIUM, {l} LOW flags"}
   {If not: "Repo monitor did not run today."}

   [ PERSONAL ]
   {If health logged: "Health logged ✓ | Sleep: {h}h | Exercise: {type}"}
   {If not logged: "⚠ Health log missing today"}
   {If perf-correlation pattern: include it here — 1 line max}

   [ SYSTEM ]
   {Any divisions with status: failed or escalate: true unresolved}
   {Any skills that failed today}

   ━━━━━━━━━━━━━━━━━━━━━━━━
   Next run: 09:00 AM
   ```

   Rules:
   - Every line must contain real data from packets — no fabrication
   - Omit any section where packet is missing AND no notable absence to report
   - Lead with what needs Matthew's attention — action items first
   - Never pad with filler

3. **Save briefing**
   Write to `state/briefing.json`:
   ```json
   {
     "last_generated": "<ISO timestamp>",
     "content": "<full briefing text>",
     "generated_by": "J_Claw"
   }
   ```

4. **Send to Telegram**
   Send the full briefing text to Matthew.
   Sign off: `— J_Claw | {rank} | Lvl {level}`

## Output
- Updated `state/briefing.json`
- Telegram message at 9PM

## Error Handling
- If a packet is missing or unreadable: note "data unavailable" for that division — do not abort
- If Telegram fails: save to `reports/briefing-{date}.md` and retry once
- Never fabricate stats — only report what packets contain
- If no meaningful activity across any division: send "Quiet day — no significant activity to report."
