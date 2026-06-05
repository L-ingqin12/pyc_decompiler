"""Bytecode disassembler: convert raw bytecode to structured Instructions.

Handles wordcode format (Python 3.6+) where each instruction is 2 bytes:
  byte 0: opcode
  byte 1: argument (0-255)

EXTENDED_ARG allows 16/24/32-bit arguments by prefixing the actual
instruction.
"""

from __future__ import annotations

from typing import Any, List, Set

from .types import Instruction, CodeObjectInfo


class DisassembleError(Exception):
    """Raised when bytecode cannot be disassembled."""


def _decode_lnotab(lnotab: bytes, first_lineno: int) -> dict:
    """Parse the line number table.

    lnotab format (pre-3.10): pairs of (byte_offset_delta, line_delta).
    Returns a dict mapping bytecode offset → line number.
    """
    lineno_map: dict = {}
    if not lnotab:
        return lineno_map

    offset = 0
    lineno = first_lineno
    it = iter(lnotab)
    for b_off, l_off in zip(it, it):
        offset += b_off
        lineno += l_off
        lineno_map[offset] = lineno

    return lineno_map


def _get_jump_targets(instructions: List[Instruction], ops_mod: Any) -> Set[int]:
    """Compute all offsets that are jump/branch targets."""
    targets: Set[int] = set()
    for instr in instructions:
        target = instr.target_offset
        if target is not None and target >= 0:
            targets.add(target)

    # Also add exception handler targets from SETUP_FINALLY/SETUP_EXCEPT
    for instr in instructions:
        if instr.opname in {"SETUP_FINALLY", "SETUP_EXCEPT",
                            "SETUP_WITH", "SETUP_ASYNC_WITH"}:
            target = instr.target_offset
            if target is not None and target >= 0:
                targets.add(target)

    return targets


def _resolve_arguments(instrs: List[Instruction], info: CodeObjectInfo, ops_mod: Any) -> None:
    """Resolve instruction arguments to meaningful values.

    Converts raw arg integers to:
      - Variable names (from co_varnames, co_names, co_cellvars, co_freevars)
      - Constant values (from co_consts)
      - Comparison operator strings
    """
    for instr in instrs:
        opname = instr.opname
        arg = instr.arg

        # Normalize specialized opcode names to generic categories
        generic = _normalize_opname(opname, ops_mod)

        if generic in {"LOAD_FAST", "STORE_FAST", "DELETE_FAST"}:
            if arg < len(info.co_varnames):
                instr.argval = info.co_varnames[arg]

        elif generic in {"LOAD_NAME", "STORE_NAME", "DELETE_NAME",
                         "LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL",
                         "LOAD_ATTR", "STORE_ATTR", "DELETE_ATTR",
                         "LOAD_METHOD", "LOAD_SUPER_ATTR"}:
            # Python 3.12 encodes name index as arg >> 1 for some ops
            actual_arg = _get_name_index(arg, opname, ops_mod)
            if actual_arg < len(info.co_names):
                instr.argval = info.co_names[actual_arg]

        elif generic in {"LOAD_DEREF", "STORE_DEREF", "DELETE_DEREF",
                         "LOAD_CLOSURE", "LOAD_CLASSDEREF"}:
            total_cell = len(info.co_cellvars) + len(info.co_freevars)
            if arg < total_cell:
                if arg < len(info.co_cellvars):
                    instr.argval = info.co_cellvars[arg]
                else:
                    instr.argval = info.co_freevars[arg - len(info.co_cellvars)]

        elif generic == "LOAD_CONST":
            if arg < len(info.co_consts):
                instr.argval = info.co_consts[arg]

        elif generic == "IMPORT_NAME":
            if arg < len(info.co_names):
                instr.argval = info.co_names[arg]

        elif generic == "IMPORT_FROM":
            if arg < len(info.co_names):
                instr.argval = info.co_names[arg]

        elif generic == "COMPARE_OP":
            from .opcodes.base import CMP_OP
            instr.argval = CMP_OP.get(arg, f"<cmp:{arg}>")

        elif generic in {"BINARY_OP", "UNARY_OP"}:
            pass


