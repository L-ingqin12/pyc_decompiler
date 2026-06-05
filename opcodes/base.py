"""Base opcode definitions shared across Python versions."""

# Compare operations (argument to COMPARE_OP)
CMP_OP = {
    0: "<",
    1: "<=",
    2: "==",
    3: "!=",
    4: ">",
    5: ">=",
    6: "in",
    7: "not in",
    8: "is",
    9: "is not",
    10: "exception match",
    11: "BAD",
}

# Binary operations for pre-3.11 BINARY_* opcodes
BINARY_OPS = {
    "BINARY_POWER": "**",
    "BINARY_MULTIPLY": "*",
    "BINARY_MATRIX_MULTIPLY": "@",
    "BINARY_FLOOR_DIVIDE": "//",
    "BINARY_TRUE_DIVIDE": "/",
    "BINARY_MODULO": "%",
    "BINARY_ADD": "+",
    "BINARY_SUBTRACT": "-",
    "BINARY_SUBSCR": "[]",
    "BINARY_LSHIFT": "<<",
    "BINARY_RSHIFT": ">>",
    "BINARY_AND": "&",
    "BINARY_XOR": "^",
    "BINARY_OR": "|",
}

INPLACE_OPS = {
    "INPLACE_POWER": "**=",
    "INPLACE_MULTIPLY": "*=",
    "INPLACE_MATRIX_MULTIPLY": "@=",
    "INPLACE_FLOOR_DIVIDE": "//=",
    "INPLACE_TRUE_DIVIDE": "/=",
    "INPLACE_MODULO": "%=",
    "INPLACE_ADD": "+=",
    "INPLACE_SUBTRACT": "-=",
    "INPLACE_LSHIFT": "<<=",
    "INPLACE_RSHIFT": ">>=",
    "INPLACE_AND": "&=",
    "INPLACE_XOR": "^=",
    "INPLACE_OR": "|=",
}

UNARY_OPS = {
    "UNARY_POSITIVE": "+",
    "UNARY_NEGATIVE": "-",
    "UNARY_NOT": "not",
    "UNARY_INVERT": "~",
}

# Boolean AND/OR detection: these opcode sequences form short-circuit
# 'and'/'or' expressions used by Python 3.7/3.8
# JUMP_IF_FALSE_OR_POP (and pattern): if false, jump to short-circuit; otherwise pop False
# JUMP_IF_TRUE_OR_POP (or pattern): if true, jump to short-circuit; otherwise pop True

# Stack rotation opcodes
ROTATION_OPS = {
    "ROT_TWO",
    "ROT_THREE",
    "ROT_FOUR",
    "DUP_TOP",
    "DUP_TOP_TWO",
}

# Opcodes that reference constants
CONST_OPS = {
    "LOAD_CONST",
}

# Opcodes that reference variable names
NAME_OPS = {
    "LOAD_NAME", "STORE_NAME", "DELETE_NAME",
    "LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL",
    "LOAD_FAST", "STORE_FAST", "DELETE_FAST",
    "LOAD_DEREF", "STORE_DEREF", "DELETE_DEREF",
    "LOAD_CLOSURE", "LOAD_CLASSDEREF",
    "LOAD_ATTR", "STORE_ATTR", "DELETE_ATTR",
}

# Opcodes that import
IMPORT_OPS = {
    "IMPORT_NAME", "IMPORT_FROM", "IMPORT_STAR",
}

# Opcodes that create functions/classes
BUILD_OPS = {
    "MAKE_FUNCTION", "MAKE_CLOSURE",
}

# Opcodes for building containers
CONTAINER_BUILD_OPS = {
    "BUILD_TUPLE", "BUILD_LIST", "BUILD_SET", "BUILD_MAP",
    "BUILD_TUPLE_UNPACK", "BUILD_LIST_UNPACK", "BUILD_SET_UNPACK",
    "BUILD_MAP_UNPACK", "BUILD_TUPLE_UNPACK_WITH_CALL",
    "BUILD_LIST_UNPACK_WITH_CALL",
    "BUILD_CONST_KEY_MAP", "BUILD_STRING", "BUILD_SLICE",
}

# Python 3.7/3.8 exception handling opcodes
EXCEPTION_OPS = {
    "POP_EXCEPT", "POP_BLOCK", "POP_FINALLY",
    "END_FINALLY", "BEGIN_FINALLY",
    "SETUP_FINALLY", "SETUP_EXCEPT", "SETUP_WITH",
    "RAISE_VARARGS", "RERAISE",
    "WITH_CLEANUP_START", "WITH_CLEANUP_FINISH",
    "LOAD_ASSERTION_ERROR",
}
