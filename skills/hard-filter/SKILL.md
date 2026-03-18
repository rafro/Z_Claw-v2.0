---
name: hard-filter
description: Apply Matthew's strict location/pay/category filters to raw job listings, score passing jobs across 5 axes, assign tiers A-D, and send Tier A & B to Telegram for review.
division: opportunity
trigger: after each job-intake run
---

## Trigger
Runs automatically after job-intake completes. Receives new_jobs array as input.

## Filter Rules

### ACCEPT

**Remote positions:**
- Software development (any stack matching Matthew's)
- AI / automation roles
- Blockchain / crypto / DeFi / Web3
- Technical analyst
- Telecom sales: $16–$23/hr range
- Customer support: ~$20/hr

**Local — Campbellton–Bathurst corridor:**
- Any category: $25/hr minimum only
- Reject anything local under $25/hr without exception

**Toronto / GTA:**
- 6-figure salary potential only ($100k+ path)
- Reject Toronto roles without clear 6-figure trajectory

### REJECT IMMEDIATELY
- Local jobs under $25/hr
- Relocation requirements with weak compensation
- Unrelated careers (retail, trades, healthcare, etc.)
- Vague postings with no pay information and no clear role
- Obvious scams or MLM-style listings

## Steps

1. **Load input**
   - Receive new_jobs array from job-intake
   - If empty: exit silently, no Telegram message needed

2. **Apply hard filters**
   - For each job: evaluate against ACCEPT/REJECT rules above
   - Tag rejected jobs with `filtered: true` and `tier: "D"` — do not score them
   - Accepted jobs proceed to scoring

3. **Score each accepted job** across 5 axes (0–10 each):
   - **Resume compatibility**: How well does this match Matthew's stack and experience?
   - **Compensation & lifestyle fit**: Does pay meet minimum thresholds? Remote-friendly?
   - **Interview probability**: Is Matthew likely to get a callback given the requirements?
   - **Career leverage**: Does this role open doors, build credibility, or add useful skills?
   - **Application complexity**: How much effort is required to apply? (lower effort = higher score)

4. **Calculate composite score**
   - Weighted average: resume_compat×0.25 + compensation×0.25 + interview_prob×0.20 + career_leverage×0.20 + app_complexity×0.10

5. **Assign tier**
   - **Tier A** (score ≥ 8.0): High priority — strong pay, strong match, strategic
   - **Tier B** (score 6.0–7.9): Review — decent opportunity, needs manual decision
   - **Tier C** (score 4.0–5.9): Interim income — acceptable but not strategic
   - **Tier D** (score < 4.0 OR hard rejected): Reject — do not surface

6. **Assign resume**
   Tag each accepted job with the correct resume before saving:
   - `resume: "technical"` → software development, AI/automation, blockchain/crypto/DeFi/Web3, fintech, trading, technical analyst, anything involving code
   - `resume: "general"` → telecom sales, customer support, call centers, sales, non-technical roles
   Never ask Matthew which resume to use — route automatically based on role type.

7. **Update state**
   - Write Tier A, B, C jobs to `state/applications.json` pipeline (include `resume` field)
   - Update `stats.pending_review` count

8. **Send Telegram report** (Tier A & B only)
   Format per job:
   ```
   [TIER A] Senior Solidity Dev — Remote
   Pay: $120k | Source: Web3.career
   Fit: 9.1/10 | Interview prob: 8/10
   Resume: Technical
   Link: https://...
   ```
   Group Tier A first, then Tier B. Add header:
   ```
   J_Claw // Opportunity Report — {timestamp}
   {count} new jobs | {tier_a} Tier A | {tier_b} Tier B
   ```

## Output
- Updated `state/applications.json`
- Telegram message with Tier A & B listings

## Error Handling
- If scoring fails for a job: log error, assign Tier C as fallback, flag for manual review
- Never send applications — output is always for Matthew's review only
- If Telegram send fails: save report to `reports/job-report-{date}.md` and retry on next run
