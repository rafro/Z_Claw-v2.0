# J_Claw — Personal AI Orchestration System

A modular, locally-hosted AI automation platform built on Windows 11. J_Claw runs a persistent Mission Control server that orchestrates Python skill agents across multiple life domains, with a desktop dashboard and mobile PWA accessible over a private network.

---

## Architecture

```
Mission Control (Node.js / PM2)
  └── server.js                 # HTTP server, WebSocket, SSE, skill runner
      ├── dashboard/            # Desktop dashboard (vanilla JS PWA)
      ├── mobile/               # Mobile PWA (Tailscale access)
      └── run_division.py       # Python runtime entry point

Python Skill Runtime
  └── runtime/
      ├── orchestrators/        # Per-division LLM orchestrators
      ├── skills/               # Individual skill modules
      └── tools/                # Shared data tools (trading, XP, state, etc.)

Divisions (agents)
  ├── trading/                  # Market scans, virtual paper trading, backtesting
  ├── opportunity/              # Job intake, filtering, funding discovery
  ├── dev-automation/           # Repo monitoring, refactor scans, code review
  ├── personal/                 # Health logging, performance correlation, burnout monitoring
  ├── op-sec/                   # Device posture, breach monitoring, privacy scans
  ├── production/               # AI media generation (images, sprites, audio, video)
  └── sentinel/                 # System health, provider uptime, queue monitoring

State
  └── state/                    # jclaw-stats.json, xp-history.jsonl, anim-queue.json, etc.
```

---

## Key Features

- **Multi-division agent orchestration** — each division runs scheduled Python skills via `run_division.py`, with results written as JSON packets
- **LLM routing** — Tier 0 (pure Python), Tier 1 (local Ollama 7B), Tier 2 (GPT-4o) per skill
- **Gamification** — XP, per-division ranks, streaks, streak multipliers, prestige system, achievements
- **Theater system** — animated battle scenes queued from division activity, viewable on both desktop and mobile
- **Real-time updates** — WebSocket + SSE streams push live events to dashboard and mobile
- **Realm Layer** — commanders (VAEL, SEREN, KAELEN, LYRIN, ZETH, LYKE) represent each division's identity
- **Virtual paper trading** — SPX500 and Gold simulation via yfinance with real market data, no broker required
- **Mobile PWA** — full-featured mobile interface accessible over Tailscale private network

---

## Divisions & Agents

| Division | Key Agents | Schedule |
|---|---|---|
| **Trading** | market-scan, virtual-trader, backtester, trading-report | Hourly / Daily 18:00 |
| **Opportunity** | job-intake, hard-filter, funding-finder | Every 3h / Daily 14:00 |
| **Dev Automation** | repo-monitor, refactor-scan, debug-agent, dev-digest | Daily |
| **Personal** | health-logger, perf-correlation, burnout-monitor | Daily |
| **Op-Sec** | device-posture, threat-surface, breach-check, cred-audit, privacy-scan | Daily / Weekly |
| **Production** | image-generate, sprite-generate, prompt-craft, asset-catalog, production-digest | On-demand / Daily |
| **Sentinel** | provider-health, queue-monitor, sentinel-digest | Every 30min / Daily |

---

## Stack

- **Runtime**: Node.js 20 (Mission Control), Python 3.13 (skills)
- **Process manager**: PM2
- **Local LLM**: Ollama (Qwen2.5 7B for skills, Llama 3.1 20B for Discord bot)
- **Cloud LLM**: OpenAI GPT-4o (production division)
- **Image generation**: ComfyUI (local)
- **Trading data**: yfinance
- **Private networking**: Tailscale
- **Notifications**: Telegram bot, Discord (Zenith bot)

---

## Security

- CORS restricted to localhost and private Tailscale CGNAT range
- Bearer token + PIN authentication for mobile access
- Timing-safe PIN comparison (`crypto.timingSafeEqual`)
- Windows Firewall rule scoped to local subnet + Tailscale
- Health and credential data stays local — no API fallback for sensitive skills

---

## Changelog

See commit history for detailed change notes. Major milestones:

- **2026-03-22** — Trading account growth tracking fixed; backtester wired to skill runner; source label corrected (`virtual_account` vs `dry_run`)
- **2026-03-22** — Phase 2 gamification: auto-prestige, streak multiplier SSE, full-screen rank-up overlay, stats division breakdown
- **2026-03-22** — Security hardening: CORS preflight fix, timing-safe PIN, all op-sec division agents wired
- **2026-03-22** — Production division: storage increased to 10GB, all agent skill keys verified
- **Earlier** — Virtual paper trader, backtester, market-scan, trading-report pipeline built
- **Earlier** — Mobile PWA with haptics, XP floats, commander panels, theater system
- **Earlier** — Realm Layer architecture: commanders, orders, chronicle, directive endpoint
