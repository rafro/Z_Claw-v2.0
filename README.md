# Z_Claw v2.0 — Personal AI Orchestration Platform

A modular, locally-hosted AI automation system running on Windows 11. Z_Claw orchestrates 7 specialized agent divisions (~55 agents) across trading, security, personal health, dev automation, and media production — all routed through a persistent Node.js Mission Control server with desktop and mobile dashboards.

Built for two users: **Tyler** (PC dashboard, port 3000) and **Matthew** (mobile PWA via Tailscale, iPhone 16 Pro Max).

---

## v2.0 — Major Updates

This version reconciles Tyler's battle-tested production system (195 commits) with deep analytical capabilities from the feature branch. Key additions:

### Trading Engine — Institutional Signal Intelligence + Risk Management
- **Multi-factor signals**: RSI, MACD, ADX, Stochastic, VWAP, Bollinger Bands with 5-factor composite scoring (trend/momentum/volatility/volume/structure)
- **VIX circuit breakers**: VIX > 35 halts entries, VIX > 25 reduces position size 50%
- **Slippage modeling**: 5 bps per fill with empirical fill tracking
- **Instrument correlations**: Prevents doubling up on correlated assets (SPX500/NAS100 = 0.92)
- **Trailing stops**: 2x ATR from peak, alongside static stop-loss
- **Deep backtester**: Walk-forward (N-fold), Monte Carlo (1,000 paths), extended metrics (Calmar, Kelly, Ulcer Index), 0-100 health score

### Cross-Division Intelligence — Divisions Talk to Each Other
- **Breach gating**: OP-Sec incident pauses Trading + Production non-critical work
- **Burnout wires**: Personal burnout status throttles Trading risk + Opportunity job escalations
- **Escalation dedup**: Fingerprint-based alert suppression prevents Telegram spam
- **Model lock**: VRAM semaphore prevents concurrent Ollama model loading
- **Atomic writes**: Crash-safe JSON persistence via tempfile + os.replace

### Production — Complete Creative Studio (24 agents)
- **Art director**: LLM-driven creative briefs (local Ollama, daily 07:00)
- **Narrative craft**: Story/chronicle events → production scene generation
- **SFX generate**: 15 Web Audio API synthesis specs (pure Python)
- **Asset optimize**: ComfyUI RealESRGAN 4x upscale + PIL fallback + WebP conversion
- **Voice catalog**: Reference WAV status tracking per commander
- **QA pipeline**: Unified quality gate (style + audio + video + image review)
- **Model trainer**: QVAC BitNet LoRA fine-tuning orchestrator (human-initiated only)
- **Adapter manager**: LoRA adapter registry with activate/deactivate/rollback

### QVAC BitNet LoRA — Self-Improving Training Pipeline
Every LLM call is captured → human-reviewed → domain-split → formatted for QVAC → trained on AMD GPU → adapter deployed. See [QVAC Setup Guide](docs/QVAC_SETUP.md).

### Gamification Restored
- 15 quest templates with progress tracking
- 17 achievements (5 new: Fortnight Flame, Monthly Guardian, First Ascension, Thrice Ascended, Forge Ignited)
- Token-aware context trimming in all chat handlers

### Dashboard Parity
All frontends fully synced — every agent has buttons, packet displays, and metrics in both mobile PWA and PC dashboard. Ghost references cleaned up.

---

## Hardware

