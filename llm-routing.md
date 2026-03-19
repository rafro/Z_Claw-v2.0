# LLM Routing Analysis — J_Claw v2
## Agent architecture: LLM orchestrators with Python tools

---

## Core Architecture Principle

Every division orchestrator IS an LLM agent. It does not get bypassed for "simple" tasks.
The distinction is between what the **LLM reasons about** vs. what **Python tools handle**:

```
Division Orchestrator (LLM)
    ├── tool: fetch_jobs()          → pure Python, returns raw data
    ├── tool: read_state_file()     → pure Python, returns JSON
    ├── tool: write_packet()        → pure Python, writes output
    └── reasoning: interprets results, scores, judges, escalates
```

The LLM reasons over results. Python tools handle I/O. The orchestrator is never bypassed.
This mirrors how Claude Code itself works — Claude reasons, tools execute.

---

## Hardware

- **Matthew's machine**: RTX 3060 Ti (8GB VRAM, CUDA), i7, 16GB RAM — always on
- **Friend's machine**: RX 9070 XT (16GB VRAM, ROCm), Ryzen 5600G, 32GB RAM — optional upgrade

---

## Tier Classification

Tiers describe **how much LLM reasoning** a skill requires and **which model** serves it.
All skills run inside an LLM orchestrator. Tiers do NOT indicate bypassing the orchestrator.

---

## Tier 0 — Python Tools Only (called BY the orchestrator, no LLM inference per call)

These are the **tools** the orchestrator invokes. They do pure I/O or computation and return
structured data back to the LLM. The orchestrator reasons over what the tool returns.

| Tool | What it does | Returns to orchestrator |
|---|---|---|
| `fetch_jobs()` | HTTP fetch + XML/JSON parse | Raw normalized listings array |
| `dedup_jobs()` | Compare against jobs-seen.json | New-only listings array |
| `calc_trading_stats()` | PnL sum, win rate, avg R | Stats dict |
| `update_xp()` | XP addition, rank threshold lookup, JSON write | Updated stats dict |
| `manage_artifacts()` | Zip/unzip, file copy, TTL check | Artifact status |
| `check_thresholds()` | Sleep avg < X, skipped count > Y | Boolean flags + values |

**Why no LLM per call:** These return deterministic data. The orchestrator LLM then
reasons over that data — "is this win rate a real pattern?" is the LLM's job, not the tool's.

---

## Tier 1 — Orchestrator Reasoning on 3060 Ti (Qwen2.5 7B / Llama 3.1 8B via Ollama + CUDA)

The LLM orchestrator handles these directly. Structured output. Fixed rubrics.
3060 Ti runs Q4_K_M 7B–8B at ~4–6GB VRAM with headroom remaining.

| Skill | Orchestrator task | Model |
|---|---|---|
| **hard-filter** | Score 5 axes, assign tier A/B/C/D, decide escalation | Qwen2.5 7B |
| **health-logger** | Parse free-form Telegram reply → structured JSON | Llama 3.1 8B |
| **funding-finder** | Score relevance + eligibility against fixed criteria | Qwen2.5 7B |
| **market-scan** | Interpret indicator data → signal present/absent | Qwen2.5 7B (if LLM needed) |
| **perf-correlation** | Find patterns in 14-day health+trading window | Qwen2.5 7B |
| **burnout-monitor** | Assess risk from sleep/log data, give verdict | Qwen2.5 7B |
| **trading-report** | Interpret stats for anomalies and patterns | Qwen2.5 7B |

**Why this tier:**
- hard-filter: 8× daily — API cost compounds fast; fixed rubric = consistent 7B output
- health-logger: privacy non-negotiable — medication data never leaves the machine
- Others: low-complexity reasoning on fixed schemas; 7B is sufficient and local is free

**Models:**
- Primary: `qwen2.5:7b-instruct-q4_K_M` (~4.5GB VRAM)
- Privacy tasks: `llama3.1:8b-q4_K_M` (~5GB VRAM)

---

## Tier 2 — Deeper Reasoning on Friend's 9070 XT (Qwen2.5 14B via Ollama + ROCm)

16GB VRAM runs 14B Q4 comfortably. Better architectural judgment, broader training.
**Availability caveat:** not always on, ROCm for RDNA4 is new. Degrades to Tier 3.

