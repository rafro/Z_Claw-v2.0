#!/usr/bin/env python3
"""
training_readiness.py

Reports per-domain QVAC training pipeline status: manifest stats,
formatted data availability, and readiness for first training run.

Usage:
    python scripts/training_readiness.py
    python scripts/training_readiness.py --domain trading
    python scripts/training_readiness.py --domain coding
"""

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path setup — same pattern as other scripts
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from runtime.tools.training_manifest import get_training_stats
from runtime.tools.domain_map import DOMAINS

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
QVAC_TRAINING_DIR = PROJECT_ROOT / "state" / "qvac-training"


def count_formatted_samples(domain: str) -> int:
    """Count non-empty lines in state/qvac-training/{domain}.jsonl."""
    data_path = QVAC_TRAINING_DIR / f"{domain}.jsonl"
    if not data_path.exists():
        return 0
    count = 0
    with data_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def report_domain(domain: str, stats: dict) -> str:
    """Build a single-line readiness report for one domain."""
    by_domain = stats.get("by_domain", {})
    domain_stats = by_domain.get(domain, {})

    captured = domain_stats.get("captured", 0)
    approved = domain_stats.get("approved", 0)
    trained = domain_stats.get("trained", 0)
    formatted = count_formatted_samples(domain)

    # Determine readiness
    if formatted == 0 and approved == 0:
        status = "NO DATA"
    elif formatted == 0 and approved > 0:
        status = "NEEDS FORMAT (run format_for_qvac.py)"
    elif formatted > 0 and trained == 0:
        status = "READY for first run"
    elif formatted > 0 and trained > 0 and approved > trained:
        status = f"READY for next run ({approved - trained} new approved)"
    elif formatted > 0 and trained > 0:
        status = "UP TO DATE"
    else:
        status = "READY for first run"

    parts = []
    if captured:
        parts.append(f"{captured} captured")
    parts.append(f"{approved} approved")
    parts.append(f"{trained} trained")
    if formatted:
        parts.append(f"{formatted} formatted")

    return f"  {domain.capitalize():<14}: {', '.join(parts)}. {status}."


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report per-domain QVAC training pipeline readiness.",
    )
    parser.add_argument(
        "--domain",
        choices=list(DOMAINS.keys()) + ["other"],
        default=None,
        metavar="DOMAIN",
        help="Show status for a specific domain only.",
    )
    args = parser.parse_args()

    stats = get_training_stats()

    print()
    print("=== QVAC Training Readiness ===")
    print(f"Last manifest update: {stats.get('last_updated', 'never')}")
    print(f"Total: {stats.get('total_captured', 0)} captured, "
          f"{stats.get('total_approved', 0)} approved, "
          f"{stats.get('total_trained', 0)} trained")
    print()

    domains_to_show = [args.domain] if args.domain else list(DOMAINS.keys()) + ["other"]

    for domain in domains_to_show:
        print(report_domain(domain, stats))

    print()


if __name__ == "__main__":
    main()