| Component | Spec |
|---|---|
| CPU | AMD Ryzen 5 5600G |
| GPU | AMD RX 9070 XT — 16GB VRAM (RDNA 4) |
| RAM | 32GB |
| OS | Windows 11 |
| Network | Tailscale (private CGNAT mesh) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Mission Control — server.js (Node.js, PM2, port 3000)  │
│                                                         │
│  ├── dashboard/index.html   PC dashboard (Catppuccin)   │
│  ├── mobile/index.html      Mobile PWA (9500+ lines)    │
│  ├── mission_control/       Task queue + approval gates  │
│  ├── state/                 Runtime state (JSON/JSONL)   │
│  └── providers/             LLM provider router          │
│       ├── OllamaProvider    (adapter-aware for LoRA)     │
│       ├── CaptureProvider   (training data capture)      │
│       ├── GroqProvider                                   │
│       └── DeterministicProvider                          │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP / SSE / WebSocket
┌──────────────────────▼──────────────────────────────────┐
│  Python Skill Runtime (~55 agents)                       │
│                                                         │
│  runtime/orchestrators/   Per-division LLM orchestrators │
│  runtime/skills/          Individual agent skill files   │
│  runtime/tools/           Shared utilities (XP, breach,  │
│                           escalation, atomic write, etc.) │
│  divisions/{div}/packets/ Executive Packet outputs       │
└─────────────────────────────────────────────────────────┘
```

---

## The 7 Divisions

| Division | Commander | Agents | Key Capabilities |
|---|---|---|---|
| **Trading** | SEREN | 5 | Multi-factor signals, VIX breakers, slippage, Monte Carlo backtesting |
| **Opportunity** | VAEL | 5 | Job intake/filter, application tracking, funding discovery |
| **Dev Automation** | KAELEN | 8 | Repo monitoring, refactoring, security scanning, artifact lifecycle |
| **Personal** | LYRIN | 6 | Health logging, burnout detection, performance correlation, weekly retros |
| **OP-Sec** | ZETH | 9 | Device posture, breach monitoring, credential audit, network profiling |
| **Production** | LYKE | 24 | Art direction, image/video/voice/music/SFX generation, QA, QVAC training |
| **Sentinel** | VEIL | 4 | Provider health, queue monitoring, agent-network staleness, system digest |

### Cross-Division Data Flows

```
                    ┌─────────────┐
                    │  SENTINEL   │ ← Watches ALL divisions for staleness
                    └──────┬──────┘
                           │
    ┌──────────────────────┼──────────────────────┐
    │                      │                      │
┌───▼───┐  breach gate  ┌──▼──┐  breach gate  ┌───▼────┐
│OP-SEC │──────────────►│TRADE│◄──────────────│DEV AUTO│
└───┬───┘              └──┬──┘              └────────┘
    │                     │
    │ breach gate    burnout│
    │                     │
┌───▼────┐          ┌────▼─────┐
│PRODUC- │          │ PERSONAL │◄──► OPPORTUNITY
│TION    │          └──────────┘     (burnout throttle)
└────────┘
```

---

## Production Division — Local Media Generation

All media generated entirely on-device via the AMD RX 9070 XT.

| Pipeline | Backend | Status |
|---|---|---|
| Images/Sprites | ComfyUI + SDXL (`animagine-xl-3.1`) | Working |
| Video | ComfyUI + AnimateDiff-Evolved | Working |
| Music | HuggingFace MusicGen + torch-directml | Working |
| Voice | Coqui XTTS v2 (CPU) | Working — needs reference WAVs |
| SFX | Web Audio API synthesis specs (pure Python) | Working |
| Art Direction | Local Ollama 7B creative briefs | Working |
| Narrative | Story → scene generation (Ollama 7B) | Working |
| QA | Unified style/audio/video/image review gate | Working |
| Upscaling | ComfyUI RealESRGAN 4x + PIL fallback | Working |

---

## QVAC BitNet LoRA — Self-Improving Pipeline

Every LLM call is silently captured via `CaptureProvider`. The system trains itself over time.

```
Agents run daily → CaptureProvider logs interactions
        │
        ▼
Human reviews (scripts/review_captures.py)
        │
        ▼
Export by domain (scripts/export_training_data.py)
        │
        ▼
Format for QVAC (scripts/format_for_qvac.py)
        │
        ▼
Train on RX 9070 XT — 13B BitNet in ~3GB VRAM (human-initiated)
        │
        ▼
Activate adapter (run_division.py production adapter-manager activate trading)
        │
        ▼
