---
name: health-logger
description: Prompt Matthew on Telegram at 6PM daily to collect health and lifestyle data. Save structured entries to state/health-log.json and hot cache. Compile executive packet for personal division chief.
schedule: daily 18:00
division: personal
runner: division-chief-personal
---

## Trigger
Called by division-chief-personal at 18:00. Sends prompt to Matthew via Telegram and collects responses.
Do NOT call Claude directly — this skill runs under the local GGUF division orchestrator.
Telegram interaction is routed through J_Claw gateway.

## Steps

1. **Send opening prompt via J_Claw gateway**
   ```
   J_Claw // Daily Health Check-In — {date}

   Reply with your data for today:
   1. Food: what did you eat and when?
   2. Hydration: approx. water intake?
   3. Adderall: dose + time taken
   4. Exercise: type + duration (or "none")
   5. Sleep: hours last night + quality (1-10)

   Reply "skip" to mark today as no data.
   ```

2. **Wait for response** (30-minute window)
   - If no response in 30 minutes: send one reminder via gateway
   - If no response in 2 hours from initial prompt: mark entry as skipped

3. **Parse response**
   Extract from Matthew's natural-language reply:
   - `food`: array of meals with approximate times
   - `hydration`: string (e.g., "2L", "not much")
   - `adderall_dose`: string (e.g., "20mg")
   - `adderall_time`: string (e.g., "9am")
   - `exercise_type`: string (e.g., "30min walk", "none")
   - `exercise_duration_min`: number or null
   - `sleep_hours`: number
   - `sleep_quality`: number 1–10

4. **Validate completeness**
   - Required fields: sleep_hours, sleep_quality
   - If critical fields missing: ask one targeted follow-up (one message only)

5. **Save to state**
   Write to: `state/health-log.json`
   - Append new entry to `entries` array
   - Update `last_logged` field

   Entry schema:
   ```json
   {
     "date": "YYYY-MM-DD",
     "logged_at": "<ISO timestamp>",
     "skipped": false,
     "food": [],
     "hydration": "",
     "adderall_dose": "",
     "adderall_time": "",
     "exercise_type": "",
     "exercise_duration_min": null,
     "sleep_hours": null,
     "sleep_quality": null
   }
   ```

6. **Save to hot cache**
   Write entry to `divisions/personal/hot/health-{date}.json`
   Division chief will index it for perf-correlation access.

7. **Confirm to Matthew via gateway**
   ```
   Logged. See you tomorrow at 6PM.
   ```

8. **Return results to division chief**
   Division chief compiles executive packet.

## Executive Packet Contribution
health-logger contributes to division-chief-personal packet:
```json
{
  "metrics": {
    "health_logged": true,
    "sleep_hours": null,
    "sleep_quality": null,
    "skipped": false
  },
  "summary": "Health logged | Sleep: {h}h / quality {q}/10 | Adderall: {dose} @ {time}",
  "artifact_refs": [{ "bundle_id": "health-{date}", "location": "hot" }]
}
```

Sensitivity: HIGH — health data details are never sent to Telegram in raw form.
J_Claw shows only summary line (sleep hours, exercise, skip status).

## Error Handling
- If Telegram send fails: log error, retry after 5 minutes (max 3 retries)
- If response is "skip": log with `skipped: true`, all fields null
- If Matthew unresponsive for 2 hours: log as skipped
- Never send multiple reminders — one follow-up only
- DO NOT save a .txt file. Only valid output is state/health-log.json and hot cache entry
