"""
export_training_data.py

Reads state/training-approved.jsonl (reviewed captures) by default, prints
stats, and exports domain-split JSONL files ready for llama-finetune.
Use --source raw to read from state/training-capture.jsonl instead.

Usage:
    python scripts/export_training_data.py --stats
    python scripts/export_training_data.py --domain trading
    python scripts/export_training_data.py --source raw --stats
    python scripts/export_training_data.py --min-response-len 100 --output-dir state/my-exports
    python scripts/export_training_data.py  # export all domains
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root and default paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_FILE = PROJECT_ROOT / "state" / "training-capture.jsonl"
APPROVED_FILE = PROJECT_ROOT / "state" / "training-approved.jsonl"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "state" / "training-exports"

# ---------------------------------------------------------------------------
# Domain mapping
# ---------------------------------------------------------------------------
DOMAINS = {
    "trading":  ["market-scan", "trading-report"],
    "coding":   ["repo-monitor", "debug-agent", "refactor-scan", "doc-update",
                 "dev-generate", "dev-review", "dev-digest", "dev-summarize",
                 "dev-finalize", "dev-test"],
    "chat":     ["chat-operator"],
    "opsec":    ["threat-surface", "cred-audit", "privacy-scan", "security-scan",
                 "opsec-scan", "opsec-digest", "device-posture", "breach-check"],
    "personal": ["health-logger", "perf-correlation", "burnout-monitor", "personal-digest"],
    "other":    [],  # catch-all
}

# Build reverse lookup: task_type -> domain
TASK_TO_DOMAIN: dict[str, str] = {}
for domain, tasks in DOMAINS.items():
    for task in tasks:
        TASK_TO_DOMAIN[task] = domain


def get_domain(task_type: str) -> str:
    """Return the domain for a given task_type, falling back to 'other'."""
    return TASK_TO_DOMAIN.get(task_type, "other")


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_entries(source_file: Path, min_response_len: int = 0) -> list[dict]:
    """
    Load and parse all entries from the given source file.
    Returns a list of dicts. Lines that fail to parse are skipped with a warning.
    Entries whose response is shorter than min_response_len are also excluded.
    """
    if not source_file.exists():
        print(f"Source file not found: {source_file}")
        print("Nothing to do — run the system to generate training data first.")
        sys.exit(0)

    entries = []
    skipped_parse = 0
    skipped_len = 0

    with source_file.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"  [WARNING] Line {lineno}: failed to parse JSON — {exc}", file=sys.stderr)
                skipped_parse += 1
                continue

            response = entry.get("response", "") or ""
            if len(response) < min_response_len:
                skipped_len += 1
                continue

            entries.append(entry)

    if skipped_parse:
        print(f"  [WARNING] Skipped {skipped_parse} unparseable line(s).", file=sys.stderr)
    if skipped_len:
        print(f"  [INFO] Filtered out {skipped_len} entr(ies) with response length < {min_response_len}.",
              file=sys.stderr)

    return entries


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(entries: list[dict], output_dir: Path) -> None:
    total = len(entries)

    # Aggregate by task_type
    by_task: dict[str, int] = defaultdict(int)
    for e in entries:
        by_task[e.get("task_type", "unknown")] += 1

    # Aggregate by provider (shorten for display)
    by_provider: dict[str, int] = defaultdict(int)
    for e in entries:
        provider = e.get("provider_id", "unknown")
        by_provider[provider] += 1

    # Aggregate by domain
    by_domain: dict[str, int] = defaultdict(int)
    for e in entries:
        domain = get_domain(e.get("task_type", ""))
        by_domain[domain] += 1

    # Average latency
    latencies = [e["latency_ms"] for e in entries if isinstance(e.get("latency_ms"), (int, float))]
    avg_latency = (sum(latencies) / len(latencies)) if latencies else 0

    print()
    print("=== Training Capture Stats ===")
    print(f"Total entries: {total}")

    print("\nBy task_type:")
    for task, count in sorted(by_task.items(), key=lambda x: -x[1]):
        domain = get_domain(task)
        print(f"  {task:<24}: {count}  ({domain})")

    print("\nBy provider:")
    for provider, count in sorted(by_provider.items(), key=lambda x: -x[1]):
        print(f"  {provider:<32}: {count}")

    print(f"\nAvg latency_ms: {avg_latency:.0f}")

    print("\nBy domain:")
    for domain in list(DOMAINS.keys()):
        count = by_domain.get(domain, 0)
        print(f"  {domain:<10}: {count} entries")

    print(f"\nOutput files would be written to: {output_dir}")
    print()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def build_finetune_line(entry: dict) -> dict:
    """Convert a capture entry to llama-finetune message format."""
    messages = list(entry.get("messages", []))  # copy
    response = entry.get("response", "") or ""
    messages.append({"role": "assistant", "content": response})
    return {"messages": messages}


def export_domains(entries: list[dict], output_dir: Path, domain_filter: str | None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Group entries by domain
    grouped: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        domain = get_domain(entry.get("task_type", ""))
        grouped[domain].append(entry)

    domains_to_export = [domain_filter] if domain_filter else list(DOMAINS.keys())

    manifest_files: dict[str, dict] = {}

    for domain in domains_to_export:
        domain_entries = grouped.get(domain, [])
        filename = f"training-{domain}.jsonl"
        out_path = output_dir / filename

        with out_path.open("w", encoding="utf-8") as fh:
            for entry in domain_entries:
                line = build_finetune_line(entry)
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")

        manifest_files[domain] = {
            "path": filename,
            "count": len(domain_entries),
        }
        print(f"  Wrote {len(domain_entries):>5} entries -> {out_path}")

    # Write manifest
    manifest = {
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        "files": manifest_files,
    }
    manifest_path = output_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"\n  Manifest written -> {manifest_path}")
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export training data for llama-finetune (reads approved captures by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        choices=["approved", "raw"],
        default="approved",
        help="Read from approved captures (default) or raw captures.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print stats only; do not export any files.",
    )
    parser.add_argument(
        "--domain",
        choices=list(DOMAINS.keys()),
        default=None,
        metavar="DOMAIN",
        help="Export only this domain (trading/coding/chat/opsec/personal/other).",
    )
    parser.add_argument(
        "--min-response-len",
        type=int,
        default=50,
        metavar="N",
        help="Skip entries where response length < N (default: 50).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help=f"Directory for output files (default: {DEFAULT_OUTPUT_DIR}).",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Resolve output_dir relative to project root if not absolute
    output_dir: Path = args.output_dir
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir

    source_file = APPROVED_FILE if args.source == "approved" else CAPTURE_FILE
    entries = load_entries(source_file, min_response_len=args.min_response_len)

    if not entries:
        print("No entries loaded (capture file may be empty or all entries were filtered).")
        sys.exit(0)

    if args.stats:
        print_stats(entries, output_dir)
        return

    # Export mode
    print(f"\nExporting {'domain: ' + args.domain if args.domain else 'all domains'}...")
    print(f"Output dir: {output_dir}\n")
    export_domains(entries, output_dir, domain_filter=args.domain)


if __name__ == "__main__":
    main()
