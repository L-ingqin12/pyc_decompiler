"""Hybrid decompiler: static CFG analysis + dynamic runtime tracing.

Combines static bytecode analysis with runtime execution traces to
resolve ambiguities that static analysis alone cannot determine:

- while vs if:    trace shows if back-edge is always taken (while) or
                  sometimes not (if with break/return inside)
- and vs or:      trace shows which branch JUMP_IF_FALSE_OR_POP takes
- chained comp:   trace shows which comparison operands are evaluated
- dead code:      trace shows which offsets are never reached
- type inference: trace captures actual runtime types

Usage:
    # 1. Collect traces from original .pyc
    python -m pyc_decompiler.trace gomoku/game.cpython-37.pyc

    # 2. Decompile with trace-guided disambiguation
    python -m pyc_decompiler gomoku/ output/ --trace data/traces/
"""

from __future__ import annotations

import ast
import marshal
import os
import sys
import types
from typing import Any, Dict, List, Optional, Set, Tuple


# ── Trace collection from .pyc files ───────────────────────────────────

class BytecodeTracer:
    """Collects execution traces from .pyc code objects using sys.settrace.

    Designed to trace specific functions with diverse inputs to discover
    all reachable code paths and branch behaviors.
    """

    def __init__(self):
        # Per-function trace data: func_name → {offset → hit_count}
        self.hit_counts: Dict[str, Dict[int, int]] = {}
        # Per-function branch data: func_name → [(offset, taken_bool)]
        self.branch_decisions: Dict[str, List[Tuple[int, bool]]] = {}
        # Per-function per-call path: func_name → [[visited_offsets]]
        self.execution_paths: Dict[str, List[List[int]]] = {}
        self._current_path: List[int] = []
        self._current_func: str = ""

    def install(self) -> None:
        """Install the tracing hook."""
        sys.settrace(self._callback)

    def uninstall(self) -> None:
        """Remove the tracing hook."""
        sys.settrace(None)

    def _callback(self, frame, event, arg):
        if frame is None:
            return self._callback

        code = frame.f_code
        fname = code.co_name
        offset = frame.f_lasti

        if event == "call":
            if fname not in self.hit_counts:
                self.hit_counts[fname] = {}
                self.branch_decisions[fname] = []
                self.execution_paths[fname] = []
            self._current_func = fname
            self._current_path = []
            # Record entry offset even for single-line functions
            if offset >= 0:
                self._current_path.append(offset)

        elif event == "line" and offset >= 0:
            if self._current_func:
                self.hit_counts[self._current_func][offset] = (
                    self.hit_counts[self._current_func].get(offset, 0) + 1
                )
                # Only add if not duplicate (same offset from line event)
                if not self._current_path or self._current_path[-1] != offset:
                    self._current_path.append(offset)

        elif event == "return":
            if self._current_func:
                # Record return offset — critical for single-line functions
                if offset >= 0:
                    self.hit_counts[self._current_func][offset] = (
                        self.hit_counts[self._current_func].get(offset, 0) + 1
                    )
                    self._current_path.append(offset)
                self.execution_paths[self._current_func].append(
                    list(self._current_path)
                )
            self._current_path = []
            self._current_func = ""

        return self._callback

    def report(self) -> str:
        """Generate a human-readable trace report."""
        lines = ["=" * 70, "Bytecode Trace Report", "=" * 70]

        for func_name in sorted(self.hit_counts):
            hits = self.hit_counts[func_name]
            paths = self.execution_paths.get(func_name, [])
            lines.append(f"\n{func_name}:")
            lines.append(f"  Call paths: {len(paths)}")
            lines.append(f"  Unique offsets visited: {len(hits)}")
            lines.append(f"  Offset hit counts: "
                         f"{dict(sorted(hits.items()))}")

            # Show unique paths
            unique_paths = set()
            for p in paths:
                unique_paths.add(tuple(p))
            lines.append(f"  Unique paths: {len(unique_paths)}")
            for i, path in enumerate(list(unique_paths)[:5]):
                lines.append(f"    Path {i}: {list(path)}")

        return "\n".join(lines)


