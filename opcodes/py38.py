"""Python 3.8 opcode table."""

from __future__ import annotations

# Opcode definitions for Python 3.8
# HAVE_ARGUMENT = 90

# name → opcode number
opname = {
    "POP_TOP": 1,
    "ROT_TWO": 2,
    "ROT_THREE": 3,
    "DUP_TOP": 4,
    "DUP_TOP_TWO": 5,
    "ROT_FOUR": 6,
    "NOP": 9,
    "UNARY_POSITIVE": 10,
    "UNARY_NEGATIVE": 11,
    "UNARY_NOT": 12,
    "UNARY_INVERT": 15,
    "BINARY_MATRIX_MULTIPLY": 16,
    "INPLACE_MATRIX_MULTIPLY": 17,
    "BINARY_POWER": 19,
    "BINARY_MULTIPLY": 20,
    "BINARY_MODULO": 22,
    "BINARY_ADD": 23,
    "BINARY_SUBTRACT": 24,
    "BINARY_SUBSCR": 25,
    "BINARY_FLOOR_DIVIDE": 26,
    "BINARY_TRUE_DIVIDE": 27,
    "INPLACE_FLOOR_DIVIDE": 28,
    "INPLACE_TRUE_DIVIDE": 29,
    "INPLACE_ADD": 55,
    "INPLACE_SUBTRACT": 56,
    "INPLACE_MULTIPLY": 57,
    "INPLACE_MODULO": 59,
    "STORE_SUBSCR": 60,
    "DELETE_SUBSCR": 61,
    "BINARY_LSHIFT": 62,
    "BINARY_RSHIFT": 63,
    "BINARY_AND": 64,
    "BINARY_XOR": 65,
    "BINARY_OR": 66,
    "INPLACE_POWER": 67,
    "GET_ITER": 68,
    "GET_YIELD_FROM_ITER": 69,
    "PRINT_EXPR": 70,
    "LOAD_BUILD_CLASS": 71,
    "YIELD_FROM": 72,
    "GET_AWAITABLE": 73,
    "LOAD_ASSERTION_ERROR": 74,
    "RETURN_GENERATOR": 75,
    "LIST_TO_TUPLE": 82,
    "RETURN_VALUE": 83,
    "IMPORT_STAR": 84,
    "SETUP_ANNOTATIONS": 85,
    "YIELD_VALUE": 86,
    "POP_BLOCK": 87,
    "END_FINALLY": 88,
    "POP_EXCEPT": 89,
    "HAVE_ARGUMENT": 90,
    "STORE_NAME": 90,
    "DELETE_NAME": 91,
    "UNPACK_SEQUENCE": 92,
    "FOR_ITER": 93,
    "UNPACK_EX": 94,
    "STORE_ATTR": 95,
    "DELETE_ATTR": 96,
    "STORE_GLOBAL": 97,
    "DELETE_GLOBAL": 98,
    "LOAD_CONST": 100,
    "LOAD_NAME": 101,
    "BUILD_TUPLE": 102,
    "BUILD_LIST": 103,
    "BUILD_SET": 104,
    "BUILD_MAP": 105,
    "LOAD_ATTR": 106,
    "COMPARE_OP": 107,
    "IMPORT_NAME": 108,
    "IMPORT_FROM": 109,
    "JUMP_FORWARD": 110,
    "JUMP_IF_FALSE_OR_POP": 111,
    "JUMP_IF_TRUE_OR_POP": 112,
    "JUMP_ABSOLUTE": 113,
    "POP_JUMP_IF_FALSE": 114,
    "POP_JUMP_IF_TRUE": 115,
    "LOAD_GLOBAL": 116,
    "IS_OP": 117,
    "CONTAINS_OP": 118,
    "RERAISE": 119,
    "RAISE_VARARGS": 130,
    "CALL_FUNCTION": 131,
    "MAKE_FUNCTION": 132,
    "BUILD_SLICE": 133,
    "LOAD_CLOSURE": 135,
    "LOAD_DEREF": 136,
    "STORE_DEREF": 137,
    "DELETE_DEREF": 138,
    "CALL_FUNCTION_KW": 141,
    "CALL_FUNCTION_EX": 142,
    "SETUP_WITH": 143,
    "EXTENDED_ARG": 144,
    "LIST_APPEND": 145,
    "SET_ADD": 146,
    "MAP_ADD": 147,
    "LOAD_CLASSDEREF": 148,
    "BUILD_LIST_UNPACK": 149,
    "BUILD_MAP_UNPACK": 150,
    "BUILD_MAP_UNPACK_WITH_CALL": 151,
    "BUILD_TUPLE_UNPACK": 152,
    "BUILD_SET_UNPACK": 153,
    "SETUP_ASYNC_WITH": 154,
    "FORMAT_VALUE": 155,
    "BUILD_CONST_KEY_MAP": 156,
    "BUILD_STRING": 157,
    "BUILD_TUPLE_UNPACK_WITH_CALL": 158,
    "LOAD_METHOD": 160,
    "CALL_METHOD": 161,
    "CALL_FINALLY": 162,
    "POP_FINALLY": 163,
    "BREAK_LOOP": 80,
    "SETUP_FINALLY": 122,
    "SETUP_EXCEPT": 121,
    "BEGIN_FINALLY": 53,
    "WITH_CLEANUP_START": 81,
    "WITH_CLEANUP_FINISH": 171,
}