ProviderRouter auto-selects fine-tuned model → Better responses → Loop
```

**Target**: BitNet b1.58 13B (TQ1_0, ~2.8 GB VRAM), fine-tuned via Tether's QVAC Fabric (Vulkan, AMD native). See [docs/QVAC_SETUP.md](docs/QVAC_SETUP.md).

### Pipeline Components

| Component | File | Purpose |
|---|---|---|
| Capture | `providers/capture.py` | Wraps LLM calls, logs to training-capture.jsonl |
| Review | `scripts/review_captures.py` | Manual QA of captured pairs |
| Export | `scripts/export_training_data.py` | Domain-split JSONL for fine-tuning |
| Format | `scripts/format_for_qvac.py` | Convert to BitNet chat template |
| Train | `runtime/skills/model_trainer.py` | Build QVAC commands (never auto-runs) |
| Manage | `runtime/skills/adapter_manager.py` | Registry, activate/deactivate adapters |
| Manifest | `runtime/tools/training_manifest.py` | Data lineage, dedup, stats |
| Router | `providers/router.py` | Adapter-aware model selection |

### Implementation Status

| Layer | Status | Notes |
|---|---|---|
| Data capture | **Complete** | All LLM calls logged automatically |
| Review/export | **Complete** | CLI scripts with domain filtering |
| QVAC format converter | **Complete** | Quality filters, dedup, manifest tracking |
| Training orchestrator | **Complete** | Queues commands, never auto-executes |
| Adapter management | **Complete** | Registry + active map for router |
| Adapter-aware router | **Complete** | Passive when no adapters exist |
| QVAC binary install | **Not yet** | Requires `git clone` + cmake build with Vulkan |
| BitNet base models | **Not yet** | Download from HuggingFace |
| First training run | **Not yet** | Need 100+ approved samples per domain |

---

## Artifact Lifecycle

### Current Implementation (Simple Hot/Cold)
- `artifact_manager.py` archives files >7 days from hot → cold (.zip), purges cold >30 days
- Division configs define `max_hot_mb` budgets (not yet enforced at runtime)
- `atomic_write.py` provides crash-safe JSON persistence across all state files

### Not Yet Implemented (Full Hydration)
The original hydration concept — selective extraction from cold archives with manifests, indexing, TTL-based cache eviction, and per-task byte budgets — was designed but deliberately deferred. Tyler's simple hot/cold model handles current scale (~55 agents). The hydration system should be built when:
- Cold storage exceeds 1,000 archives
- Skills need to reference historical data across divisions
- Training data management requires structured artifact discovery

The `training_manifest.py` module implements hydration-inspired lineage tracking specifically for the QVAC training pipeline (tracking which samples have been captured, reviewed, approved, and trained).

---

## Gamification

- **XP** earned per skill run, per division
- **5-tier ranks** per division
- **15 quests** with progress tracking (First Hunt, Triple Threat, Forge Master, etc.)
- **17 achievements** including streak milestones, prestige marks, and production firsts
- **Streaks** with weekly shields
- **Prestige**: all divisions at Rank 5 → +5% permanent XP multiplier (stackable)
- **Token-aware context trimming** in all 3 chat handlers

---

## Stack

| Layer | Tech |
|---|---|
| Mission Control | Node.js 20, PM2 |
| Skills | Python 3.13 (~55 agents) |
| Local LLM | Ollama (Qwen2.5 7B / Coder 14B, AMD ROCm/Vulkan) |
| Cloud LLM | Groq 70B, DeepSeek, Gemini (escalation only) |
| Image/Video | ComfyUI + AnimateDiff-Evolved |
| Music | HuggingFace MusicGen + torch-directml |
| Voice | Coqui XTTS v2 |
| Training | QVAC Fabric (BitNet LoRA, Vulkan) |
| Mobile network | Tailscale |
| Notifications | VAPID push, Discord (Zenith bot) |
| State safety | atomic_write.py (tempfile + os.replace) |
| Cross-division | breach_check, escalation dedup, model_lock, burnout wires |

---

## Running

```bash
# Start everything
pm2 start ecosystem.config.js

# Check status
pm2 status

# View logs
pm2 logs server
pm2 logs openclaw-gateway

# Run a skill manually
python run_division.py trading market-scan
python run_division.py production art-director general vael
python run_division.py production model-trainer trading bitnet-1b status
python run_division.py production adapter-manager status
```

The PC dashboard is at `http://localhost:3000`.
Mobile access via Tailscale: `http://<tailscale-ip>:3000/mobile`.

