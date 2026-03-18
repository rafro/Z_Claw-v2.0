---
name: funding-finder
description: Scan publicly available grant databases, accelerator listings, and ecosystem funding programs for opportunities matching Matthew's focus areas. Run daily at 2PM, send relevant results to Telegram.
schedule: daily 14:00
division: opportunity
---

## Trigger
Runs daily at 14:00 (2PM).

## Focus Areas
Scan for funding opportunities in:
- Software / SaaS products
- AI tools and automation
- Fintech and trading platforms
- DeFi / Web3 / blockchain
- Gaming tools and infrastructure
- Canadian startup programs (priority — Matthew is in NB)

## Sources

Fetch from these free, public sources only. No login required.

| Source | URL | Coverage |
|---|---|---|
| Canada Business Network grants | `https://innovation.ised-isde.canada.ca/s/list-liste?language=en&token=a2T4H000002nCMZUA2` | Federal grants |
| NRC IRAP | `https://nrc.canada.ca/en/support-technology-innovation/nrc-industrial-research-assistance-program` | R&D funding NB eligible |
| BDC programs | `https://www.bdc.ca/en/financing` | Business funding Canada |
| Accelerate NB | `https://www.acceleratenb.ca` | NB-specific startup support |
| Y Combinator (open calls) | `https://www.ycombinator.com/apply` | Global accelerator |
| Ethereum Foundation grants | `https://esp.ethereum.foundation` | Web3/Ethereum ecosystem |
| Gitcoin Grants | `https://grants.gitcoin.co` | Open source / DeFi |
| Solana Foundation grants | `https://solana.org/grants` | Solana ecosystem |

Do NOT attempt to scrape pages requiring login or CAPTCHA.
If a source is unreachable: log the error and continue — never abort the full run.

## Steps

1. **Pre-flight check**
   - Read `C:\Users\Matty\OpenClaw-Orchestrator\state\funding-seen.json` if it exists
   - Build a set of seen funding IDs (use URL as unique ID)
   - If file missing: create it with `{ "seen": [], "last_run": null }`

2. **Fetch each source**
   For each source:
   - Fetch the page/feed
   - Extract: name, description, amount (if listed), deadline (if listed), eligibility, URL
   - If fetch fails: log to `C:\Users\Matty\OpenClaw-Orchestrator\logs\funding-finder-errors.log`, continue

3. **Filter results**
   Keep only opportunities that match ALL of:
   - Relevant to Matthew's focus areas (software, AI, fintech, Web3, gaming, DeFi)
   - Open / accepting applications (not expired)
   - Not already in funding-seen.json

   Reject:
   - Hardware, manufacturing, agriculture, healthcare, non-tech
   - Closed or expired programs
   - Programs requiring established revenue > $1M (Matthew is pre-revenue/freelance)

4. **Score each result**
   Rate 1–10 on:
   - **Eligibility fit**: Does Matthew qualify? (NB location, solo/small team, tech focus)
   - **Effort required**: How much work to apply? (lower effort = higher score)
   - **Potential value**: Grant size or program value

   Keep only results scoring ≥ 6 overall. Discard the rest silently.

5. **Update state**
   - Append all seen URLs to `funding-seen.json` (even rejected ones — avoid re-checking)
   - Update `last_run` to current ISO timestamp
   - Save file to `C:\Users\Matty\OpenClaw-Orchestrator\state\funding-seen.json`

6. **Send Telegram report** (new results only)
   If new qualifying results found:
   ```
   J_Claw // Funding Finder — {date}
   {N} new opportunities found

   [{score}/10] {Name}
   Amount: {amount or "unspecified"}
   Deadline: {deadline or "rolling"}
   Fit: {1-line eligibility summary}
   Link: {url}
   ```

   If no new results: send nothing — do not spam with empty reports.

## Output
- Updated `C:\Users\Matty\OpenClaw-Orchestrator\state\funding-seen.json`
- Telegram message if new qualifying results found

## Error Handling
- Per-source failure: log to `logs/funding-finder-errors.log`, continue with remaining sources
- If ALL sources fail: send Telegram alert "funding-finder: all sources failed at {timestamp}"
- If funding-seen.json is corrupt: recreate it from scratch with empty seen array
- Never send duplicate opportunities across runs