# opcode number → name
opcode = {v: k for k, v in opname.items()}

# Opcodes >= HAVE_ARGUMENT take a 2-byte argument
HAVE_ARGUMENT = 90

# Set of opcodes that take an argument
has_arg = {op for op in opcode if op >= HAVE_ARGUMENT}

# Jump opcodes (relative)
JUMP_RELATIVE = {
    opname["JUMP_FORWARD"],
    opname["SETUP_FINALLY"],
    opname["SETUP_EXCEPT"],
    opname["SETUP_WITH"],
    opname["SETUP_ASYNC_WITH"],
    opname["FOR_ITER"],
}

# Jump opcodes (absolute)
JUMP_ABSOLUTE_SET = {
    opname["JUMP_ABSOLUTE"],
    opname["POP_JUMP_IF_FALSE"],
    opname["POP_JUMP_IF_TRUE"],
    opname["JUMP_IF_FALSE_OR_POP"],
    opname["JUMP_IF_TRUE_OR_POP"],
}

# Conditional jump opcodes
JUMP_CONDITIONAL = {
    opname["POP_JUMP_IF_FALSE"],
    opname["POP_JUMP_IF_TRUE"],
    opname["JUMP_IF_FALSE_OR_POP"],
    opname["JUMP_IF_TRUE_OR_POP"],
    opname["FOR_ITER"],
    opname["SETUP_FINALLY"],
    opname["SETUP_EXCEPT"],
    opname["SETUP_WITH"],
    opname["SETUP_ASYNC_WITH"],
}

# All jump opcodes
JUMP_OPS = JUMP_RELATIVE | JUMP_ABSOLUTE_SET | {opname["BREAK_LOOP"]}

# Opcodes that always terminate the block
TERMINATOR_OPS = {
    opname["RETURN_VALUE"],
    opname["RAISE_VARARGS"],
    opname["RERAISE"],
    opname["BREAK_LOOP"],
}


def compute_target_offset(opcode_num: int, offset: int, arg: int) -> int:
    """Compute the absolute target offset for a jump instruction."""
    if opcode_num in JUMP_RELATIVE:
        return offset + 2 + arg
    elif opcode_num in JUMP_ABSOLUTE_SET:
        return arg
    return -1


