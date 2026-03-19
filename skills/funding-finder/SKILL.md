---
name: funding-finder
description: Scan publicly available grant databases, accelerator listings, and ecosystem funding programs for opportunities matching Matthew's focus areas. Run daily at 2PM by division-chief-opportunity. Compiles executive packet.
schedule: daily 14:00
division: opportunity
runner: division-chief-opportunity
---

## Trigger
Called by division-chief-opportunity at 14:00 daily.
Do NOT call Claude directly — this skill runs under the local GGUF division orchestrator.

## Focus Areas
- Software / SaaS products
- AI tools and automation
- Fintech and trading platforms
- DeFi / Web3 / blockchain
- Gaming tools and infrastructure
- Canadian startup programs (priority — Matthew is in NB)

## Sources

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
   - Read `state/funding-seen.json` if it exists — build set of seen URLs
   - If file missing: create with `{ "seen": [], "last_run": null }`

2. **Fetch each source**
   - Extract: name, description, amount (if listed), deadline (if listed), eligibility, URL
   - If fetch fails: log to `logs/funding-finder-errors.log`, continue

3. **Filter results**
   Keep only opportunities matching ALL:
   - Relevant to focus areas (software, AI, fintech, Web3, gaming, DeFi)
   - Open / accepting applications (not expired)
   - Not already in funding-seen.json
   - Not requiring established revenue > $1M

4. **Score each result** (1–10):
   - **Eligibility fit**: Does Matthew qualify? (NB location, solo/small team, tech focus)
   - **Effort required**: How much work to apply? (lower effort = higher score)
   - **Potential value**: Grant size or program value
   Keep only results scoring ≥ 6. Discard the rest silently.

5. **Update state**
   - Append all seen URLs to `funding-seen.json` (even rejected ones)
   - Update `last_run`
   - Save to hot cache: `divisions/opportunity/hot/funding-{date}.json`

6. **Return results to division chief**
   Division chief compiles executive packet and handles Telegram output.

## Executive Packet Contribution
funding-finder contributes to division-chief-opportunity packet:
```json
{
  "metrics": {
    "funding_opportunities": 0
  },
  "summary": "{N} new funding opportunities found | {top_name} — {amount}",
  "action_items": [
    {
      "priority": "medium",
      "description": "[{score}/10] {Name} | Amount: {amount} | Deadline: {deadline} | {eligibility} | Link: {url}",
      "requires_matthew": true
    }
  ]
}
```

If no new results: `action_items` is empty, `funding_opportunities` is 0.
J_Claw only sends Telegram if there are new results in the packet.

## Error Handling
- Per-source failure: log to `logs/funding-finder-errors.log`, continue
- If ALL sources fail: set `status: failed` in packet contribution — division chief escalates
- If `funding-seen.json` corrupt: recreate with empty seen array
- Never send duplicate opportunities across runs
