"""
Adapter Manager — tracks and manages trained LoRA adapters.

Tier 0 skill (pure Python, no LLM).  QVAC training produces LoRA adapters
saved to ``state/adapters/{domain}/``.  This skill provides a registry that
the ProviderRouter can consult to know which adapter is active for each domain.

Registry files:
  state/adapter-registry.json  — full metadata + history
  state/active-adapters.json   — lightweight {domain: adapter_path} map for router
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)

ADAPTERS_DIR    = STATE_DIR / "adapters"
REGISTRY_FILE   = STATE_DIR / "adapter-registry.json"
ACTIVE_MAP_FILE = STATE_DIR / "active-adapters.json"


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def _load_registry() -> dict:
    if REGISTRY_FILE.exists():
        try:
            with open(REGISTRY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.warning("adapter_manager: corrupt registry — resetting")
    return {"adapters": {}, "history": []}


def _save_registry(registry: dict) -> None:
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)


def _load_active_map() -> dict:
    if ACTIVE_MAP_FILE.exists():
        try:
            with open(ACTIVE_MAP_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_active_map(active_map: dict) -> None:
    ACTIVE_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ACTIVE_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(active_map, f, indent=2)


def _sync_active_map(registry: dict) -> dict:
    """Rebuild the active-adapters map from the registry and persist it."""
    active_map = {}
    for domain, entry in registry.get("adapters", {}).items():
        if entry.get("active"):
            active_map[domain] = entry.get("adapter_path", "")
    _save_active_map(active_map)
    return active_map


# ---------------------------------------------------------------------------
# Filesystem scanning
# ---------------------------------------------------------------------------

def _scan_domain(domain_dir: Path) -> dict:
    """Return metadata for a single adapter domain directory."""
    info: dict = {
        "domain": domain_dir.name,
        "path": str(domain_dir),
        "files": [],
        "total_size_bytes": 0,
    }
    if not domain_dir.is_dir():
        return info
    for f in sorted(domain_dir.iterdir()):
        if f.is_file():
            stat = f.stat()
            info["files"].append({
                "name": f.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
            info["total_size_bytes"] += stat.st_size
    return info


def _scan_all_domains() -> list[dict]:
    """Walk state/adapters/ and return a list of per-domain scan results."""
    results: list[dict] = []
    if not ADAPTERS_DIR.is_dir():
        return results
    for child in sorted(ADAPTERS_DIR.iterdir()):
        if child.is_dir():
            results.append(_scan_domain(child))
    return results


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_status(domain: Optional[str] = None) -> dict:
    """Inventory all adapters (or one domain) — merge filesystem + registry."""
    registry = _load_registry()
    scanned = _scan_all_domains()
    active_map = _load_active_map()

    domain_statuses: list[dict] = []
    for scan in scanned:
        d = scan["domain"]
        if domain and d != domain:
            continue
        reg_entry = registry.get("adapters", {}).get(d, {})
        domain_statuses.append({
            "domain":          d,
            "active":          reg_entry.get("active", False),
            "version":         reg_entry.get("version", 0),
            "base_model":      reg_entry.get("base_model", "unknown"),
            "adapter_path":    reg_entry.get("adapter_path", ""),
            "trained_at":      reg_entry.get("trained_at", ""),
            "samples_used":    reg_entry.get("samples_used", 0),
            "performance_notes": reg_entry.get("performance_notes", ""),
            "files":           scan["files"],
            "total_size_bytes": scan["total_size_bytes"],
        })

    total = len(domain_statuses)
    active_count = sum(1 for ds in domain_statuses if ds["active"])

    return {
        "status": "success",
        "summary": (
            f"Adapter inventory: {total} domain(s) found, {active_count} active."
            + (f" Filtered to domain='{domain}'." if domain else "")
        ),
        "metrics": {
            "total_domains": total,
            "active_count": active_count,
            "active_map": active_map,
            "domains": domain_statuses,
        },
        "action_items": [],
        "escalate": False,
    }


def _action_activate(domain: str) -> dict:
    """Mark an adapter as active for a domain."""
    if not domain:
        return {
            "status": "failed",
            "summary": "activate requires a domain argument.",
            "metrics": {},
            "action_items": [],
            "escalate": False,
        }

    registry = _load_registry()
    entry = registry["adapters"].get(domain)

    # If no registry entry exists, try to bootstrap from filesystem
    if entry is None:
        domain_dir = ADAPTERS_DIR / domain
        if not domain_dir.is_dir():
            return {
                "status": "failed",
                "summary": f"No adapter directory found for domain '{domain}' at {domain_dir}.",
                "metrics": {"domain": domain},
                "action_items": [
                    {"priority": "medium", "description": f"Train a LoRA adapter for domain '{domain}' via QVAC.", "requires_matthew": False}
                ],
                "escalate": False,
            }
        # Bootstrap a minimal registry entry
        scan = _scan_domain(domain_dir)
        adapter_file = next(
            (fi["name"] for fi in scan["files"] if fi["name"].endswith((".bin", ".safetensors", ".pt", ".gguf"))),
            None,
        )
        if adapter_file is None:
            return {
                "status": "failed",
                "summary": f"Adapter directory exists for '{domain}' but no model file (.bin/.safetensors/.pt/.gguf) found.",
                "metrics": {"domain": domain, "files_found": [fi["name"] for fi in scan["files"]]},
                "action_items": [],
                "escalate": False,
            }
        entry = {
            "active": False,
            "adapter_path": str(ADAPTERS_DIR / domain / adapter_file),
            "base_model": "unknown",
            "trained_at": "",
            "samples_used": 0,
            "domain": domain,
            "version": 1,
            "performance_notes": "Auto-discovered from filesystem.",
        }
        registry["adapters"][domain] = entry

    now = datetime.now(timezone.utc).isoformat()
    entry["active"] = True
    registry["adapters"][domain] = entry
    _save_registry(registry)

    # Append history
    registry["history"].append({
        "domain": domain,
        "version": entry.get("version", 1),
        "trained_at": entry.get("trained_at", ""),
        "activated_at": now,
        "deactivated_at": None,
        "samples_used": entry.get("samples_used", 0),
    })
    _save_registry(registry)

    active_map = _sync_active_map(registry)
    log.info("adapter_manager: activated adapter for domain '%s' — %s", domain, entry.get("adapter_path"))

    return {
        "status": "success",
        "summary": f"Adapter activated for domain '{domain}' (v{entry.get('version', 1)}). Active adapters: {len(active_map)}.",
        "metrics": {
            "domain": domain,
            "adapter_path": entry.get("adapter_path", ""),
            "version": entry.get("version", 1),
            "active_map": active_map,
        },
        "action_items": [],
        "escalate": False,
    }


def _action_deactivate(domain: str) -> dict:
    """Remove active adapter for a domain — fall back to base model."""
    if not domain:
        return {
            "status": "failed",
            "summary": "deactivate requires a domain argument.",
            "metrics": {},
            "action_items": [],
            "escalate": False,
        }

    registry = _load_registry()
    entry = registry["adapters"].get(domain)

    if entry is None or not entry.get("active"):
        return {
            "status": "partial",
            "summary": f"No active adapter for domain '{domain}' — already using base model.",
            "metrics": {"domain": domain},
            "action_items": [],
            "escalate": False,
        }

    now = datetime.now(timezone.utc).isoformat()
    entry["active"] = False
    registry["adapters"][domain] = entry

    # Update the most recent history entry for this domain
    for hist in reversed(registry["history"]):
        if hist["domain"] == domain and hist["deactivated_at"] is None:
            hist["deactivated_at"] = now
            break

    _save_registry(registry)
    active_map = _sync_active_map(registry)
    log.info("adapter_manager: deactivated adapter for domain '%s' — falling back to base model", domain)

    return {
        "status": "success",
        "summary": f"Adapter deactivated for domain '{domain}'. Now using base model. Active adapters: {len(active_map)}.",
        "metrics": {
            "domain": domain,
            "active_map": active_map,
        },
        "action_items": [],
        "escalate": False,
    }


def _action_history(domain: Optional[str] = None) -> dict:
    """Show training/activation history for a domain (or all)."""
    registry = _load_registry()
    history = registry.get("history", [])

    if domain:
        history = [h for h in history if h["domain"] == domain]

    return {
        "status": "success",
        "summary": (
            f"Adapter history: {len(history)} event(s)"
            + (f" for domain '{domain}'." if domain else " across all domains.")
        ),
        "metrics": {
            "domain": domain,
            "event_count": len(history),
            "events": history,
        },
        "action_items": [],
        "escalate": False,
    }


# ---------------------------------------------------------------------------
# Skill entry point
# ---------------------------------------------------------------------------

def run(action: str = "status", domain: Optional[str] = None) -> dict:
    """
    Adapter Manager entry point.

    Actions:
      status     — inventory all adapters across all domains
      activate   — mark an adapter as active for a domain
      deactivate — remove active adapter for a domain (fall back to base model)
      history    — show training history for a domain

    Args:
        action:  One of "status", "activate", "deactivate", "history".
        domain:  Domain name (required for activate/deactivate, optional for status/history).
    """
    action = (action or "status").lower().strip()
    domain = (domain or "").strip() or None

    if action == "status":
        return _action_status(domain)
    if action == "activate":
        return _action_activate(domain or "")
    if action == "deactivate":
        return _action_deactivate(domain or "")
    if action == "history":
        return _action_history(domain)

    return {
        "status": "failed",
        "summary": f"Unknown action '{action}'. Valid actions: status, activate, deactivate, history.",
        "metrics": {},
        "action_items": [],
        "escalate": False,
    }
