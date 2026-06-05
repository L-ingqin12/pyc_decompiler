"""Core data types for the Python bytecode decompiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Any


@dataclass
class Instruction:
    """A single bytecode instruction."""

    offset: int          # byte offset in original bytecode
    opcode: int          # numeric opcode
    opname: str          # human-readable opcode name
    arg: int             # raw argument value (0 if no arg)
    argval: Any = None   # resolved argument value (var name, const value, target offset, etc.)
    lineno: int = -1     # source line number (from lnotab)
    is_jump_target: bool = False  # whether this instruction is a jump/branch target

    @property
    def is_jump(self) -> bool:
        """Whether this instruction is an unconditional jump."""
        return self.opname in {
            "JUMP_FORWARD", "JUMP_ABSOLUTE", "JUMP_BACKWARD",
            "RETURN_VALUE", "RAISE_VARARGS", "RERAISE",
            "BREAK_LOOP", "CONTINUE_LOOP",
        }

    @property
    def is_conditional_jump(self) -> bool:
        """Whether this instruction is a conditional jump."""
        return self.opname in {
            "POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE",
            "JUMP_IF_FALSE_OR_POP", "JUMP_IF_TRUE_OR_POP",
            "FOR_ITER", "SETUP_FINALLY", "SETUP_EXCEPT",
            "SETUP_WITH", "SETUP_ASYNC_WITH",
        }

    @property
    def is_return(self) -> bool:
        """Whether this instruction terminates with a return."""
        return self.opname == "RETURN_VALUE"

    @property
    def is_raise(self) -> bool:
        """Whether this instruction raises an exception."""
        return self.opname in {"RAISE_VARARGS", "RERAISE"}

    @property
    def target_offset(self) -> Optional[int]:
        """Target offset for jump instructions, if computable."""
        if self.opname in {"JUMP_FORWARD", "SETUP_FINALLY", "SETUP_EXCEPT",
                           "SETUP_WITH", "SETUP_ASYNC_WITH", "FOR_ITER"}:
            return self.offset + 2 + self.arg
        elif self.opname in {"JUMP_ABSOLUTE", "POP_JUMP_IF_FALSE",
                             "POP_JUMP_IF_TRUE", "JUMP_IF_FALSE_OR_POP",
                             "JUMP_IF_TRUE_OR_POP"}:
            return self.arg
        elif self.opname == "JUMP_BACKWARD":
            return self.offset + 2 - self.arg
        return None

    @property
    def falls_through(self) -> bool:
        """Whether execution can fall through to the next instruction."""
        if self.is_return or self.is_raise:
            return False
        if self.opname in {
            "JUMP_FORWARD", "JUMP_ABSOLUTE", "JUMP_BACKWARD",
            "BREAK_LOOP", "CONTINUE_LOOP",
        }:
            return False
        return True

    def __repr__(self) -> str:
        parts = [f"{self.offset:4d} {self.opname:24s}"]
        if self.arg > 0:
            parts.append(f"{self.arg:4d}")
            if self.argval is not None:
                parts.append(f"({self.argval!r})")
        return " ".join(parts)


@dataclass
class BasicBlock:
    """A basic block: a sequence of instructions with one entry point and no
    internal branches."""

    id: int
    instructions: List[Instruction] = field(default_factory=list)

    # CFG edges
    successor_ids: List[int] = field(default_factory=list)
    predecessor_ids: List[int] = field(default_factory=list)

    # CFG flags
    is_entry: bool = False
    is_exit: bool = False
    is_loop_header: bool = False
    is_exception_handler: bool = False
    is_finally_block: bool = False

    @property
    def last_instruction(self) -> Optional[Instruction]:
        return self.instructions[-1] if self.instructions else None

    @property
    def first_instruction(self) -> Optional[Instruction]:
        return self.instructions[0] if self.instructions else None

    @property
    def start_offset(self) -> int:
        return self.instructions[0].offset if self.instructions else -1

    @property
    def end_offset(self) -> int:
        return self.instructions[-1].offset if self.instructions else -1

    @property
    def terminator(self) -> Optional[Instruction]:
        """The instruction that terminates this block (jump, return, raise)."""
        last = self.last_instruction
        if last and not last.falls_through:
            return last
        return None

    def __repr__(self) -> str:
        flags = []
        if self.is_entry:
            flags.append("entry")
        if self.is_exit:
            flags.append("exit")
        if self.is_loop_header:
            flags.append("loop_header")
        if self.is_exception_handler:
            flags.append("exc_handler")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        return (f"Block({self.id}){flag_str} "
                f"preds={self.predecessor_ids} succs={self.successor_ids} "
                f"range={self.start_offset}-{self.end_offset}"
                f" ({len(self.instructions)} instrs)")


@dataclass
class CodeObjectInfo:
    """Metadata and bytecode extracted from a code object."""

    co_name: str
    co_filename: str
    co_firstlineno: int
    co_argcount: int
    co_kwonlyargcount: int
    co_nlocals: int
    co_stacksize: int
    co_flags: int
    co_consts: Tuple[Any, ...]
    co_names: Tuple[str, ...]
    co_varnames: Tuple[str, ...]
    co_cellvars: Tuple[str, ...]
    co_freevars: Tuple[str, ...]
    co_lnotab: bytes
    raw_bytecode: bytes

    # Decompiled results
    instructions: List[Instruction] = field(default_factory=list)
    blocks: List[BasicBlock] = field(default_factory=list)
    ast_node: Optional[Any] = None  # ast.Module or ast.FunctionDef, etc.

    # Nested code objects
    nested: List[CodeObjectInfo] = field(default_factory=list)

    def __repr__(self) -> str:
        return (f"CodeObject(name={self.co_name!r}, "
                f"file={self.co_filename!r}, "
                f"line={self.co_firstlineno}, "
                f"args={self.co_argcount}, "
                f"locals={self.co_nlocals}, "
                f"stack={self.co_stacksize}, "
                f"flags={self.co_flags:#x})")


@dataclass
class ModuleInfo:
    """Information about a decompiled module."""

    source_path: str           # original .py path (reconstructed)
    pyc_path: str              # .pyc file path
    python_version: Tuple[int, int]  # e.g., (3, 7)
    magic_number: int
    code: CodeObjectInfo       # top-level module code object
    source_code: str = ""      # decompiled source
    errors: List[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (f"ModuleInfo(pyc={self.pyc_path!r}, "
                f"py={self.source_path!r}, "
                f"ver={self.python_version}, "
                f"errors={len(self.errors)})")


@dataclass
class DecompileResult:
    """Result of decompiling a project or file."""

    modules: List[ModuleInfo] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    files_processed: int = 0
    files_succeeded: int = 0
    files_failed: int = 0

    @property
    def success_rate(self) -> float:
        if self.files_processed == 0:
            return 0.0
        return self.files_succeeded / self.files_processed
