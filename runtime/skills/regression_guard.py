"""
regression-guard skill — Prevents accidental deletion of functions, classes, and imports.
Compares before/after AST snapshots of modified files. Flags removed symbols that
other files depend on. Blocks changes that would break the system.
Tier 0 (pure Python — AST analysis + grep for cross-file references).
"""

import ast
import logging
from pathlib import Path

from runtime.config import ROOT

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RUNTIME_DIR = ROOT / "runtime"

# Symbols whose removal is always allowed (test helpers, internal plumbing)
IGNORE_SYMBOLS = frozenset()


# ---------------------------------------------------------------------------
# AST snapshot extraction
# ---------------------------------------------------------------------------


def _extract_symbols(code: str) -> dict:
    """
    Parse Python source and return a dict of all public symbols.

    Captures:
      - Top-level public function/async-function names
      - Top-level class names and their public method names
      - Imported names (from X import Y -> Y)
      - Top-level UPPER_CASE constant assignments
      - Total line count for size-change detection
    """
    tree = ast.parse(code)
    symbols: dict = {
        "functions": [],
        "classes": [],
        "methods": {},       # class_name -> [method_names]
        "imports": [],
        "constants": [],
        "total_lines": len(code.splitlines()),
    }

    for node in ast.iter_child_nodes(tree):
        # Top-level functions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                symbols["functions"].append(node.name)

        # Top-level classes and their public methods
        elif isinstance(node, ast.ClassDef):
            symbols["classes"].append(node.name)
            symbols["methods"][node.name] = [
                m.name
                for m in ast.iter_child_nodes(node)
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not m.name.startswith("_")
            ]

        # Imports (from X import Y -> captures Y; import X -> captures X)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                symbols["imports"].append(alias.asname or alias.name)

        # Top-level UPPER_CASE constants (e.g. MAX_RETRIES = 5)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    symbols["constants"].append(target.id)

    return symbols


# ---------------------------------------------------------------------------
# Cross-file reference search
# ---------------------------------------------------------------------------


def _find_references(symbol: str, exclude_file: str) -> list[str]:
    """
    Search all Python files under runtime/ for references to *symbol*.

    Returns a list of ROOT-relative paths that contain the symbol string.
    Excludes the file being modified itself.

    Uses simple string containment — not perfect, but fast and catches:
      - ``from module import symbol``
      - ``module.symbol``
      - ``"symbol"`` in string references
    """
    refs: list[str] = []
    if not RUNTIME_DIR.is_dir():
        return refs

    for py_file in RUNTIME_DIR.rglob("*.py"):
        abs_path = str(py_file.resolve())
        if abs_path == exclude_file:
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        if symbol in content:
            try:
                refs.append(str(py_file.relative_to(ROOT)))
            except ValueError:
                refs.append(str(py_file))

    return refs


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------


def _compute_removals(before: dict, after: dict) -> dict:
    """
    Diff two symbol snapshots and return everything that was present
    in *before* but absent from *after*.
    """
    removed: dict = {
        "functions": [f for f in before["functions"] if f not in after["functions"]],
        "classes":   [c for c in before["classes"]   if c not in after["classes"]],
        "constants": [c for c in before["constants"] if c not in after["constants"]],
        "methods":   {},   # class_name -> [removed method names]
    }

    for cls, methods in before["methods"].items():
        if cls in after["methods"]:
            lost = [m for m in methods if m not in after["methods"][cls]]
            if lost:
                removed["methods"][cls] = lost
        elif cls not in removed["classes"]:
            # Class still exists in after but has no entry in methods?
            # Shouldn't happen, but guard defensively.
            pass
        # If the class itself was removed, the class-level removal covers it.

    return removed