---

## Security

- CORS restricted to localhost and Tailscale CGNAT range
- Mobile: server-side PIN (timing-safe) + optional WebAuthn biometric
- Windows Firewall scoped to local subnet + Tailscale
- Health and credential data never sent to cloud providers
- Training data reviewed by human before any fine-tuning
- LoRA training is always human-initiated (never auto-executed by cron)
- Sensitive state files gitignored
- Breach detection gates all non-critical operations

---

## Prop Firm Trading Engine

The trading division is built for futures prop firm evaluation and funded account management.

### Signal Resolution (3-tier priority)
1. **Schema-driven** — Uses exact backtested parameters from strategy schema (9 indicator types)
2. **Name-parsing** — Legacy fallback matching strategy name strings
3. **Multi-factor composite** — 6-score weighted system (trend/momentum/volatility/volume/structure/intermarket)

### Risk Controls
| Control | Threshold | Action |
|---|---|---|
| Trailing drawdown | 5% from peak equity | Force-liquidate ALL positions, halt permanently |
| Daily loss | -3% | Force-close ALL positions |
| VIX halt | > 35 | Block all new entries |
| VIX reduce | > 25 | 50% position size |
| Loss streak | 5 consecutive | 30-min cooldown, then half size |
| Contract limits | 5/instrument, 10 total | Clamp or skip entry |
| Correlation | > 0.80 | Block correlated entries |
| Per-instrument slippage | SPX=3, Gold=8, Crude=5, Bonds=3 bps | Applied to every fill |

### Daytrading Support
- **4x daily execution**: 03:00 (Pre-London), 10:00 (NY Open), 15:00 (NY Afternoon), 18:00 (NY Close)
- **Multi-timeframe**: Primary timeframe for direction + entry timeframe for timing
- **Time-of-day filters**: ny_rth, ny_extended, london, asia sessions + allowed/blocked hours
- **Session-aware data**: RTH-only bar filtering for cleaner analysis

### Intermarket Signals
- 3 confirmation types: `intermarket_trend`, `intermarket_momentum`, `intermarket_divergence`
- 15% weight in composite scoring
- Uses cross-instrument correlations for genuine multi-asset alpha

### Instruments (diversified, all holdable simultaneously)
| Instrument | Ticker | Futures | Slippage |
|---|---|---|---|
| SPX500 | ^GSPC | MES | 3 bps |
| XAUUSD | GC=F | MGC | 8 bps |
| CRUDE | CL=F | MCL | 5 bps |
| BONDS | ZN=F | MBT | 3 bps |

---

## What's Next

### Your Side (Setup Required)
1. Install Ollama + pull models (`qwen2.5:7b-instruct-q4_K_M`, `qwen2.5:14b-instruct-q4_K_M`)
2. Create `.env` file (ADZUNA_APP_ID, ADZUNA_APP_KEY, HIBP_API_KEY, TELEGRAM_BOT_TOKEN)
3. Install Python deps (`pip install yfinance pandas`)
4. Start system (`pm2 start ecosystem.config.js`)
5. Install ComfyUI + AnimateDiff for media generation
6. Install QVAC for model fine-tuning (when ready)

### Code Improvements (Future Sessions)
- **Multiple-testing correction** — Deflated Sharpe Ratio for 250-strategy search
- **Evaluation mode vs funded mode** — Different VIX thresholds, profit target tracking
- **Weekend/news event protection** — FOMC/NFP calendar, forced close before weekends
- **Production auto-trigger** — Art director → narrative craft → prompt craft → generation chain
- **Dashboard: trading metrics** — Surface trailing DD, contract usage, intermarket scores, session status
- **Dashboard: widget integration** — 7 prepared widget patches not yet integrated into dashboard
- **Voice reference recordings** — 5-30s WAV per commander for XTTS v2 cloning
- **Streak XP multiplier** — +10% per 7-day milestone, stacks to +50%
- **Agent-network expansion** — Live P&L streaming integration

---

*Z_Claw is a personal system. It is not a product, a framework, or a template. It is an ongoing experiment in what a single developer can automate when given enough stubbornness and a decent GPU.*