| Skill | Why 14B | Model |
|---|---|---|
| **repo-monitor** | Architectural commentary, not just pattern matching | Qwen2.5 14B |
| **debug-agent** | Root cause analysis — 7B gives surface answers, 14B gives why | Qwen2.5 14B / DeepSeek Coder 14B |
| **refactor-scan** | Nuanced judgment on code quality, not rule-following | Qwen2.5 14B |
| **security-scan** | Broader security training coverage = better vulnerability detection | Qwen2.5 14B |
| **doc-update** | Higher quality prose for public-facing documentation | Qwen2.5 14B |

---

## Tier 3 — API Fallback (Gemini 2.0 Flash / Claude Haiku)

Activated when friend's machine is unavailable. Weekly tasks keep monthly spend at ~$1–5.

| Skill | API | Why |
|---|---|---|
| debug-agent | Claude Haiku | Best reasoning for complex errors |
| refactor-scan | Gemini Flash | Weekly; strong judgment, low cost |
| security-scan | Gemini Flash | Broad security knowledge |
| doc-update | Claude Haiku | Best prose quality |

---

## Tier 4 — Claude / J_Claw (executive layer only)

| Task | Why Claude |
|---|---|
| daily-briefing | Cross-division synthesis; voice and tone |
| Telegram composition | Consistency matters at the output layer |
| Escalation decisions | Requires the strongest reasoning |
| Direct Matthew queries | Real-time conversation |

---

## Full Routing Map

```
SKILL              FREQUENCY    SENSITIVITY   MODEL                  TIER
──────────────────────────────────────────────────────────────────────────
fetch_jobs()       tool call    low           Python (no model)      Tool
dedup_jobs()       tool call    low           Python (no model)      Tool
calc_trading()     tool call    low           Python (no model)      Tool
update_xp()        tool call    low           Python (no model)      Tool
──────────────────────────────────────────────────────────────────────────
hard-filter        every 3h     low           Qwen2.5 7B (3060 Ti)   1
health-logger      daily        HIGH          Llama 3.1 8B (3060 Ti) 1
funding-finder     daily        low           Qwen2.5 7B (3060 Ti)   1
trading-report     daily        low           Qwen2.5 7B (3060 Ti)   1
perf-correlation   daily        high          Qwen2.5 7B (3060 Ti)   1
burnout-monitor    daily        medium        Qwen2.5 7B (3060 Ti)   1
market-scan        hourly       low           Qwen2.5 7B (3060 Ti)   1
──────────────────────────────────────────────────────────────────────────
repo-monitor       every 3h     low           Qwen2.5 14B → Haiku    2→3
debug-agent        on-demand    medium        Qwen2.5 14B → Haiku    2→3
refactor-scan      weekly       low           Qwen2.5 14B → Flash    2→3
security-scan      weekly       low           Qwen2.5 14B → Flash    2→3
doc-update         weekly       low           Qwen2.5 14B → Haiku    2→3
──────────────────────────────────────────────────────────────────────────
daily-briefing     daily        low           Claude (J_Claw)        4
escalation         on event     varies        Claude (J_Claw)        4
Telegram compose   on event     varies        Claude (J_Claw)        4
```

---

## Recommended Stack

```
Ollama (CUDA — 3060 Ti)
  └── qwen2.5:7b-instruct-q4_K_M   → hard-filter, funding-finder, trading-report,
                                      perf-correlation, burnout-monitor, market-scan
  └── llama3.1:8b-q4_K_M           → health-logger (privacy)

Ollama (ROCm — friend's 9070 XT, optional)
  └── qwen2.5:14b-instruct-q4_K_M  → repo-monitor, debug-agent, refactor-scan,
                                      security-scan, doc-update

API fallbacks
  └── Gemini 2.0 Flash              → refactor-scan, security-scan (weekly)
  └── Claude Haiku                  → debug-agent, doc-update

Python tools (no model — called by orchestrator)
  └── fetch_jobs, dedup_jobs, calc_trading_stats, update_xp,
      manage_artifacts, check_thresholds, read_state, write_packet

Claude (J_Claw)
  └── daily-briefing, Telegram, escalations, direct Matthew queries
```