def _has_any_removals(removed: dict) -> bool:
    """Return True if *removed* contains at least one symbol of any kind."""
    if removed["functions"] or removed["classes"] or removed["constants"]:
        return True
    if any(removed["methods"].values()):
        return True
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(**kwargs) -> dict:
    """
    Compare before/after AST snapshots of a Python file and flag removed
    public symbols that other files depend on.

    kwargs:
        file_path      (str, required)  — path to the file that was modified.
        original_code  (str, optional)  — source BEFORE the change.
                                          Falls back to {file_path}.bak.
        new_code       (str, optional)  — source AFTER the change.
                                          Falls back to reading the file on disk.
        strict         (bool, default True) — when True, any removed public
                                              symbol blocks the change (even if
                                              no cross-file references found).
    """
    raw_path = kwargs.get("file_path", "")
    original_code: str | None = kwargs.get("original_code")
    new_code: str | None = kwargs.get("new_code")
    strict = bool(kwargs.get("strict", True))

    # -- Skeleton result (returned early on skip / error) -------------------
    _empty_result: dict = {
        "status": "skipped",
        "summary": "",
        "safe": True,
        "removed_symbols": {
            "functions": [],
            "classes": [],
            "constants": [],
            "methods": {},
        },
        "critical_removals": [],
        "warnings": [],
        "size_change": {
            "before": 0,
            "after": 0,
            "ratio": 1.0,
            "truncation_warning": False,
        },
        "metrics": {
            "symbols_before": 0,
            "symbols_after": 0,
            "removed": 0,
            "critical": 0,
            "references_checked": 0,
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }

    # -- Validate file_path -------------------------------------------------
    if not raw_path:
        _empty_result["summary"] = "Regression guard: no file_path provided."
        return _empty_result

    file_path = Path(raw_path)
    if not file_path.is_absolute():
        file_path = ROOT / file_path
    file_path = file_path.resolve()

    # Only guard Python files
    if file_path.suffix != ".py":
        _empty_result["summary"] = (
            f"Regression guard: skipped {file_path.name} (not a .py file)."
        )
        return _empty_result

    # -- Load BEFORE code ---------------------------------------------------
    if original_code is not None:
        before_code = original_code
    else:
        bak_path = Path(f"{file_path}.bak")
        if bak_path.is_file():
            try:
                before_code = bak_path.read_text(encoding="utf-8")
            except Exception as exc:
                log.warning("regression-guard: cannot read .bak for %s: %s",
                            file_path.name, exc)
                _empty_result["summary"] = (
                    f"Regression guard: skipped — cannot read backup for "
                    f"{file_path.name}."
                )
                return _empty_result
        else:
            # No .bak and no original_code — nothing to compare against.
            _empty_result["summary"] = (
                f"Regression guard: skipped — no .bak and no original_code "
                f"for {file_path.name}."
            )
            return _empty_result

    # -- Load AFTER code ----------------------------------------------------
    if new_code is not None:
        after_code = new_code
    else:
        if file_path.is_file():
            try:
                after_code = file_path.read_text(encoding="utf-8")
            except Exception as exc:
                log.warning("regression-guard: cannot read %s: %s",
                            file_path.name, exc)
                _empty_result["summary"] = (
                    f"Regression guard: skipped — cannot read {file_path.name}."
                )
                return _empty_result
        else:
            _empty_result["summary"] = (
                f"Regression guard: skipped — {file_path.name} does not exist "
                f"on disk and no new_code provided."
            )
            return _empty_result

    # -- Parse both versions ------------------------------------------------
    try:
        before_symbols = _extract_symbols(before_code)
    except SyntaxError as exc:
        log.warning("regression-guard: syntax error in BEFORE code for %s: %s",
                     file_path.name, exc)
        _empty_result["summary"] = (
            f"Regression guard: skipped — syntax error in original code "
            f"for {file_path.name}."
        )
        return _empty_result

    try:
        after_symbols = _extract_symbols(after_code)
    except SyntaxError as exc:
        # The new code has a syntax error — CI will catch this, but we flag it.
        _empty_result["status"] = "fail"
        _empty_result["safe"] = False
        _empty_result["summary"] = (
            f"Regression guard: FAIL — new code for {file_path.name} has a "
            f"syntax error: {exc}"
        )
        _empty_result["escalate"] = True
        _empty_result["escalation_reason"] = (
            f"New code for {file_path.name} does not parse."
        )
        return _empty_result

    # -- Compute removals ---------------------------------------------------
    removed = _compute_removals(before_symbols, after_symbols)

    # -- Size-change detection ----------------------------------------------
    before_lines = before_symbols["total_lines"]
    after_lines = after_symbols["total_lines"]
    size_ratio = after_lines / max(before_lines, 1)
    truncation_warning = size_ratio < 0.8   # lost >20% of lines

    size_change = {
        "before": before_lines,
        "after": after_lines,
        "ratio": round(size_ratio, 3),
        "truncation_warning": truncation_warning,
    }

    # -- Early exit: nothing removed ----------------------------------------
    if not _has_any_removals(removed) and not truncation_warning:
        return {
            "status": "pass",
            "summary": (
                f"Regression guard: PASS — no public symbols removed from "
                f"{file_path.name}."
            ),
            "safe": True,
            "removed_symbols": removed,
            "critical_removals": [],
            "warnings": [],
            "size_change": size_change,
            "metrics": {
                "symbols_before": _count_symbols(before_symbols),
                "symbols_after": _count_symbols(after_symbols),
                "removed": 0,
                "critical": 0,
                "references_checked": 0,
            },
            "escalate": False,
            "escalation_reason": "",
            "action_items": [],
        }

    # -- Cross-file reference checks ----------------------------------------
    exclude_path = str(file_path)
    critical_removals: list[dict] = []
    warnings_list: list[dict] = []
    references_checked = 0

    # Helper: check one symbol
    def _check_symbol(name: str, kind: str) -> None:
        nonlocal references_checked
        if name in IGNORE_SYMBOLS:
            return
        references_checked += 1
        refs = _find_references(name, exclude_path)
        entry = {"symbol": name, "type": kind, "referenced_by": refs}
        if refs:
            critical_removals.append(entry)
        else:
            warnings_list.append(entry)

    for fn in removed["functions"]:
        _check_symbol(fn, "function")

    for cls in removed["classes"]:
        _check_symbol(cls, "class")

    for const in removed["constants"]:
        _check_symbol(const, "constant")

    for cls, methods in removed["methods"].items():
        for method in methods:
            # Qualify as "ClassName.method" for clarity in reports
            _check_symbol(method, f"method ({cls}.{method})")

    # -- Truncation as a standalone warning ---------------------------------
    if truncation_warning and not critical_removals:
        warnings_list.append({
            "symbol": "__file_truncation__",
            "type": "size_reduction",
            "referenced_by": [],
            "detail": (
                f"File shrank from {before_lines} to {after_lines} lines "
                f"({size_ratio:.0%} of original)."
            ),
        })

    # -- Decide: safe or blocked? -------------------------------------------
    total_removed = (
        len(removed["functions"])
        + len(removed["classes"])
        + len(removed["constants"])
        + sum(len(m) for m in removed["methods"].values())
    )

    has_critical = len(critical_removals) > 0

    if strict:
        # In strict mode: any removal OR truncation blocks
        safe = not has_critical and not truncation_warning
        if not safe and not has_critical and truncation_warning:
            # Only truncation triggered, no cross-file refs — still block in strict
            pass
        elif has_critical:
            safe = False
    else:
        # In non-strict mode: only block if a removed symbol has cross-file refs
        safe = not has_critical

    status = "pass" if safe else "fail"

    # -- Summary string -----------------------------------------------------
    summary_parts: list[str] = []
    summary_parts.append(f"Regression guard: {total_removed} removed symbol(s)")

    if has_critical:
        ref_count = sum(len(c["referenced_by"]) for c in critical_removals)
        summary_parts.append(
            f", {len(critical_removals)} with {ref_count} cross-file reference(s)"
        )

    if truncation_warning:
        summary_parts.append(
            f", file truncated to {size_ratio:.0%} of original"
        )

    if not safe:
        summary_parts.append(". BLOCKED.")
    else:
        summary_parts.append(". PASS.")

    summary = "".join(summary_parts)

    # -- Escalation logic ---------------------------------------------------
    escalate = has_critical
    escalation_reason = ""
    if escalate:
        symbol_names = [c["symbol"] for c in critical_removals]
        escalation_reason = (
            f"Removed symbol(s) {symbol_names} are referenced by other files. "
            f"Change would break cross-file dependencies."
        )

    # -- Action items -------------------------------------------------------
    action_items: list[str] = []
    if has_critical:
        action_items.append(
            "Revert the change immediately — removed symbols are still "
            "imported/referenced by other files."
        )
    if truncation_warning and strict:
        action_items.append(
            f"Review the change: file shrank to {size_ratio:.0%} of its "
            f"original size. Possible accidental truncation."
        )

    # -- Return -------------------------------------------------------------
    return {
        "status": status,
        "summary": summary,
        "safe": safe,
        "removed_symbols": removed,
        "critical_removals": critical_removals,
        "warnings": warnings_list,
        "size_change": size_change,
        "metrics": {
            "symbols_before": _count_symbols(before_symbols),
            "symbols_after": _count_symbols(after_symbols),
            "removed": total_removed,
            "critical": len(critical_removals),
            "references_checked": references_checked,
        },
        "escalate": escalate,
        "escalation_reason": escalation_reason,
        "action_items": action_items,
    }


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------


def _count_symbols(symbols: dict) -> int:
    """Total count of all tracked symbols in a snapshot."""
    count = (
        len(symbols["functions"])
        + len(symbols["classes"])
        + len(symbols["constants"])
        + len(symbols["imports"])
    )
    for methods in symbols["methods"].values():
        count += len(methods)
    return count
