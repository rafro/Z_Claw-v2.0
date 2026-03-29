#!/usr/bin/env python3
"""
format_for_qvac.py

Converts approved training captures into QVAC BitNet LoRA fine-tuning format.
Reads from state/training-approved.jsonl (or a domain-specific export via --input),
applies quality filters (min response length, max token estimate, deduplication),
and writes chat-template JSONL with a manifest.

Usage:
    python scripts/format_for_qvac.py --stats
    python scripts/format_for_qvac.py --domain coding
    python scripts/format_for_qvac.py --input state/training-exports/training-trading.jsonl --domain trading
    python scripts/format_for_qvac.py --max-tokens 256 --min-response-len 100
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root and default paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = PROJECT_ROOT / "state" / "training-approved.jsonl"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "state" / "qvac-training"

# ---------------------------------------------------------------------------
# Domain mapping (kept in sync with export_training_data.py / review_captures.py)
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

TASK_TO_DOMAIN: dict[str, str] = {}
for _domain, _tasks in DOMAINS.items():
    for _task in _tasks:
        TASK_TO_DOMAIN[_task] = _domain

# Characters-per-token estimate for QVAC sequence length budget
CHARS_PER_TOKEN = 4

# Default QVAC system prompt template
SYSTEM_PROMPT_TEMPLATE = "You are a specialized AI assistant for {domain}."


def get_domain(task_type: str) -> str:
    """Return the domain for a given task_type, falling back to 'other'."""
    return TASK_TO_DOMAIN.get(task_type, "other")


# ---------------------------------------------------------------------------
# Entry parsing
# ---------------------------------------------------------------------------

def extract_roles(entry: dict) -> tuple[str, str, str]:
    """
    Extract (system_content, user_content, assistant_content) from an entry.

    Handles two schemas:
      - Approved format: {"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}]}
      - Raw capture format: {"messages": [...], "response": "..."}
    """
    system_content = ""
    user_content = ""
    assistant_content = ""

    for msg in entry.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_content = content
        elif role == "user":
            user_content = content
        elif role == "assistant":
            assistant_content = content

    # Raw capture entries store the response separately
    if not assistant_content and entry.get("response"):
        assistant_content = entry["response"]

    return system_content, user_content, assistant_content


def infer_domain_from_entry(entry: dict) -> str:
    """
    Attempt to infer domain from entry metadata.
    Falls back to 'other' if task_type is absent (e.g. approved-format entries).
    """
    task_type = entry.get("task_type", "")
    if task_type:
        return get_domain(task_type)
    return "other"


def infer_domain_from_filename(filepath: Path) -> str | None:
    """
    Try to detect domain from filename patterns like training-trading.jsonl
    or trading.jsonl.
    """
    stem = filepath.stem  # e.g. "training-trading" or "trading"
    for domain in DOMAINS:
        if stem == domain or stem == f"training-{domain}":
            return domain
    return None


# ---------------------------------------------------------------------------
# Quality filters
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Estimate token count using chars-per-token heuristic."""
    return len(text) // CHARS_PER_TOKEN


def content_hash(user_content: str) -> str:
    """SHA-256 hash of user message for deduplication."""
    return hashlib.sha256(user_content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# QVAC format conversion
# ---------------------------------------------------------------------------

def to_qvac_entry(
    system_content: str,
    user_content: str,
    assistant_content: str,
    domain: str,
) -> dict:
    """Build a QVAC chat-template training entry."""
    # Use the original system prompt if present, otherwise generate one
    if not system_content.strip():
        system_content = SYSTEM_PROMPT_TEMPLATE.format(domain=domain)

    return {
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ]
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_entries(input_path: Path) -> list[dict]:
    """Load and parse all entries from a JSONL file."""
    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    entries = []
    skipped = 0
    with input_path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                print(f"  [WARNING] Line {lineno}: failed to parse JSON - {exc}", file=sys.stderr)
                skipped += 1

    if skipped:
        print(f"  [WARNING] Skipped {skipped} unparseable line(s).", file=sys.stderr)

    return entries


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def process_entries(
    entries: list[dict],
    domain: str,
    max_tokens: int,
    min_response_len: int,
) -> tuple[list[dict], dict]:
    """
    Apply quality filters and convert entries to QVAC format.

    Returns:
        (qvac_entries, filter_stats)
    """
    max_chars = max_tokens * CHARS_PER_TOKEN

    seen_hashes: set[str] = set()

    skipped_short = 0
    skipped_long = 0
    skipped_dup = 0
    accepted = 0

    results: list[dict] = []

    for entry in entries:
        system_content, user_content, assistant_content = extract_roles(entry)

        # Filter: assistant response too short
        if len(assistant_content) < min_response_len:
            skipped_short += 1
            continue

        # Filter: total sequence too long for QVAC
        total_chars = len(system_content) + len(user_content) + len(assistant_content)
        if total_chars > max_chars:
            skipped_long += 1
            continue

        # Filter: deduplicate by user message hash
        h = content_hash(user_content)
        if h in seen_hashes:
            skipped_dup += 1
            continue
        seen_hashes.add(h)

        qvac_entry = to_qvac_entry(system_content, user_content, assistant_content, domain)
        results.append(qvac_entry)
        accepted += 1

    stats = {
        "total_input": len(entries),
        "accepted": accepted,
        "skipped_short_response": skipped_short,
        "skipped_exceeds_max_tokens": skipped_long,
        "skipped_duplicate": skipped_dup,
    }

    return results, stats


def estimate_total_tokens(qvac_entries: list[dict]) -> int:
    """Sum estimated token count across all entries."""
    total = 0
    for entry in qvac_entries:
        for msg in entry.get("messages", []):
            total += estimate_tokens(msg.get("content", ""))
    return total


# ---------------------------------------------------------------------------
# Stats display
# ---------------------------------------------------------------------------

def print_stats(
    entries: list[dict],
    domain: str,
    max_tokens: int,
    min_response_len: int,
    input_path: Path,
) -> None:
    """Process and display stats without writing any files."""
    qvac_entries, filter_stats = process_entries(entries, domain, max_tokens, min_response_len)
    total_tokens = estimate_total_tokens(qvac_entries)

    print()
    print("=== QVAC BitNet LoRA Format Stats ===")
    print(f"Source file:           {input_path}")
    print(f"Domain:                {domain}")
    print(f"Max tokens:            {max_tokens} ({max_tokens * CHARS_PER_TOKEN} chars)")
    print(f"Min response length:   {min_response_len} chars")
    print()
    print(f"Total input entries:   {filter_stats['total_input']}")
    print(f"Accepted:              {filter_stats['accepted']}")
    print(f"Skipped (short):       {filter_stats['skipped_short_response']}")
    print(f"Skipped (too long):    {filter_stats['skipped_exceeds_max_tokens']}")
    print(f"Skipped (duplicate):   {filter_stats['skipped_duplicate']}")
    print()
    print(f"Estimated total tokens: {total_tokens}")
    print()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def write_output(
    qvac_entries: list[dict],
    filter_stats: dict,
    domain: str,
    output_dir: Path,
    input_path: Path,
    max_tokens: int,
    min_response_len: int,
) -> Path:
    """Write QVAC JSONL and manifest. Returns path to the JSONL file."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write domain JSONL
    out_path = output_dir / f"{domain}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for entry in qvac_entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    total_tokens = estimate_total_tokens(qvac_entries)

    # Write / update manifest
    manifest_path = output_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)

    manifest["domains"][domain] = {
        "domain": domain,
        "sample_count": len(qvac_entries),
        "total_tokens_estimate": total_tokens,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_file": str(input_path),
        "filters_applied": {
            "min_response_len": min_response_len,
            "max_tokens": max_tokens,
            "deduplication": True,
        },
    }
    manifest["last_updated"] = datetime.now(tz=timezone.utc).isoformat()

    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    return out_path


def _load_manifest(manifest_path: Path) -> dict:
    """Load existing manifest or create a new skeleton."""
    if manifest_path.exists():
        try:
            with manifest_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, KeyError):
            pass
    return {"domains": {}, "last_updated": None}


# ---------------------------------------------------------------------------
# Domain resolution
# ---------------------------------------------------------------------------

def resolve_domain(
    cli_domain: str | None,
    input_path: Path,
    entries: list[dict],
) -> str:
    """
    Determine the domain to use, with the following priority:
      1. Explicit --domain CLI arg
      2. Inferred from input filename (e.g. training-trading.jsonl)
      3. Inferred from task_type in entry metadata (if consistent)
      4. Fallback to 'other'
    """
    if cli_domain:
        return cli_domain

    # Try filename
    from_filename = infer_domain_from_filename(input_path)
    if from_filename:
        return from_filename

    # Try entry metadata (use first entry with task_type)
    for entry in entries:
        task_type = entry.get("task_type", "")
        if task_type:
            return get_domain(task_type)

    return "other"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert approved training captures to QVAC BitNet LoRA format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    %(prog)s --stats
    %(prog)s --domain coding
    %(prog)s --input state/training-exports/training-trading.jsonl --domain trading
    %(prog)s --max-tokens 256 --min-response-len 100
    %(prog)s --output-dir state/qvac-custom
""",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        metavar="FILE",
        help=f"Input JSONL file (default: {DEFAULT_INPUT.relative_to(PROJECT_ROOT)}).",
    )
    parser.add_argument(
        "--domain",
        choices=list(DOMAINS.keys()),
        default=None,
        metavar="DOMAIN",
        help="Target domain (trading/coding/chat/opsec/personal/other). "
             "Auto-detected from filename or entry metadata if omitted.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help=f"Directory for output files (default: {DEFAULT_OUTPUT_DIR.relative_to(PROJECT_ROOT)}).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=512,
        metavar="N",
        help="Max estimated tokens per sample; entries exceeding this are skipped (default: 512).",
    )
    parser.add_argument(
        "--min-response-len",
        type=int,
        default=50,
        metavar="N",
        help="Skip entries with assistant response shorter than N characters (default: 50).",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print stats only; do not write any files.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Resolve input path
    input_path: Path = args.input if args.input else DEFAULT_INPUT
    if not input_path.is_absolute():
        input_path = PROJECT_ROOT / input_path

    # Resolve output dir
    output_dir: Path = args.output_dir
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir

    # Load
    entries = load_entries(input_path)
    if not entries:
        print("No entries loaded (file may be empty or all lines failed to parse).")
        sys.exit(0)

    # Determine domain
    domain = resolve_domain(args.domain, input_path, entries)

    if args.stats:
        print_stats(entries, domain, args.max_tokens, args.min_response_len, input_path)
        return

    # Process
    qvac_entries, filter_stats = process_entries(
        entries, domain, args.max_tokens, args.min_response_len,
    )

    if not qvac_entries:
        print("No entries survived quality filters. Nothing to write.")
        print(f"  Skipped (short):     {filter_stats['skipped_short_response']}")
        print(f"  Skipped (too long):  {filter_stats['skipped_exceeds_max_tokens']}")
        print(f"  Skipped (duplicate): {filter_stats['skipped_duplicate']}")
        sys.exit(0)

    # Write
    out_path = write_output(
        qvac_entries, filter_stats, domain, output_dir,
        input_path, args.max_tokens, args.min_response_len,
    )

    total_tokens = estimate_total_tokens(qvac_entries)

    print()
    print(f"Domain:               {domain}")
    print(f"Input:                {input_path}")
    print(f"Output:               {out_path}")
    print(f"Samples written:      {len(qvac_entries)}")
    print(f"Est. total tokens:    {total_tokens}")
    print(f"Skipped (short):      {filter_stats['skipped_short_response']}")
    print(f"Skipped (too long):   {filter_stats['skipped_exceeds_max_tokens']}")
    print(f"Skipped (duplicate):  {filter_stats['skipped_duplicate']}")
    print(f"Manifest:             {output_dir / 'manifest.json'}")
    print()


if __name__ == "__main__":
    main()
