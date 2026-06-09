"""Runtime bytecode tracing for decompilation guidance.

Injects tracing hooks into .pyc execution to collect runtime information
that guides decompilation. Uses three complementary approaches:

1. sys.settrace — lightweight tracing of function calls, returns, and
   line-level execution paths. Works for any Python version.

2. Bytecode injection — modifies bytecode to insert tracing calls at
   branch points (POP_JUMP_IF_FALSE, JUMP_IF_FALSE_OR_POP, FOR_ITER).
   Requires rewriting jump targets; used for detailed branch analysis.

3. Frame inspection — at trace points, inspects stack frames to capture
   local variables and partial expression values.

The collected trace data resolves ambiguities that static bytecode
analysis cannot: distinguishing while from if, recovering boolean
short-circuit patterns, resolving dynamic types.
"""

from __future__ import annotations

import base64
import dis
import json
import sys
import types
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class TraceEvent:
    """A single trace event recorded during execution."""

    __slots__ = (
        "event_type", "func_name", "filename", "lineno",
        "bytecode_offset", "locals_snapshot", "stack_depth",
        "branch_taken", "return_value",
    )

    def __init__(
        self,
        event_type: str,
        func_name: str = "",
        filename: str = "",
        lineno: int = 0,
        bytecode_offset: int = 0,
        locals_snapshot: Optional[Dict[str, Any]] = None,
        stack_depth: int = 0,
        branch_taken: Optional[bool] = None,
        return_value: Any = None,
    ):
        self.event_type = event_type
        self.func_name = func_name
        self.filename = filename
        self.lineno = lineno
        self.bytecode_offset = bytecode_offset
        self.locals_snapshot = locals_snapshot or {}
        self.stack_depth = stack_depth
        self.branch_taken = branch_taken
        self.return_value = return_value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event_type,
            "func": self.func_name,
            "file": self.filename,
            "line": self.lineno,
            "offset": self.bytecode_offset,
            "locals": {k: repr(v) for k, v in self.locals_snapshot.items()},
            "stack_depth": self.stack_depth,
            "branch": self.branch_taken,
            "return": repr(self.return_value) if self.return_value is not None else None,
        }


class RuntimeTracer:
    """Collects execution traces from .pyc code for decompilation guidance.

    Usage:
        tracer = RuntimeTracer()
        tracer.install()

        # Run the code to trace (original .pyc or decompiled .py)
        import my_module
        my_module.some_function(1, 2, 3)

        tracer.uninstall()

        # Analyze traces
        report = tracer.analyze()
        print(report)
    """

    def __init__(self, trace_file: Optional[str] = None):
        self.events: List[TraceEvent] = []
        self._installed = False
        self._old_trace = None
        self._call_stack: List[int] = []  # track nesting depth
        self._trace_file = trace_file
        self._frame_counts: Dict[int, int] = {}  # offset → hit count

    # ── Public API ──────────────────────────────────────────────────────

    def install(self) -> None:
        """Install the tracing hook."""
        if self._installed:
            return
        self._old_trace = sys.gettrace()
        sys.settrace(self._trace_callback)
        self._installed = True

    def uninstall(self) -> None:
        """Remove the tracing hook."""
        if not self._installed:
            return
        sys.settrace(self._old_trace)
        self._installed = False

    def reset(self) -> None:
        """Clear all collected events."""
        self.events.clear()
        self._frame_counts.clear()
        self._call_stack.clear()

    def analyze(self) -> TraceReport:
        """Analyze collected traces and produce a decompilation guidance report."""
        return TraceReport(self.events, self._frame_counts)

    def save(self, filepath: str) -> None:
        """Save trace events to a JSON file."""
        with open(filepath, "w") as f:
            json.dump([e.to_dict() for e in self.events], f, indent=2)

    # ── Debug helpers ───────────────────────────────────────────────────

    def dump_bytecode(self, func: Callable) -> str:
        """Disassemble a function's bytecode for trace correlation."""
        import io
        out = io.StringIO()
        dis.dis(func, file=out)
        return out.getvalue()

    # ── Internal: sys.settrace callback ─────────────────────────────────

    def _trace_callback(self, frame, event, arg):
        """sys.settrace callback — records execution flow events."""
        if frame is None:
            return self._trace_callback

        code = frame.f_code
        func_name = code.co_name
        filename = code.co_filename
        lineno = frame.f_lineno
        offset = frame.f_lasti

        if event == "call":
            self._call_stack.append(id(frame))
            # Snapshot locals for function entry
            locals_snap = {
                k: v for k, v in frame.f_locals.items()
                if not k.startswith("__") and not isinstance(v, types.ModuleType)
            }
            self._record(TraceEvent(
                event_type="call",
                func_name=func_name,
                filename=filename,
                lineno=lineno,
                bytecode_offset=offset,
                locals_snapshot=locals_snap,
            ))

        elif event == "return":
            self._record(TraceEvent(
                event_type="return",
                func_name=func_name,
                filename=filename,
                lineno=lineno,
                bytecode_offset=offset,
                return_value=arg,
            ))
            if self._call_stack:
                self._call_stack.pop()

        elif event == "line":
            # Track which bytecode offsets are hit
            self._frame_counts[offset] = self._frame_counts.get(offset, 0) + 1

            # At branch points, log local state to help determine conditions
            self._record(TraceEvent(
                event_type="line",
                func_name=func_name,
                filename=filename,
                lineno=lineno,
                bytecode_offset=offset,
                stack_depth=len(self._call_stack),
            ))

        elif event == "exception":
            self._record(TraceEvent(
                event_type="exception",
                func_name=func_name,
                filename=filename,
                lineno=lineno,
                bytecode_offset=offset,
                return_value=arg,  # exception info
            ))

        return self._trace_callback

    def _record(self, event: TraceEvent) -> None:
        """Record an event, optionally writing to file."""
        self.events.append(event)
        if self._trace_file:
            with open(self._trace_file, "a") as f:
                f.write(json.dumps(event.to_dict()) + "\n")


