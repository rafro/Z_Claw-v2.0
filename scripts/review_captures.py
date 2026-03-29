#!/usr/bin/env python3
"""
Interactive CLI for reviewing LLM training captures before fine-tuning.
Reads from state/training-capture.jsonl, writes approved pairs to state/training-approved.jsonl.
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from runtime.tools.domain_map import compute_capture_hash, get_domain
from runtime.tools.training_manifest import record_capture, record_review

CAPTURE_FILE = PROJECT_ROOT / "state" / "training-capture.jsonl"
APPROVED_FILE = PROJECT_ROOT / "state" / "training-approved.jsonl"

DOMAIN_TASKS = {
    "trading":  ["market-scan", "trading-report"],
    "coding":   ["repo-monitor", "debug-agent", "refactor-scan", "doc-update", "dev-generate", "dev-review", "dev-digest", "dev-summarize", "dev-finalize", "dev-test"],
    "chat":     ["chat-operator"],
    "opsec":    ["threat-surface", "cred-audit", "privacy-scan", "security-scan", "opsec-scan", "opsec-digest", "device-posture", "breach-check"],
    "personal": ["health-logger", "perf-correlation", "burnout-monitor", "personal-digest"],
}

TRUNC_SYSTEM = 300
TRUNC_USER   = 500
TRUNC_ASST   = 600


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def get_tasks_for_domain(domain: str) -> list[str]:
    return DOMAIN_TASKS.get(domain, [])


def load_entries(min_response_len: int, domain: str | None, task_type: str | None) -> list[dict]:
    if not CAPTURE_FILE.exists():
        print(f"Capture file not found: {CAPTURE_FILE}")
        sys.exit(0)

    domain_tasks = get_tasks_for_domain(domain) if domain else None

    entries = []
    with CAPTURE_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            response = entry.get("response", "")
            if len(response) < min_response_len:
                continue

            entry_task = entry.get("task_type", "")

            if domain_tasks is not None and entry_task not in domain_tasks:
                continue

            if task_type is not None and entry_task != task_type:
                continue

            entries.append(entry)

    return entries


def extract_messages(entry: dict) -> tuple[str, str]:
    """Return (system_content, user_content) from the messages list."""
    system_content = ""
    user_content = ""
    for msg in entry.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_content = content
        elif role == "user":
            user_content = content
    return system_content, user_content


def display_entry(entry: dict, index: int, total: int) -> None:
    task_type  = entry.get("task_type", "unknown")
    provider   = entry.get("provider_id", "unknown")
    latency    = entry.get("latency_ms", 0)
    ts         = entry.get("ts", "unknown")
    response   = entry.get("response", "")

    system_content, user_content = extract_messages(entry)

    print("\n" + "=" * 54)
    print(f"[{index}/{total}] task_type: {task_type} | provider: {provider} | latency: {latency}ms")
    print(f"Captured: {ts}")
    print("-" * 54)
    print("SYSTEM:")
    for line in truncate(system_content, TRUNC_SYSTEM).splitlines():
        print(f"  {line}")
    print("-" * 54)
    print("USER:")
    for line in truncate(user_content, TRUNC_USER).splitlines():
        print(f"  {line}")
    print("-" * 54)
    print("ASSISTANT:")
    for line in truncate(response, TRUNC_ASST).splitlines():
        print(f"  {line}")
    print("=" * 54)


def write_approved(entry: dict) -> None:
    system_content, user_content = extract_messages(entry)
    response = entry.get("response", "")

    record = {
        "messages": [
            {"role": "system",    "content": system_content},
            {"role": "user",      "content": user_content},
            {"role": "assistant", "content": response},
        ]
    }

    APPROVED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with APPROVED_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def print_summary(reviewed: int, kept: int, deleted: int, skipped: int) -> None:
    print("\nReview complete.")
    print(f"  Reviewed: {reviewed}")
    print(f"  Kept:     {kept}")
    print(f"  Deleted:  {deleted}")
    print(f"  Skipped:  {skipped}")
    if kept > 0:
        print(f"Approved pairs written to: {APPROVED_FILE}")


def run_review(entries: list[dict]) -> None:
    total    = len(entries)
    reviewed = 0
    kept     = 0
    deleted  = 0
    skipped  = 0

    if total == 0:
        print("No entries match the given filters.")
        return

    try:
        for i, entry in enumerate(entries, start=1):
            display_entry(entry, i, total)
            print("[k] keep  [d] delete  [s] skip  [q] quit  > ", end="", flush=True)

            try:
                choice = input().strip().lower()
            except EOFError:
                print("\nEOF reached.")
                break

            if choice == "k":
                write_approved(entry)
                try:
                    entry_hash = compute_capture_hash(entry.get("messages", []), entry.get("response", ""))
                    domain = get_domain(entry.get("task_type", ""))
                    # Bootstrap: record capture if not already tracked
                    record_capture(entry_hash, domain, entry.get("ts", ""))
                    record_review(entry_hash, approved=True, reviewer="human-cli")
                except Exception as e:
                    print(f"  (manifest warning: {e})")
                kept += 1
                reviewed += 1
            elif choice == "d":
                try:
                    entry_hash = compute_capture_hash(entry.get("messages", []), entry.get("response", ""))
                    domain = get_domain(entry.get("task_type", ""))
                    # Bootstrap: record capture if not already tracked
                    record_capture(entry_hash, domain, entry.get("ts", ""))
                    record_review(entry_hash, approved=False, reviewer="human-cli")
                except Exception as e:
                    print(f"  (manifest warning: {e})")
                deleted += 1
                reviewed += 1
            elif choice == "s":
                skipped += 1
                reviewed += 1
            elif choice == "q":
                print("Quitting...")
                break
            else:
                print(f"  Unknown input '{choice}', treating as skip.")
                skipped += 1
                reviewed += 1

    except KeyboardInterrupt:
        print("\nInterrupted.")

    print_summary(reviewed, kept, deleted, skipped)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactively review LLM training captures and approve pairs for fine-tuning."
    )
    parser.add_argument(
        "--domain",
        choices=list(DOMAIN_TASKS.keys()),
        default=None,
        help="Filter entries by domain (trading/coding/chat/opsec/personal)",
    )
    parser.add_argument(
        "--task-type",
        default=None,
        metavar="TASK",
        help="Filter entries by specific task_type",
    )
    parser.add_argument(
        "--min-response-len",
        type=int,
        default=50,
        metavar="N",
        help="Skip entries with responses shorter than N characters (default: 50)",
    )
    args = parser.parse_args()

    entries = load_entries(
        min_response_len=args.min_response_len,
        domain=args.domain,
        task_type=args.task_type,
    )

    print(f"Loaded {len(entries)} entries from {CAPTURE_FILE}")
    if args.domain:
        print(f"  Domain filter: {args.domain}")
    if args.task_type:
        print(f"  Task-type filter: {args.task_type}")
    if args.min_response_len != 50:
        print(f"  Min response length: {args.min_response_len}")

    run_review(entries)


if __name__ == "__main__":
    main()
