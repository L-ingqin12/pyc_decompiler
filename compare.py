"""Runtime comparison: original .pyc vs decompiled .py behavior.

Runs the same test inputs against both original and decompiled code,
compares outputs, and produces a per-function correctness report.
Uses the execution trace to guide decompiler fixes.

Usage:
    python -m pyc_decompiler.compare gomoku_test/ gomoku_decomp_test/
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import traceback
import types
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class FunctionComparator:
    """Compares original and decompiled function behavior."""

    def __init__(self, orig_module, decomp_module):
        self.orig_mod = orig_module
        self.decomp_mod = decomp_module
        self.results: Dict[str, CompareResult] = {}

    def compare_all_functions(self) -> Dict[str, CompareResult]:
        """Compare all public functions between original and decompiled modules."""
        orig_funcs = {
            name: obj for name, obj in vars(self.orig_mod).items()
            if callable(obj) and not name.startswith('_')
            and not isinstance(obj, type)  # skip classes for now
        }
        decomp_funcs = {
            name: obj for name, obj in vars(self.decomp_mod).items()
            if callable(obj) and not name.startswith('_')
        }

        all_names = set(orig_funcs) & set(decomp_funcs)
        for name in sorted(all_names):
            self.results[name] = self._compare_function(
                name, orig_funcs[name], decomp_funcs.get(name)
            )

        # Also report functions only in original (missing from decompiled)
        for name in sorted(set(orig_funcs) - set(decomp_funcs)):
            self.results[name] = CompareResult(
                name=name,
                status="missing",
                error="Function not found in decompiled module",
            )

        return self.results

    def _compare_function(
        self, name: str, orig_fn: Callable, decomp_fn: Optional[Callable]
    ) -> CompareResult:
        """Compare a single function's behavior."""
        if decomp_fn is None:
            return CompareResult(name=name, status="missing")

        result = CompareResult(name=name, status="unknown")

        # Check signature match
        try:
            import inspect
            orig_sig = inspect.signature(orig_fn)
            decomp_sig = inspect.signature(decomp_fn)
            if str(orig_sig) != str(decomp_sig):
                result.warnings.append(
                    f"Signature mismatch: orig={orig_sig} decomp={decomp_sig}"
                )
        except (ValueError, TypeError):
            pass

        # Try calling both with the same test inputs
        test_cases = _generate_test_inputs(orig_fn)
        passed = 0
        failed = 0

        for test_args, test_kwargs in test_cases:
            try:
                # Call original
                orig_result = orig_fn(*test_args, **test_kwargs)

                # Call decompiled
                try:
                    decomp_result = decomp_fn(*test_args, **test_kwargs)
                except Exception as e:
                    result.errors.append(
                        f"Decompiled function raised {type(e).__name__}: {e}"
                    )
                    failed += 1
                    continue

                # Compare results
                if not _values_equal(orig_result, decomp_result):
                    result.errors.append(
                        f"Result mismatch for args={test_args}: "
                        f"orig={orig_result!r} decomp={decomp_result!r}"
                    )
                    failed += 1
                else:
                    passed += 1

            except Exception as e:
                result.errors.append(
                    f"Original function raised {type(e).__name__}: {e}"
                )
                failed += 1

        result.passed = passed
        result.failed = failed
        if passed > 0 and failed == 0:
            result.status = "pass"
        elif passed > 0:
            result.status = "partial"
        else:
            result.status = "fail"

        return result