class TraceReport:
    """Analysis of execution traces for decompilation guidance."""

    def __init__(self, events: List[TraceEvent], frame_counts: Dict[int, int]):
        self.events = events
        self.frame_counts = frame_counts

        # Per-function analysis
        self.functions: Dict[str, FunctionTrace] = {}
        self._build()

    def _build(self) -> None:
        """Build per-function trace summaries."""
        current_func = None
        call_depth = 0

        for e in self.events:
            if e.event_type == "call":
                call_depth += 1
                if e.func_name not in self.functions:
                    self.functions[e.func_name] = FunctionTrace(e.func_name)
                current_func = self.functions[e.func_name]
                current_func.call_count += 1
                current_func.entry_offsets.add(e.bytecode_offset)

            elif e.event_type == "return" and current_func:
                current_func.return_offsets.add(e.bytecode_offset)
                if call_depth <= 1:
                    current_func = None

            elif e.event_type == "line" and current_func:
                current_func.visited_offsets.add(e.bytecode_offset)

    def summary(self) -> str:
        """Return a human-readable summary."""
        lines = ["=" * 60, "Runtime Trace Analysis", "=" * 60]
        for name, ft in sorted(self.functions.items()):
            lines.append(f"\n  {name}:")
            lines.append(f"    calls: {ft.call_count}")
            lines.append(f"    visited {len(ft.visited_offsets)} unique offsets")
            lines.append(f"    entry offsets: {sorted(ft.entry_offsets)}")
            lines.append(f"    return offsets: {sorted(ft.return_offsets)}")
        return "\n".join(lines)

    def branch_dominance(self, branch_offsets: List[int]) -> Dict[int, float]:
        """For each branch offset, return the ratio of True vs False paths.

        A high ratio (>0.9) suggests the branch is a loop back-edge
        (almost always taken) or an error path (almost never taken).
        """
        result = {}
        for offset in branch_offsets:
            count = self.frame_counts.get(offset, 0)
            # The next instruction's count represents the True/fallthrough path
            next_offset = offset + 2  # wordcode: each instr is 2 bytes
            next_count = self.frame_counts.get(next_offset, 0)
            total = count + next_count
            if total > 0:
                result[offset] = next_count / total
            else:
                result[offset] = 0.0
        return result


class FunctionTrace:
    """Per-function trace statistics."""

    def __init__(self, name: str):
        self.name = name
        self.call_count = 0
        self.visited_offsets: Set[int] = set()
        self.entry_offsets: Set[int] = set()
        self.return_offsets: Set[int] = set()


# ── Bytecode Injection Engine ──────────────────────────────────────────
#
# Python bytecode instructions are 2 bytes (wordcode). To inject tracing
# into an existing code object, we:
# 1. Generate new bytecode with tracing instructions inserted
# 2. Adjust all jump offsets to account for inserted bytes
# 3. Rebuild the code object with the new bytecode
#
# Tracing injection points:
#   - Before POP_JUMP_IF_FALSE:   record condition value and which path
#   - Before JUMP_IF_FALSE_OR_POP: record left operand of and/or
#   - Before FOR_ITER:            record loop iteration count
#   - Before RETURN_VALUE:        record return value


