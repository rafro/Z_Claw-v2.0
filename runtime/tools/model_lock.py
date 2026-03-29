"""Model lock — prevents concurrent Ollama model loads, pure Python."""
import json
import logging
import os
import time
from pathlib import Path

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)

LOCK_FILE = STATE_DIR / "model-lock.json"
LOCK_TTL_SECONDS = 120  # auto-expire stale locks after 2 minutes


def _read_lock() -> dict | None:
    try:
        if not LOCK_FILE.exists():
            return None
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        # Check TTL
        if time.time() - data.get("acquired_at", 0) > LOCK_TTL_SECONDS:
            log.info("Stale model lock expired (pid=%s)", data.get("pid"))
            _release_lock()
            return None
        return data
    except Exception:
        return None


def _write_lock(model: str, holder: str) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({
        "model": model,
        "holder": holder,
        "pid": os.getpid(),
        "acquired_at": time.time(),
    }, indent=2))


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def acquire(model: str, holder: str = "", timeout: float = 60.0) -> bool:
    """
    Try to acquire the model lock. Returns True on success.
    Blocks up to `timeout` seconds waiting for an existing lock to clear.
    """
    deadline = time.time() + timeout
    while True:
        existing = _read_lock()
        if existing is None:
            _write_lock(model, holder)
            log.debug("Model lock acquired: %s (holder=%s)", model, holder)
            return True
        if time.time() >= deadline:
            log.warning(
                "Model lock timeout — held by %s (pid=%s) for model %s",
                existing.get("holder"), existing.get("pid"), existing.get("model"),
            )
            return False
        time.sleep(0.5)


def release() -> None:
    """Release the model lock."""
    _release_lock()
    log.debug("Model lock released")


def is_locked() -> bool:
    """Check if the model lock is currently held."""
    return _read_lock() is not None


class ModelLock:
    """Context manager for model lock."""

    def __init__(self, model: str, holder: str = "", timeout: float = 60.0):
        self.model = model
        self.holder = holder
        self.timeout = timeout
        self.acquired = False

    def __enter__(self):
        self.acquired = acquire(self.model, self.holder, self.timeout)
        return self

    def __exit__(self, *exc):
        if self.acquired:
            release()
        return False
