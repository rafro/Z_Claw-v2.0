# QVAC BitNet LoRA Fine-Tuning Pipeline

Setup guide for training domain-specific LoRA adapters on Tyler's AMD RX 9070 XT using QVAC Fabric and BitNet b1.58 quantized models. No NVIDIA hardware required.

---

## Overview

J_Claw is designed to replace cloud API calls with locally fine-tuned models over time. Every LLM call made by any division is silently captured via `CaptureProvider` to `state/training-capture.jsonl`. Humans review and approve that data. QVAC trains LoRA adapters from it. The agents get smarter.

```
Agents run daily ──► CaptureProvider logs interactions
                            │
                            ▼
              Humans review + approve captures
                            │
                            ▼
             QVAC trains domain-specific LoRA adapter
                            │
                            ▼
          ProviderRouter loads adapter ──► agents get smarter
```

**Why BitNet + QVAC:**
- BitNet b1.58 uses ternary weights ({-1, 0, 1}), drastically reducing VRAM
- 13B parameters fine-tuned in ~3GB VRAM
- QVAC Fabric runs on Vulkan compute shaders — works on AMD, Intel, and Apple Silicon
- Tyler's RX 9070 XT (16GB VRAM, RDNA 4) runs this comfortably
- Zero cloud dependency for training or inference

---

## Hardware Requirements