def _normalize_opname(opname: str, ops_mod: Any) -> str:
    """Map specialized opcode names to their generic category.

    E.g., LOAD_ATTR_SLOT → LOAD_ATTR, CALL_PY_EXACT_ARGS → CALL.
    """
    # Check the ops module's SPECIALIZED_MAP if available
    spec_map = getattr(ops_mod, '_SPECIALIZED_MAP', {})
    for opnum, gen_name in spec_map.items():
        spec_opnum = ops_mod.opname.get(opname)
        if spec_opnum == opnum:
            return gen_name

    # Fallback: strip known prefixes to get base opcode category
    bases = {
        "LOAD_ATTR_": "LOAD_ATTR", "STORE_ATTR_": "STORE_ATTR",
        "LOAD_GLOBAL_": "LOAD_GLOBAL", "CALL_": "CALL",
        "FOR_ITER_": "FOR_ITER", "BINARY_OP_": "BINARY_OP",
        "COMPARE_OP_": "COMPARE_OP", "BINARY_SUBSCR_": "BINARY_SUBSCR",
        "STORE_SUBSCR_": "STORE_SUBSCR", "UNPACK_SEQUENCE_": "UNPACK_SEQUENCE",
        "LOAD_SUPER_ATTR_": "LOAD_SUPER_ATTR",
        "STORE_FAST__": "STORE_FAST", "LOAD_FAST__": "LOAD_FAST",
        "LOAD_CONST__": "LOAD_CONST", "SEND_": "SEND",
    }
    for prefix, base in bases.items():
        if opname.startswith(prefix):
            return base
    return opname


def _get_name_index(arg: int, opname: str, ops_mod: Any) -> int:
    """Get the actual name index from an opcode argument.

    In Python 3.12+, ops with inline caches encode the name index
    as `(index << 1) | flag` where the low bit is a type flag.
    """
    if not hasattr(ops_mod, '_SPECIALIZED_MAP'):
        return arg
    shifted_ops = {"LOAD_ATTR", "STORE_ATTR", "DELETE_ATTR",
                   "LOAD_GLOBAL", "STORE_GLOBAL",
                   "LOAD_METHOD", "LOAD_SUPER_ATTR"}
    generic = _normalize_opname(opname, ops_mod)
    if generic in shifted_ops:
        return arg >> 1
    return arg


def disassemble(info: CodeObjectInfo, ops_mod: Any) -> List[Instruction]:
    """Convert raw bytecode to a list of Instructions.

    Args:
        info: The code object info with raw bytecode.
        ops_mod: The opcode module for the Python version.

    Returns:
        List of Instruction objects, with EXTENDED_ARG resolved and
        arguments resolved to meaningful values.
    """
    bytecode = info.raw_bytecode
    opcode_map = ops_mod.opcode
    has_arg = ops_mod.has_arg

    if len(bytecode) % 2 != 0:
        raise DisassembleError(
            f"Bytecode length {len(bytecode)} is not even; "
            f"expected wordcode format (2 bytes per instruction)"
        )

    # First pass: decode instructions
    instructions: List[Instruction] = []
    lineno_map = _decode_lnotab(info.co_lnotab, info.co_firstlineno)
    extended_arg = 0

    for offset in range(0, len(bytecode), 2):
        op = bytecode[offset]
        arg = bytecode[offset + 1]
        opname = opcode_map.get(op, f"<{op}>")

        # Resolve EXTENDED_ARG
        if op == ops_mod.opname.get("EXTENDED_ARG", 144):
            extended_arg = (extended_arg << 8) | arg
            continue

        # Combine with extended arg
        full_arg = (extended_arg << 8) | arg if extended_arg else arg
        extended_arg = 0

        lineno = lineno_map.get(offset // 2, -1)

        instr = Instruction(
            offset=offset,
            opcode=op,
            opname=opname,
            arg=full_arg if op in has_arg else 0,
            lineno=lineno,
        )
        instructions.append(instr)

    # Second pass: mark jump targets
    targets = _get_jump_targets(instructions, ops_mod)
    for instr in instructions:
        if instr.offset in targets:
            instr.is_jump_target = True

    # Resolve arguments to meaningful values
    _resolve_arguments(instructions, info, ops_mod)

    return instructions


def disassemble_all(info: CodeObjectInfo, ops_mod: Any) -> None:
    """Disassemble a code object and all its nested code objects."""
    info.instructions = disassemble(info, ops_mod)
    for nested in info.nested:
        disassemble_all(nested, ops_mod)