# Minimal bytecode templates (Python 3.7 wordcode):
# Each injected sequence must preserve the stack.
#
# STACK_SNAPSHOT injection (no net stack effect):
#   LOAD_GLOBAL <tracer_func>     (3 bytes with EXTENDED_ARG if needed)
#   CALL_FUNCTION 0               (3 bytes)
#   POP_TOP                       (1 byte)
#
# Total: ~7 bytes per injection point. All jump offsets in the original
# bytecode must be increased by 7× (number of prior injection points).


def inject_tracing(
    code: types.CodeType,
    trace_func_name: str = "_pyc_tracer_hook",
    inject_at_jumps: bool = True,
    inject_at_returns: bool = True,
) -> types.CodeType:
    """Inject tracing calls into a code object's bytecode.

    Inserts calls to *trace_func_name* (a global function) at key points:
    jump instructions and return statements. The trace function receives
    the bytecode offset and can log state.

    Args:
        code: The code object to instrument.
        trace_func_name: Name of the global function to call at trace points.
        inject_at_jumps: Whether to inject before conditional jumps.
        inject_at_returns: Whether to inject before return statements.

    Returns:
        A new code object with tracing bytecode injected.
    """
    bytecode = code.co_code
    instructions = _parse_instructions(bytecode)

    # Find injection points (instruction indices)
    inject_at: Set[int] = set()
    for i, (offset, opcode, arg) in enumerate(instructions):
        opname = dis.opname[opcode]
        if inject_at_jumps and opname in {
            "POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE",
            "JUMP_IF_FALSE_OR_POP", "JUMP_IF_TRUE_OR_POP",
            "FOR_ITER",
        }:
            inject_at.add(i)
        if inject_at_returns and opname == "RETURN_VALUE":
            inject_at.add(i)

    if not inject_at:
        return code

    # Build tracing bytecode for a single injection
    # LOAD_GLOBAL <trace_func> (opcode 116)
    # CALL_FUNCTION 0 (opcode 131, arg 0)
    # POP_TOP (opcode 1)
    #
    # We need to find the arg index for trace_func_name in co_names.
    # If not present, we can't easily add it without rebuilding the code object.
    # Simpler: use a pre-registered function that's always available.
    #
    # Actually, let's use LOAD_CONST (name) + LOAD_GLOBAL style.
    # Or: use LOAD_GLOBAL directly. trace_func_name must be in co_names.
    names = list(code.co_names)
    if trace_func_name not in names:
        names.append(trace_func_name)
    trace_name_idx = names.index(trace_func_name)

    # Build new bytecode with injections
    new_bytecode = bytearray()
    old_to_new_offset: Dict[int, int] = {}  # map old offsets → new offsets

    for i, (old_offset, opcode, arg) in enumerate(instructions):
        # Map old offset to new offset
        new_offset = len(new_bytecode)
        old_to_new_offset[old_offset] = new_offset

        # Inject tracing before this instruction if needed
        if i in inject_at:
            # LOAD_GLOBAL <trace_name_idx>
            if trace_name_idx < 256:
                new_bytecode.extend([116, trace_name_idx])
            else:
                # Need EXTENDED_ARG for large name index
                ext = trace_name_idx >> 8
                new_bytecode.extend([144, ext])  # EXTENDED_ARG
                new_bytecode.extend([116, trace_name_idx & 0xFF])

            # CALL_FUNCTION 0
            new_bytecode.extend([131, 0])

            # POP_TOP
            new_bytecode.extend([1, 0])

        # Write the original instruction, adjusting jump targets
        if opcode >= dis.HAVE_ARGUMENT:
            new_arg = _adjust_jump_arg(opname, arg, old_to_new_offset, injections_before=len(inject_at))
            if opcode == 144:  # EXTENDED_ARG
                new_bytecode.extend([opcode, arg])
            elif arg < 256:
                new_bytecode.extend([opcode, new_arg & 0xFF])
            else:
                ext = new_arg >> 8
                new_bytecode.extend([144, ext])  # EXTENDED_ARG
                new_bytecode.extend([opcode, new_arg & 0xFF])
        else:
            new_bytecode.extend([opcode, 0])

    # Rebuild code object with new bytecode and names
    new_code = types.CodeType(
        code.co_argcount,
        code.co_kwonlyargcount,
        code.co_nlocals,
        code.co_stacksize + 3,  # extra stack space for tracing
        code.co_flags,
        bytes(new_bytecode),
        code.co_consts,
        tuple(names),
        code.co_varnames,
        code.co_filename,
        code.co_name,
        code.co_firstlineno,
        code.co_lnotab,
        code.co_freevars,
        code.co_cellvars,
    )
    return new_code


