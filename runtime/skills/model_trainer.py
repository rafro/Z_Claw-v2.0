"""
model_trainer skill — orchestrates QVAC BitNet LoRA fine-tuning runs.
Tier 0: pure Python, no LLM calls. Manages training data validation,
adapter status tracking, and command generation for manual execution.

Training is NEVER auto-executed — commands are queued for human review.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
TRAINING_DATA_DIR = STATE_DIR / "qvac-training"
ADAPTERS_DIR = STATE_DIR / "adapters"
TRAINING_CONFIG_PATH = STATE_DIR / "training-config.json"
TRAINING_QUEUE_PATH = STATE_DIR / "training-queue.json"
TRAINING_LOCK_PATH = STATE_DIR / "training.lock"

# ── Default QVAC path (Tyler's Windows box) ─────────────────────────────────
DEFAULT_QVAC_PATH = "C:/Users/Tyler/qvac-fabric-llm.cpp"

# ── Default training config ─────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "qvac_path": "auto",
    "default_base_model": "bitnet-1b-tq1_0",
    "lora_rank": 8,
    "lora_alpha": 16,
    "sequence_length": 512,
    "batch_size": 512,
    "min_samples": 100,
    "max_epochs": 3,
    "learning_rate": 2e-4,
}

# ── Base model registry (fits on RX 9070 XT 16GB) ───────────────────────────
MODEL_REGISTRY = {
    "bitnet-1b": {
        "path_suffix": "models/bitnet-1b-tq1_0.gguf",
        "params": "1B",
        "vram_gb": 2,
        "est_minutes_per_epoch_per_1k": 5,
    },
    "bitnet-3b": {
        "path_suffix": "models/bitnet-3b-tq1_0.gguf",
        "params": "3B",
        "vram_gb": 4,
        "est_minutes_per_epoch_per_1k": 12,
    },
    "bitnet-7b": {
        "path_suffix": "models/bitnet-7b-tq1_0.gguf",
        "params": "7B",
        "vram_gb": 8,
        "est_minutes_per_epoch_per_1k": 25,
    },
    "bitnet-13b": {
        "path_suffix": "models/bitnet-13b-tq1_0.gguf",
        "params": "13B",
        "vram_gb": 15,
        "est_minutes_per_epoch_per_1k": 55,
    },
}


def _load_config() -> dict:
    """Load training config from state, falling back to defaults."""
    if TRAINING_CONFIG_PATH.exists():
        try:
            with open(TRAINING_CONFIG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            # Merge with defaults so new keys are always present
            merged = {**DEFAULT_CONFIG, **saved}
            return merged
        except Exception as e:
            log.warning("Failed to load training config, using defaults: %s", e)
    return dict(DEFAULT_CONFIG)


def _save_config(config: dict) -> None:
    """Persist training config to state."""
    TRAINING_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TRAINING_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def _resolve_qvac_path(config: dict) -> str:
    """Resolve QVAC installation path from env, config, or default."""
    env_path = os.environ.get("QVAC_PATH")
    if env_path:
        return env_path
    cfg_path = config.get("qvac_path", "auto")
    if cfg_path and cfg_path != "auto":
        return cfg_path
    return DEFAULT_QVAC_PATH


def _qvac_installed(qvac_path: str) -> bool:
    """Check if the QVAC finetune binary exists."""
    finetune_bin = Path(qvac_path) / "build" / "bin" / "finetune"
    # Check both with and without .exe for cross-platform
    return finetune_bin.exists() or finetune_bin.with_suffix(".exe").exists()


def _training_data_path(domain: str) -> Path:
    """Return path to training data JSONL for a domain."""
    return TRAINING_DATA_DIR / f"{domain}.jsonl"


def _adapter_dir(domain: str) -> Path:
    """Return path to adapter output directory."""
    return ADAPTERS_DIR / domain


def _count_samples(data_path: Path) -> int:
    """Count non-empty lines in a JSONL file."""
    if not data_path.exists():
        return 0
    count = 0
    with open(data_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def _validate_jsonl_entries(data_path: Path, n: int = 5) -> list[str]:
    """Parse the first N entries and return a list of issues found."""
    issues = []
    if not data_path.exists():
        issues.append(f"Training data file not found: {data_path}")
        return issues

    entries_checked = 0
    with open(data_path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if entries_checked >= n:
                break
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                issues.append(f"Line {i + 1}: invalid JSON — {e}")
                entries_checked += 1
                continue

            # Validate expected QVAC training format
            if not isinstance(entry, dict):
                issues.append(f"Line {i + 1}: entry is not a JSON object")
            elif "text" not in entry and "instruction" not in entry:
                issues.append(
                    f"Line {i + 1}: missing 'text' or 'instruction' field "
                    f"(found keys: {list(entry.keys())})"
                )
            entries_checked += 1

    if entries_checked == 0:
        issues.append("Training data file is empty")

    return issues


def _estimate_duration(sample_count: int, base_model: str, epochs: int) -> float:
    """Estimate training duration in minutes."""
    model_info = MODEL_REGISTRY.get(base_model, MODEL_REGISTRY["bitnet-1b"])
    rate = model_info["est_minutes_per_epoch_per_1k"]
    thousands = max(sample_count / 1000, 0.1)
    return round(rate * thousands * epochs, 1)


def _load_queue() -> dict:
    """Load the training queue file."""
    if TRAINING_QUEUE_PATH.exists():
        try:
            with open(TRAINING_QUEUE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"queued_runs": [], "history": []}
    return {"queued_runs": [], "history": []}


def _save_queue(queue: dict) -> None:
    """Persist the training queue."""
    TRAINING_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TRAINING_QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)


def _is_training_locked() -> bool:
    """Check if a training run is currently in progress."""
    return TRAINING_LOCK_PATH.exists()


def _get_last_trained(domain: str) -> str | None:
    """Get the timestamp of the last completed training run for a domain."""
    queue = _load_queue()
    for entry in reversed(queue.get("history", [])):
        if entry.get("domain") == domain and entry.get("completed_at"):
            return entry["completed_at"]
    return None


# ── Actions ──────────────────────────────────────────────────────────────────

def _action_status(domain: str, base_model: str, config: dict) -> dict:
    """Check training data, adapter, and QVAC installation status."""
    data_path = _training_data_path(domain)
    adapter_dir = _adapter_dir(domain)
    qvac_path = _resolve_qvac_path(config)
    sample_count = _count_samples(data_path)
    adapter_exists = adapter_dir.exists() and any(adapter_dir.iterdir()) if adapter_dir.exists() else False
    qvac_installed = _qvac_installed(qvac_path)
    last_trained = _get_last_trained(domain)
    min_samples = config.get("min_samples", DEFAULT_CONFIG["min_samples"])

    ready_to_train = (
        sample_count >= min_samples
        and qvac_installed
        and not _is_training_locked()
    )

    summary_parts = []
    summary_parts.append(f"{domain}: {sample_count} samples")
    if adapter_exists:
        summary_parts.append("adapter exists")
    if qvac_installed:
        summary_parts.append("QVAC installed")
    else:
        summary_parts.append("QVAC not found")
    if ready_to_train:
        summary_parts.append("READY to train")
    elif sample_count < min_samples:
        summary_parts.append(f"need {min_samples - sample_count} more samples")

    return {
        "status": "success",
        "action": "status",
        "summary": " | ".join(summary_parts),
        "domain": domain,
        "base_model": base_model,
        "ready_to_train": ready_to_train,
        "samples_available": sample_count,
        "min_samples": min_samples,
        "adapter_exists": adapter_exists,
        "adapter_path": str(adapter_dir) if adapter_exists else None,
        "qvac_installed": qvac_installed,
        "qvac_path": qvac_path,
        "last_trained": last_trained,
        "training_locked": _is_training_locked(),
        "escalate": False,
        "escalation_reason": "",
    }


def _action_prepare(domain: str, base_model: str, config: dict) -> dict:
    """Validate training data and readiness for a training run."""
    data_path = _training_data_path(domain)
    min_samples = config.get("min_samples", DEFAULT_CONFIG["min_samples"])
    epochs = config.get("max_epochs", DEFAULT_CONFIG["max_epochs"])
    sample_count = _count_samples(data_path)

    issues = []

    # Check lock
    if _is_training_locked():
        issues.append("Training is currently in progress (lock file exists)")

    # Check sample count
    if sample_count < min_samples:
        issues.append(
            f"Insufficient samples: {sample_count}/{min_samples} "
            f"(need {min_samples - sample_count} more)"
        )

    # Validate data format
    if data_path.exists():
        format_issues = _validate_jsonl_entries(data_path, n=5)
        issues.extend(format_issues)
    else:
        issues.append(f"Training data not found: {data_path}")

    # Check model registry
    if base_model not in MODEL_REGISTRY:
        issues.append(
            f"Unknown base model '{base_model}' — "
            f"available: {', '.join(MODEL_REGISTRY.keys())}"
        )

    valid = len(issues) == 0
    est_duration = _estimate_duration(sample_count, base_model, epochs) if sample_count > 0 else 0

    summary = (
        f"{domain}: {'VALID' if valid else 'NOT READY'} — "
        f"{sample_count} samples, ~{est_duration} min estimated"
    )
    if issues:
        summary += f" | {len(issues)} issue(s)"

    return {
        "status": "success",
        "action": "prepare",
        "summary": summary,
        "domain": domain,
        "base_model": base_model,
        "valid": valid,
        "sample_count": sample_count,
        "min_samples": min_samples,
        "estimated_duration_minutes": est_duration,
        "issues": issues,
        "escalate": False,
        "escalation_reason": "",
    }


def _action_train(domain: str, base_model: str, config: dict) -> dict:
    """Build a QVAC training command and queue it for manual execution."""
    qvac_path = _resolve_qvac_path(config)
    data_path = _training_data_path(domain)
    adapter_dir = _adapter_dir(domain)
    epochs = config.get("max_epochs", DEFAULT_CONFIG["max_epochs"])
    lr = config.get("learning_rate", DEFAULT_CONFIG["learning_rate"])
    lora_rank = config.get("lora_rank", DEFAULT_CONFIG["lora_rank"])
    lora_alpha = config.get("lora_alpha", DEFAULT_CONFIG["lora_alpha"])
    seq_len = config.get("sequence_length", DEFAULT_CONFIG["sequence_length"])
    batch_size = config.get("batch_size", DEFAULT_CONFIG["batch_size"])
    sample_count = _count_samples(data_path)
    qvac_installed = _qvac_installed(qvac_path)

    # Resolve model path
    model_info = MODEL_REGISTRY.get(base_model, MODEL_REGISTRY["bitnet-1b"])
    model_path = str(Path(qvac_path) / model_info["path_suffix"])
    adapter_out = str(adapter_dir / "adapter.bin")

    # Build the finetune command
    finetune_bin = str(Path(qvac_path) / "build" / "bin" / "finetune")
    command = (
        f"{finetune_bin} \\\n"
        f"  --model {model_path} \\\n"
        f"  --data {data_path} \\\n"
        f"  --adapter-out {adapter_out} \\\n"
        f"  --lora-rank {lora_rank} --lora-alpha {lora_alpha} \\\n"
        f"  --seq-len {seq_len} --batch-size {batch_size} \\\n"
        f"  --epochs {epochs} --lr {lr}"
    )

    est_duration = _estimate_duration(sample_count, base_model, epochs)

    # Queue the run
    queue = _load_queue()
    run_entry = {
        "domain": domain,
        "base_model": base_model,
        "command": command,
        "samples": sample_count,
        "estimated_duration_minutes": est_duration,
        "adapter_out": adapter_out,
        "qvac_installed": qvac_installed,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "status": "queued",
    }
    queue["queued_runs"].append(run_entry)
    _save_queue(queue)

    # Ensure adapter output directory exists
    adapter_dir.mkdir(parents=True, exist_ok=True)

    if qvac_installed:
        summary = (
            f"Training command queued for {domain} ({base_model}, "
            f"{sample_count} samples, ~{est_duration} min). "
            f"QVAC installed — ready for manual execution."
        )
    else:
        summary = (
            f"Training command queued for {domain} ({base_model}, "
            f"{sample_count} samples, ~{est_duration} min). "
            f"QVAC NOT installed at {qvac_path} — install before running."
        )

    log.info(
        "Model trainer: queued %s run for domain=%s model=%s samples=%d est=%s min",
        "ready" if qvac_installed else "pending",
        domain, base_model, sample_count, est_duration,
    )

    return {
        "status": "success",
        "action": "train",
        "summary": summary,
        "train_status": "queued",
        "domain": domain,
        "base_model": base_model,
        "command": command,
        "samples": sample_count,
        "estimated_duration_minutes": est_duration,
        "adapter_out": adapter_out,
        "qvac_installed": qvac_installed,
        "qvac_path": qvac_path,
        "escalate": False,
        "escalation_reason": "",
    }


# ── Entry point ──────────────────────────────────────────────────────────────

def run(
    domain: str = "trading",
    base_model: str = "bitnet-1b",
    action: str = "status",
) -> dict:
    """
    Model training orchestration for QVAC BitNet LoRA fine-tuning.

    Args:
        domain:     Training domain (e.g. "trading", "opportunity").
                    Maps to state/qvac-training/{domain}.jsonl
        base_model: Base model key from MODEL_REGISTRY.
        action:     "status" | "prepare" | "train"

    Returns:
        Structured result dict matching the skill packet pattern.
    """
    config = _load_config()

    # Ensure default config is persisted on first run
    if not TRAINING_CONFIG_PATH.exists():
        _save_config(config)

    # Use config default model if none specified
    if base_model == "bitnet-1b" and config.get("default_base_model"):
        cfg_model = config["default_base_model"]
        # Map config model names to registry keys
        model_aliases = {
            "bitnet-1b-tq1_0": "bitnet-1b",
            "bitnet-3b-tq1_0": "bitnet-3b",
            "bitnet-7b-tq1_0": "bitnet-7b",
            "bitnet-13b-tq1_0": "bitnet-13b",
        }
        base_model = model_aliases.get(cfg_model, base_model)

    try:
        if action == "status":
            return _action_status(domain, base_model, config)
        elif action == "prepare":
            return _action_prepare(domain, base_model, config)
        elif action == "train":
            return _action_train(domain, base_model, config)
        else:
            return {
                "status": "failed",
                "action": action,
                "summary": f"Unknown action: {action} (expected: status, prepare, train)",
                "escalate": True,
                "escalation_reason": f"Unknown model-trainer action: {action}",
            }
    except Exception as e:
        log.error("Model trainer failed: action=%s domain=%s — %s", action, domain, e)
        return {
            "status": "failed",
            "action": action,
            "summary": f"Model trainer error: {e}",
            "domain": domain,
            "base_model": base_model,
            "escalate": True,
            "escalation_reason": f"Model trainer error: {e}",
        }