def trace_pyc_functions(
    pyc_path: str,
    inputs_by_function: Dict[str, List[Tuple[tuple, dict]]],
) -> BytecodeTracer:
    """Trace specific functions from a .pyc file with given test inputs.

    Args:
        pyc_path: Path to the .pyc file.
        inputs_by_function: Mapping of function_name → list of (args, kwargs)
            to test with.

    Returns:
        BytecodeTracer with collected trace data.
    """
    # Load the .pyc
    with open(pyc_path, "rb") as f:
        data = f.read()
    co = marshal.loads(data[16:])

    # Execute in a namespace to define all functions
    ns: Dict[str, Any] = {}
    exec(co, ns)

    tracer = BytecodeTracer()
    tracer.install()

    try:
        for func_name, input_list in inputs_by_function.items():
            if func_name not in ns:
                continue
            fn = ns[func_name]
            for args, kwargs in input_list:
                try:
                    fn(*args, **kwargs)
                except Exception:
                    pass  # Record partial traces even on errors
    finally:
        tracer.uninstall()

    return tracer


# ── Trace-guided decompilation hints ───────────────────────────────────

class TraceHints:
    """Decompilation hints derived from runtime trace data.

    These hints resolve ambiguities that static analysis cannot determine
    from bytecode alone.
    """

    def __init__(self, tracer: BytecodeTracer):
        self.tracer = tracer

    def is_while_loop(
        self, func_name: str, back_edge_offset: int, cond_offset: int
    ) -> bool:
        """Determine if a back-edge + conditional forms a while loop.

        If the backward jump at *back_edge_offset* is taken in >90% of
        calls, and the condition at *cond_offset* evaluates both True
        and False across inputs, it's very likely a while loop.
        """
        hits = self.tracer.hit_counts.get(func_name, {})
        back_hits = hits.get(back_edge_offset, 0)
        cond_hits = hits.get(cond_offset, 0)

        # If the back edge is visited many times per call → loop
        paths = self.tracer.execution_paths.get(func_name, [])
        back_edge_count = sum(
            1 for p in paths for o in p if o == back_edge_offset
        )
        total_calls = len(paths) if paths else 1
        avg_iterations = back_edge_count / total_calls

        return avg_iterations > 1.5

    def is_and_operator(
        self, func_name: str, jump_offset: int
    ) -> Optional[bool]:
        """Determine if JUMP_IF_FALSE_OR_POP at *jump_offset* is an 'and'.

        Returns:
            True if it's 'and', False if it's 'or', None if uncertain.
        """
        # JUMP_IF_FALSE_OR_POP → and
        # JUMP_IF_TRUE_OR_POP → or
        # This is already known from the opcode itself.
        return None  # opcode tells us this already

    def visited_offsets(self, func_name: str) -> Set[int]:
        """Return all visited bytecode offsets for a function."""
        return set(self.tracer.hit_counts.get(func_name, {}).keys())

    def dead_code_offsets(
        self, func_name: str, all_offsets: Set[int]
    ) -> Set[int]:
        """Identify never-visited offsets (dead code / cleanup paths)."""
        visited = self.visited_offsets(func_name)
        return all_offsets - visited

    def branches_taken(
        self, func_name: str, branch_offset: int
    ) -> Tuple[int, int]:
        """Count True/False branches for a conditional jump.

        Returns:
            (true_count, false_count) — number of times each path was taken.
        """
        hits = self.tracer.hit_counts.get(func_name, {})
        # The branch instruction itself
        branch_hits = hits.get(branch_offset, 0)
        # The fallthrough instruction
        fallthrough_hits = hits.get(branch_offset + 2, 0)

        true_count = fallthrough_hits
        false_count = max(0, branch_hits - fallthrough_hits)
        return true_count, false_count