def _parse_instructions(bytecode: bytes) -> List[Tuple[int, int, int]]:
    """Parse wordcode bytecode into (offset, opcode, arg) tuples."""
    instructions = []
    i = 0
    extended_arg = 0
    while i < len(bytecode) - 1:
        opcode = bytecode[i]
        arg = bytecode[i + 1]
        offset = i

        if opcode == 144:  # EXTENDED_ARG
            extended_arg = (extended_arg << 8) | arg
            instructions.append((offset, opcode, arg))
            i += 2
            continue

        full_arg = (extended_arg << 8) | arg
        instructions.append((offset, opcode, full_arg))
        extended_arg = 0
        i += 2

    return instructions


def _adjust_jump_arg(
    opname: str, arg: int,
    old_to_new: Dict[int, int],
    injections_before: int,
) -> int:
    """Adjust a jump argument to account for injected bytecode.

    For absolute jumps (JUMP_ABSOLUTE, POP_JUMP_IF_*, etc.), the argument
    is a bytecode offset that needs remapping.
    """
    if opname in {
        "JUMP_ABSOLUTE", "POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE",
        "JUMP_IF_FALSE_OR_POP", "JUMP_IF_TRUE_OR_POP",
        "FOR_ITER", "SETUP_LOOP", "SETUP_EXCEPT", "SETUP_FINALLY",
        "SETUP_WITH", "SETUP_ASYNC_WITH",
    }:
        if arg in old_to_new:
            return old_to_new[arg]
    elif opname in {"JUMP_FORWARD"}:
        # Relative jump: the injected code shifts the target forward
        # This is approximate — exact calculation requires knowing which
        # injections are between the jump and its target.
        return arg + injections_before * 7
    elif opname == "JUMP_BACKWARD":
        # Backward relative jump
        return arg
    return arg


# ── Tracing hook function ──────────────────────────────────────────────

# Global registry of tracer hooks (one per traced module)
_tracer_registry: Dict[str, RuntimeTracer] = {}


def _pyc_tracer_hook() -> None:
    """Called from injected bytecode before jump/return instructions.

    Uses sys._getframe(1) to inspect the calling frame and record the
    current bytecode offset, stack depth, and local variables.
    """
    frame = sys._getframe(1)  # caller's frame
    if frame is None:
        return

    code = frame.f_code
    offset = frame.f_lasti
    func_name = code.co_name

    # Find or create tracer for this module
    mod_name = code.co_filename
    if mod_name not in _tracer_registry:
        return

    tracer = _tracer_registry[mod_name]
    tracer._frame_counts[offset] = tracer._frame_counts.get(offset, 0) + 1

    # Capture locals (avoid recursive tracing for tracer itself)
    locals_snap = {}
    for k, v in frame.f_locals.items():
        if k.startswith("_"):
            continue
        try:
            locals_snap[k] = repr(v)
        except Exception:
            locals_snap[k] = "<unrepresentable>"

    tracer.events.append(TraceEvent(
        event_type="hook",
        func_name=func_name,
        filename=mod_name,
        lineno=frame.f_lineno,
        bytecode_offset=offset,
        locals_snapshot=locals_snap,
    ))


def register_tracer(module_name: str, tracer: RuntimeTracer) -> None:
    """Register a tracer for a specific module."""
    _tracer_registry[module_name] = tracer


# ── Convenience: trace a Python file with sys.settrace ─────────────────

def trace_module(module_path: str, output_path: Optional[str] = None) -> TraceReport:
    """Run a Python module with tracing enabled and return the report.

    Args:
        module_path: Path to the .py or .pyc file to trace.
        output_path: Optional path to save trace JSON.

    Returns:
        TraceReport with execution analysis.
    """
    import importlib.util
    import os

    tracer = RuntimeTracer(trace_file=output_path)
    tracer.install()

    try:
        # Load and execute the module
        mod_name = os.path.splitext(os.path.basename(module_path))[0]
        spec = importlib.util.spec_from_file_location(mod_name, module_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    finally:
        tracer.uninstall()

    return tracer.analyze()


def trace_function(func: Callable, *args, **kwargs) -> TraceReport:
    """Trace a single function call and return the report."""
    tracer = RuntimeTracer()
    tracer.install()
    try:
        func(*args, **kwargs)
    except Exception:
        pass
    finally:
        tracer.uninstall()
    return tracer.analyze()