def _generate_test_inputs(fn: Callable) -> List[Tuple[Tuple, Dict]]:
    """Generate reasonable test inputs for a function based on its signature."""
    import inspect

    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return [((), {})]

    params = list(sig.parameters.values())
    if not params or all(
        p.name == 'self' or p.default is not inspect.Parameter.empty
        for p in params
    ):
        return [((), {})]

    # Generate type-appropriate test values
    test_values = []
    for param in params:
        if param.name.startswith('_') or param.name == 'self':
            test_values.append(None)
            continue

        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            annotation = None

        default = param.default
        if default is not inspect.Parameter.empty:
            test_values.append(default)
            continue

        # Heuristic: use 0 for int, 0.0 for float, [] for list, etc.
        if annotation == int or (annotation is None and 'n' in param.name.lower()):
            test_values.append(0)
            test_values.append(5)  # also test with positive value
        elif annotation == float:
            test_values.append(0.0)
        elif annotation == bool:
            test_values.append(False)
            test_values.append(True)
        elif annotation == str:
            test_values.append("")
        elif annotation == list:
            test_values.append([])
        elif annotation == tuple:
            test_values.append(())
        elif annotation == dict:
            test_values.append({})
        else:
            test_values.append(0)

    # Build test cases
    test_cases = []
    # Test with zeros/defaults
    test_cases.append((test_values[:len(params)], {}))
    return test_cases


def _values_equal(a: Any, b: Any) -> bool:
    """Compare two values for equality, handling edge cases."""
    if type(a) != type(b):
        return False
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        if len(a) != len(b):
            return False
        return all(k in b and _values_equal(a[k], b[k]) for k in a)
    try:
        return a == b
    except Exception:
        return False


class CompareResult:
    """Result of comparing a single function."""

    def __init__(self, name: str, status: str = "unknown", error: str = ""):
        self.name = name
        self.status = status  # pass, partial, fail, missing
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.passed = 0
        self.failed = 0
        if error:
            self.errors.append(error)

    def __repr__(self):
        return (f"CompareResult({self.name!r}, status={self.status!r}, "
                f"passed={self.passed}, failed={self.failed})")


# ── Module-level comparison ────────────────────────────────────────────

def compare_modules(orig_pkg_path: str, decomp_pkg_path: str):
    """Compare all modules between original and decompiled packages.

    For each module, loads both versions, compares all public functions,
    and prints a report.
    """
    # Add paths to sys.path
    orig_parent = os.path.dirname(orig_pkg_path)
    decomp_parent = os.path.dirname(decomp_pkg_path)
    if orig_parent not in sys.path:
        sys.path.insert(0, orig_parent)
    if decomp_parent not in sys.path:
        sys.path.insert(0, decomp_parent)

    # Find common .py files
    orig_files = {
        f[:-3] for f in os.listdir(orig_pkg_path)
        if f.endswith('.py') and not f.startswith('_')
    }

    for mod_name in sorted(orig_files):
        print(f"\n{'='*60}")
        print(f"  Module: {mod_name}")
        print(f"{'='*60}")

        try:
            # Import original
            orig_spec = importlib.util.spec_from_file_location(
                f"orig_{mod_name}",
                os.path.join(orig_pkg_path, f"{mod_name}.py"),
            )
            orig_mod = importlib.util.module_from_spec(orig_spec)
            orig_spec.loader.exec_module(orig_mod)

            # Import decompiled
            decomp_spec = importlib.util.spec_from_file_location(
                f"decomp_{mod_name}",
                os.path.join(decomp_pkg_path, f"{mod_name}.py"),
            )
            decomp_mod = importlib.util.module_from_spec(decomp_spec)

            try:
                decomp_spec.loader.exec_module(decomp_mod)
            except SyntaxError as e:
                print(f"  SYNTAX ERROR in decompiled module: {e}")
                continue
            except Exception as e:
                print(f"  IMPORT ERROR for decompiled module: {e}")
                traceback.print_exc()
                continue

            # Compare
            comparator = FunctionComparator(orig_mod, decomp_mod)
            results = comparator.compare_all_functions()

            for name, result in sorted(results.items()):
                status_icon = {"pass": "✅", "partial": "⚠️", "fail": "❌",
                              "missing": "🚫"}.get(result.status, "❓")
                print(f"  {status_icon} {name}: {result.status} "
                      f"({result.passed}/{result.passed + result.failed})")
                for err in result.errors:
                    print(f"      Error: {err}")
                for warn in result.warnings:
                    print(f"      Warn: {warn}")

        except Exception as e:
            print(f"  Failed to load module: {e}")


# ── CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <original_pkg_dir> <decompiled_pkg_dir>")
        sys.exit(1)

    compare_modules(sys.argv[1], sys.argv[2])