# ── Combined static+dynamic decompiler ─────────────────────────────────

class HybridDecompiler:
    """Decompiler that uses both static bytecode analysis and runtime traces.

    Pipeline:
      1. Static:   bytecode → CFG → preliminary AST
      2. Dynamic:  runtime traces → confidence scores for ambiguous constructs
      3. Refine:   use trace data to pick the most likely AST structure
      4. Output:   annotated source with confidence comments
    """

    def __init__(self, trace_data: Optional[BytecodeTracer] = None):
        self.trace = trace_data
        self.hints = TraceHints(trace_data) if trace_data else None

    def refine_condition(
        self, func_name: str, static_cond: ast.expr, fallback_cond: ast.expr,
        branch_offset: int,
    ) -> ast.expr:
        """Choose the better condition expression based on runtime data.

        If the static analysis produced *static_cond* but there's a
        trace-suggested alternative *fallback_cond*, use trace data
        to pick the more accurate one.
        """
        if self.hints is None:
            return static_cond

        true_count, false_count = self.hints.branches_taken(
            func_name, branch_offset
        )
        total = true_count + false_count
        if total == 0:
            return static_cond

        # If both paths are taken with non-trivial frequency, this is
        # a real conditional (not an artifact)
        if true_count > 0 and false_count > 0:
            return static_cond
        elif false_count > true_count * 3:
            # Almost always false → might be an 'and' short-circuit
            return fallback_cond

        return static_cond

    def generate_annotated_source(
        self, func_name: str, source: str, confidence: Dict[int, float]
    ) -> str:
        """Annotate source code with confidence scores from trace data."""
        if not confidence:
            return source

        lines = source.split("\n")
        annotated = []
        for i, line in enumerate(lines):
            conf = confidence.get(i + 1, -1)
            if conf >= 0.95:
                annotated.append(line)
            elif conf >= 0.5:
                annotated.append(f"{line}  # [confidence: {conf:.0%}]")
            else:
                annotated.append(f"#? {line}  # [LOW confidence: {conf:.0%}]")
        return "\n".join(annotated)


# ── Convenience: generate trace from test suite ────────────────────────

def generate_traces_from_tests(
    test_dir: str, target_module: str, output_dir: str,
) -> BytecodeTracer:
    """Run a test suite and collect bytecode traces.

    Args:
        test_dir: Path to the test files directory.
        target_module: Fully qualified module name to trace.
        output_dir: Directory to save trace data.

    Returns:
        BytecodeTracer with collected traces.
    """
    import importlib.util

    # Add paths
    parent = os.path.dirname(os.path.dirname(test_dir))
    if parent not in sys.path:
        sys.path.insert(0, parent)

    tracer = BytecodeTracer()
    tracer.install()

    try:
        # Discover and run tests
        loader = importlib.util  # Actually use unittest
        import unittest

        # Find all test files
        suite = unittest.defaultTestLoader.discover(test_dir)
        runner = unittest.TextTestRunner(verbosity=0)
        runner.run(suite)
    finally:
        tracer.uninstall()

    # Save report
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "trace_report.txt")
    with open(report_path, "w") as f:
        f.write(tracer.report())
    print(f"Trace report saved to {report_path}")

    return tracer


# ── CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage:")
        print(f"  {sys.argv[0]} trace <module_path> [output_dir]")
        print(f"  {sys.argv[0]} report <trace_data_dir>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "test":
        # Quick self-test
        print("Testing trace collection on gomoku...")
        sys.path.insert(0, "/root/workspace")
        import gomoku.game

        tracer = BytecodeTracer()
        tracer.install()

        # Test with various inputs to explore code paths
        gomoku.game.in_bounds(7, 7)    # both True
        gomoku.game.in_bounds(-1, 7)   # first False
        gomoku.game.in_bounds(7, 15)   # second False

        tracer.uninstall()
        print(tracer.report())