def get_stack_effect(opcode_num: int, arg: int = 0) -> tuple:
    """Approximate stack effect: (pops, pushes)."""
    name = opcode.get(opcode_num, f"<{opcode_num}>")

    # Load ops
    if name in {"LOAD_CONST", "LOAD_FAST", "LOAD_NAME", "LOAD_GLOBAL",
                "LOAD_DEREF", "LOAD_CLOSURE", "LOAD_CLASSDEREF",
                "LOAD_ATTR", "LOAD_METHOD", "LOAD_BUILD_CLASS",
                "LOAD_ASSERTION_ERROR", "GET_ITER", "GET_YIELD_FROM_ITER",
                "GET_AWAITABLE"}:
        return (0, 1)

    # Store ops
    if name in {"STORE_FAST", "STORE_NAME", "STORE_GLOBAL", "STORE_DEREF",
                "STORE_ATTR", "POP_TOP", "DELETE_FAST", "DELETE_NAME",
                "DELETE_GLOBAL", "DELETE_DEREF", "DELETE_ATTR",
                "PRINT_EXPR"}:
        return (1, 0)

    if name == "STORE_SUBSCR":
        return (3, 0)
    if name == "DELETE_SUBSCR":
        return (2, 0)

    # Binary ops
    if name.startswith("BINARY_") and name != "BINARY_SUBSCR":
        return (2, 1)
    if name == "BINARY_SUBSCR":
        return (2, 1)

    # In-place ops
    if name.startswith("INPLACE_"):
        return (2, 1)

    # Unary
    if name.startswith("UNARY_"):
        return (1, 1)

    # Compare
    if name == "COMPARE_OP":
        return (2, 1)
    if name in {"IS_OP", "CONTAINS_OP"}:
        return (2, 1)

    # Call
    if name in {"CALL_FUNCTION", "CALL_METHOD"}:
        return (arg + 1, 1)
    if name == "CALL_FUNCTION_KW":
        return (arg + 2, 1)
    if name == "CALL_FUNCTION_EX":
        if arg & 1:
            return (3, 1)
        return (2, 1)

    # Build containers
    if name in {"BUILD_TUPLE", "BUILD_LIST", "BUILD_SET", "BUILD_MAP"}:
        return (arg, 1)
    if name == "BUILD_CONST_KEY_MAP":
        return (arg + 1, 1)
    if name == "BUILD_STRING":
        return (arg, 1)
    if name == "BUILD_SLICE":
        if arg == 3:
            return (3, 1)
        return (2, 1)

    # Unpack
    if name in {"UNPACK_SEQUENCE", "UNPACK_EX"}:
        return (1, arg)

    # Make function
    if name in {"MAKE_FUNCTION", "MAKE_CLOSURE"}:
        return (arg + 1, 1)

    # Import
    if name == "IMPORT_NAME":
        return (2, 1)
    if name == "IMPORT_FROM":
        return (0, 1)
    if name == "IMPORT_STAR":
        return (0, 0)

    # Jumps
    if opcode_num in JUMP_OPS:
        return (0, 0)
    if name in {"POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE"}:
        return (1, 0)
    if name in {"JUMP_IF_FALSE_OR_POP", "JUMP_IF_TRUE_OR_POP"}:
        return (1, 0)

    # Return/raise
    if name == "RETURN_VALUE":
        return (1, 0)
    if name == "RAISE_VARARGS":
        return (arg, 0)
    if name == "RERAISE":
        return (1, 0)

    # Yield
    if name == "YIELD_VALUE":
        return (1, 1)
    if name == "YIELD_FROM":
        return (1, 1)

    # Container append
    if name in {"LIST_APPEND", "SET_ADD", "MAP_ADD"}:
        return (1, 0)

    # Setup
    if name in {"SETUP_FINALLY", "SETUP_EXCEPT", "SETUP_WITH",
                "SETUP_ASYNC_WITH"}:
        return (0, 0)

    if name == "FORMAT_VALUE":
        pops = 1
        if arg & 0x04:
            pops += 1
        return (pops, 1)

    # DUP
    if name == "DUP_TOP":
        return (0, 1)
    if name == "DUP_TOP_TWO":
        return (0, 2)

    # ROT
    if name in {"ROT_TWO", "ROT_THREE", "ROT_FOUR"}:
        return (0, 0)

    # Default
    return (0, 0)
