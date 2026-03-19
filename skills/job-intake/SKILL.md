---
name: job-intake
description: Fetch job listings every 3 hours from free APIs and RSS feeds, normalize to standard schema, deduplicate against previously seen jobs, and pass new listings to hard-filter. Run by the opportunity division orchestrator. Outputs executive packet.
schedule: every 3 hours
division: opportunity
runner: division-chief-opportunity
---

## Trigger
Called by division-chief-opportunity on schedule. Also runs on manual invocation from Matthew.
Do NOT call Claude directly — this skill runs under the local GGUF division orchestrator.

## Sources

| Source | Type | Coverage | Salary | Status |
|---|---|---|---|---|
| We Work Remotely | RSS | Remote global | No | ✅ Live |
| Remote OK | REST API | Remote global | Yes | ✅ Live |
| Remotive | REST API | Remote global | Partial | ✅ Live (rate-limit aware) |
| Adzuna | REST API | Canada — NB, Toronto, Remote | Yes | ✅ Live (API key required) |
| Web3.career | RSS | — | — | ❌ Dead (500/404) |
| CryptoJobsList | RSS | — | — | ❌ Blocked (403) |
| Remote.co | RSS | — | — | ❌ Timeout |

Do NOT attempt to scrape LinkedIn, Indeed, or any job board that requires login.
Web3.career, CryptoJobsList, and Remote.co are confirmed dead/blocked as of 2026-03-17.
Adzuna credentials are stored in `C:\Users\Matty\OpenClaw-Orchestrator\.env` — load from there, never hardcode.

## Fetch Methods

### We Work Remotely (RSS)
```
GET https://weworkremotely.com/remote-jobs.rss
```
Returns XML. Parse each `<item>`. Use `<link>` as unique job ID.
Fields: `<title>`, `<link>`, `<description>`, `<pubDate>`, `<region>`.

### Remote OK (REST API)
```
GET https://remoteok.com/api
```
Returns JSON array. First element is metadata — skip it, parse from index 1.
Fields: id, url, title, company, location, salary_min, salary_max, tags, date.

### Remotive (REST API — use only if no rate limit this session)
```
GET https://remotive.com/api/remote-jobs
```
Returns JSON array. Fields: id, url, title, company_name, candidate_required_location, salary, description, job_type, tags, publication_date.

### Adzuna (REST API — Canadian coverage)
Credentials: load `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` from `C:\Users\Matty\OpenClaw-Orchestrator\.env`

Run all three queries per cycle:

Note: /ca/ endpoint returns 0 results — Canada is not indexed by Adzuna. Use /us/ for all queries.
Remote roles post to US endpoint regardless of location. Toronto/NB filtering is handled by hard-filter.

**Query 1 — Remote Web3/tech roles:**
```
GET http://api.adzuna.com/v1/api/jobs/us/search/1
  ?app_id={ADZUNA_APP_ID}&app_key={ADZUNA_APP_KEY}
  &what=blockchain+OR+solidity+OR+web3+OR+defi+OR+AI+developer
  &where=remote&salary_min=60000&results_per_page=50&sort_by=date
```

**Query 2 — Remote software dev / high-comp roles:**
```
GET http://api.adzuna.com/v1/api/jobs/us/search/1
  ?app_id={ADZUNA_APP_ID}&app_key={ADZUNA_APP_KEY}
  &what=software+developer+OR+engineer+OR+technical+analyst
  &where=remote&salary_min=100000&results_per_page=50&sort_by=date
```

**Query 3 — Remote support / telecom sales:**
```
GET http://api.adzuna.com/v1/api/jobs/us/search/1
  ?app_id={ADZUNA_APP_ID}&app_key={ADZUNA_APP_KEY}
  &what=telecom+sales+OR+customer+support+OR+technical+support
  &where=remote&salary_min=35000&results_per_page=20&sort_by=date
```

Returns JSON. Fields: id, title, company.display_name, location.display_name, salary_min, salary_max, redirect_url, created.

## Steps

1. **Pre-flight: API budget check**
   - If rate limit error received this session: skip Remotive, use RSS feeds only
   - If all sources previously failed this session: compile failure packet and return — do not retry
   - Prefer RSS sources at all times — zero-cost, no quotas

2. **Load seen jobs**
   - Read `C:\Users\Matty\OpenClaw-Orchestrator\state\jobs-seen.json`
   - Build seen set using composite key: `source + job_id`

3. **Fetch listings per source**
   - For each source: fetch using method above
   - If a source fetch fails: log the error, continue to next source
   - Never abort the full run due to a single source failure

4. **Normalize each listing** to standard schema:
   ```json
   {
     "id": "<source>-<job_id>",
     "title": "",
     "company": "",
     "location": "",
     "remote": true,
     "pay_min": null,
     "pay_max": null,
     "pay_type": "hourly | salary | unspecified",
     "description_summary": "",
     "url": "",
     "source": "",
     "fetched_at": "<ISO timestamp>",
     "seen": false,
     "filtered": false,
     "tier": null,
     "resume": null
   }
   ```
   - Extract pay from salary field or description where possible
   - If location is empty or "worldwide", set `remote: true`

5. **Deduplicate**
   - Compare each listing against seen set by composite ID
   - Skip any job already seen — never re-surface it
   - Only new jobs proceed

6. **Update state**
   - Read `state/jobs-seen.json`
   - Append new listings to `jobs` array
   - Update `last_run` to current ISO timestamp
   - Increment `total_seen` by count of new listings
   - Write updated JSON back

7. **Pass to hard-filter**
   - Return new listings array to division-chief-opportunity for hard-filter step

## Output
- Updated `state/jobs-seen.json`
- New listings array → passed to hard-filter within division orchestrator

## Executive Packet Contribution
job-intake contributes to the division-chief-opportunity packet:
- `metrics.new_jobs_found` — count of new listings passed to hard-filter
- `summary` — source status summary (which sources succeeded/failed)
- `artifact_refs` — reference to hot cache bundle of new job listings

## Error Handling
- Per-source failure: log to `logs/job-intake-errors.log`, continue
- If ALL sources fail: return failure status to division chief — it will escalate
- If state file is missing: create it with empty schema `{ "jobs": [], "last_run": null, "total_seen": 0 }`
- Never skip deduplication under any circumstances