| Component | Minimum | Recommended |
|---|---|---|
| GPU | Any Vulkan-capable GPU | AMD RX 9070 XT (16GB VRAM) |
| VRAM | 1GB (1B model) | 3GB (13B model) |
| Disk | ~5GB for QVAC + base models | ~10GB with multiple adapters |
| OS | Windows 11, Linux, macOS | Windows 11 (Tyler's setup) |

Supported GPU vendors: AMD (RDNA 2+), Intel (Arc), Apple Silicon (M1+). NVIDIA works too but is not required.

---

## Installation Steps

All paths assume Tyler's Windows 11 machine.

```bash
# Clone QVAC Fabric
cd C:/Users/Tyler
git clone https://github.com/tetherto/qvac-fabric-llm.cpp
cd qvac-fabric-llm.cpp

# Build for AMD/Vulkan
cmake -B build -DGGML_VULKAN=ON
cmake --build build --config Release
```

### Download BitNet Base Models

| Model | Size | VRAM | Link |
|---|---|---|---|
| BitNet 1B (TQ2_0) | ~0.4GB | ~1GB | [huggingface.co/microsoft/bitnet-b1.58-1B-GGUF](https://huggingface.co/microsoft/bitnet-b1.58-1B-GGUF) |
| BitNet 7B (TQ2_0) | ~2.5GB | ~2GB | [huggingface.co/microsoft/bitnet-b1.58-7B-GGUF](https://huggingface.co/microsoft/bitnet-b1.58-7B-GGUF) |
| BitNet 13B (TQ2_0) | ~4.3GB | ~3GB | [huggingface.co/microsoft/bitnet-b1.58-13B-GGUF](https://huggingface.co/microsoft/bitnet-b1.58-13B-GGUF) |

Place downloaded `.gguf` files in `C:/Users/Tyler/qvac-fabric-llm.cpp/models/`.

### Set Environment Variable

```bash
# Windows (cmd)
set QVAC_PATH=C:/Users/Tyler/qvac-fabric-llm.cpp

# Windows (PowerShell)
$env:QVAC_PATH = "C:/Users/Tyler/qvac-fabric-llm.cpp"

# Or set permanently via System Properties > Environment Variables
```

---

## Z_Claw Training Pipeline

Training is a 7-step manual process. Nothing auto-triggers.

```
Step 1: Accumulate    Agents run daily, CaptureProvider logs to training-capture.jsonl
Step 2: Review        python scripts/review_captures.py
Step 3: Export        python scripts/export_training_data.py --domain trading
Step 4: Format        python scripts/format_for_qvac.py --domain trading
Step 5: Check status  python run_division.py production model-trainer trading status
Step 6: Train         Run command from training-queue.json manually
Step 7: Activate      python run_division.py production adapter-manager activate trading
```

### Step 1 — Accumulate

Automatic. Every LLM call J_Claw makes is logged by `CaptureProvider` into `state/training-capture.jsonl`. Each entry includes the prompt, response, domain, provider used, and timestamp.

### Step 2 — Review Captures

```bash
python scripts/review_captures.py
```

Opens an interactive review session. Mark each capture as `approved`, `rejected`, or `skip`. Only approved captures proceed to training. This is the quality gate.

### Step 3 — Export by Domain

```bash
python scripts/export_training_data.py --domain trading
```

Filters approved captures for a specific domain and writes them to a training-ready format. Repeat for each domain you want to train.

### Step 4 — Format for QVAC

```bash
python scripts/format_for_qvac.py --domain trading
```

Converts exported data into the GGUF-compatible format QVAC expects. Outputs to the QVAC models directory.

### Step 5 — Check Readiness

```bash
python run_division.py production model-trainer trading status
```

Reports: number of approved samples, estimated training time, and whether the minimum sample threshold (100) is met.

### Step 6 — Train

The model-trainer skill writes training commands to `state/training-queue.json`. Run the command manually:

```bash
# Example (actual command comes from training-queue.json)
%QVAC_PATH%/build/bin/llama-finetune ^
  --model models/bitnet-13b-tq2_0.gguf ^
  --lora-out adapters/trading-lora.gguf ^
  --train-data training/trading.jsonl ^
  --lora-r 8 --lora-alpha 16 ^
  --ctx 512 --batch 512 ^
  --epochs 3 --lr 2e-4 ^
  --vulkan
```

### Step 7 — Activate Adapter

```bash
python run_division.py production adapter-manager activate trading
```

The `ProviderRouter` picks up the new adapter. Trading-domain calls now route through the fine-tuned model first, with Ollama and cloud providers as fallback.

---

## Training Parameters

Default configuration lives in `state/training-config.json`.

| Parameter | Default | Notes |
|---|---|---|
| LoRA rank (`lora-r`) | 8 | Higher = more expressive, more VRAM |
| LoRA alpha (`lora-alpha`) | 16 | Scaling factor, typically 2x rank |
| Sequence length (`ctx`) | 512 | Max context per training sample |
| Batch size (`batch`) | 512 | Tokens per batch |
| Epochs | 3 | Full passes over training data |
| Learning rate (`lr`) | 2e-4 | Standard LoRA fine-tune rate |
| Minimum samples | 100 | Per domain, enforced by model-trainer |

These defaults work well for the 13B model on the 9070 XT. Adjust `batch` down if VRAM is tight.

---

## Domain-Specific Training Notes

Each domain captures different interaction patterns and benefits from different data characteristics.

| Domain | What It Learns | Example Tasks |
|---|---|---|
| **Trading** | Market analysis, signal interpretation, report generation | SEREN's market scans, trading reports, backtester analysis |
| **Coding** | Code review, refactoring suggestions, debug analysis | KAELEN's refactor scans, security scans, code audits |
| **Chat** | J_Claw personality, commander dialogue, narrative voice | Commander interactions, realm layer dialogue, story choices |
| **OpSec** | Security analysis, threat assessment, privacy recommendations | ZETH's breach checks, threat surface scans, credential audits |
| **Personal** | Health insights, burnout detection, performance correlation | LYRIN's health logs, burnout monitoring, performance tracking |

Start with **Trading** — it has the highest call volume and the most structured output format, making it the easiest to validate.

---

## Adapter Management

Adapters are managed through the Production division's `adapter-manager` skill.

### View Status

```bash
python run_division.py production adapter-manager status
```

Shows all available adapters, which are active, training dates, and sample counts.

### Activate an Adapter

```bash
python run_division.py production adapter-manager activate trading
```

The ProviderRouter begins routing trading-domain calls through the LoRA adapter.

### Deactivate an Adapter

```bash
python run_division.py production adapter-manager deactivate trading
```

The ProviderRouter falls back to the base model (Ollama Qwen2.5 7B or cloud provider) for that domain.

### Rollback

If an adapter produces poor results:

1. Deactivate it: `python run_division.py production adapter-manager deactivate trading`
2. The ProviderRouter immediately falls back to the base model
3. No data is lost — the adapter file remains on disk for inspection
4. Review the training data quality and retrain if needed

---

## Safety Notes

Training and deployment follow a strict human-in-the-loop policy.

- **Training is always human-initiated.** No cron job, no automated trigger. A person runs the command.
- **Review before training.** Every capture must be explicitly approved via `review_captures.py`. Unreviewed data never enters the training pipeline.
- **Adapter activation is manual.** No adapter is auto-swapped into the router. A person decides when to activate.
- **Base model is always available.** Deactivating an adapter instantly restores the previous routing. There is no state to roll back — the base model never changes.
- **CaptureProvider is append-only.** Raw captures in `state/training-capture.jsonl` are never modified by the training pipeline. The original data is always preserved.

---

*QVAC BitNet LoRA fine-tuning is Phase 2 of J_Claw's self-improvement loop. Phase 1 (capture accumulation) is already running. This guide covers the full pipeline from captured data to deployed adapter.*
