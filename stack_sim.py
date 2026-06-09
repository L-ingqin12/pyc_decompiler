"""Symbolic stack simulation for expression recovery.

Walks bytecode instructions while maintaining a symbolic stack of AST
expression nodes. At each instruction, pops operands from the stack and
pushes the result back, building up expression trees.

For control flow merges, reconciles stack states from multiple incoming
paths.
"""

from __future__ import annotations

import ast
from typing import Any, Dict, List, Optional, Tuple

from .pyc_types import Instruction, BasicBlock, CodeObjectInfo
from .opcodes.base import (
    BINARY_OPS, INPLACE_OPS, UNARY_OPS, CMP_OP,
)

# Python 3.7/3.8 binary operator mapping (opname → ast operator class)
_BINOP_MAP = {
    "BINARY_POWER": ast.Pow,
    "BINARY_MULTIPLY": ast.Mult,
    "BINARY_MATRIX_MULTIPLY": ast.MatMult,
    "BINARY_FLOOR_DIVIDE": ast.FloorDiv,
    "BINARY_TRUE_DIVIDE": ast.Div,
    "BINARY_MODULO": ast.Mod,
    "BINARY_ADD": ast.Add,
    "BINARY_SUBTRACT": ast.Sub,
    "BINARY_SUBSCR": None,  # special: Subscript
    "BINARY_LSHIFT": ast.LShift,
    "BINARY_RSHIFT": ast.RShift,
    "BINARY_AND": ast.BitAnd,
    "BINARY_XOR": ast.BitXor,
    "BINARY_OR": ast.BitOr,
}

_INPLACE_OP_MAP = {
    "INPLACE_POWER": ast.Pow,
    "INPLACE_MULTIPLY": ast.Mult,
    "INPLACE_MATRIX_MULTIPLY": ast.MatMult,
    "INPLACE_FLOOR_DIVIDE": ast.FloorDiv,
    "INPLACE_TRUE_DIVIDE": ast.Div,
    "INPLACE_MODULO": ast.Mod,
    "INPLACE_ADD": ast.Add,
    "INPLACE_SUBTRACT": ast.Sub,
    "INPLACE_LSHIFT": ast.LShift,
    "INPLACE_RSHIFT": ast.RShift,
    "INPLACE_AND": ast.BitAnd,
    "INPLACE_XOR": ast.BitXor,
    "INPLACE_OR": ast.BitOr,
}

_UNARY_OP_MAP = {
    "UNARY_POSITIVE": ast.UAdd,
    "UNARY_NEGATIVE": ast.USub,
    "UNARY_NOT": ast.Not,
    "UNARY_INVERT": ast.Invert,
}

_CMP_AST_MAP = {
    "<": ast.Lt,
    "<=": ast.LtE,
    "==": ast.Eq,
    "!=": ast.NotEq,
    ">": ast.Gt,
    ">=": ast.GtE,
    "in": ast.In,
    "not in": ast.NotIn,
    "is": ast.Is,
    "is not": ast.IsNot,
}


def _name_node(name: str, ctx=ast.Load) -> ast.Name:
    """Create a Name AST node."""
    return ast.Name(id=name, ctx=ctx())


def _const_node(val: Any) -> ast.AST:
    """Create a Constant AST node, handling special cases."""
    if val is None:
        return ast.Constant(value=None)
    if isinstance(val, bool):
        return ast.Constant(value=val)
    if isinstance(val, (int, float, complex)):
        return ast.Constant(value=val)
    if isinstance(val, str):
        return ast.Constant(value=val)
    if isinstance(val, bytes):
        return ast.Constant(value=val)
    if isinstance(val, tuple):
        return ast.Tuple(
            elts=[_const_node(v) for v in val],
            ctx=ast.Load(),
        )
    if isinstance(val, frozenset):
        return ast.Constant(value=val)
    if isinstance(val, type(...)):
        return ast.Constant(value=...)
    # Fallback for other types
    return ast.Constant(value=val)


def _attr_node(obj: ast.AST, attr: str, ctx=ast.Load) -> ast.Attribute:
    """Create an Attribute access AST node."""
    return ast.Attribute(value=obj, attr=attr, ctx=ctx())


class StackSimulator:
    """Symbolic stack simulator for a single basic block path."""

    def __init__(self, info: CodeObjectInfo):
        self.info = info
        self.stack: List[ast.AST] = []
        self._block_stack: List[Any] = []  # for SETUP_FINALLY/SETUP_EXCEPT tracking

    def copy(self) -> StackSimulator:
        """Deep copy the current simulator state."""
        import copy
        new = StackSimulator(self.info)
        new.stack = copy.deepcopy(self.stack)
        new._block_stack = list(self._block_stack)
        return new

    def push(self, node: ast.AST) -> None:
        self.stack.append(node)

    def pop(self) -> ast.AST:
        if not self.stack:
            return ast.Constant(value=None)
        return self.stack.pop()

    def peek(self, n: int = 0) -> Optional[ast.AST]:
        idx = len(self.stack) - 1 - n
        if idx < 0:
            return None
        return self.stack[idx]

    def dup_top(self) -> None:
        if self.stack:
            import copy
            self.stack.append(copy.deepcopy(self.stack[-1]))

    def rot_two(self) -> None:
        if len(self.stack) >= 2:
            self.stack[-1], self.stack[-2] = self.stack[-2], self.stack[-1]

    def rot_three(self) -> None:
        if len(self.stack) >= 3:
            self.stack[-1], self.stack[-2], self.stack[-3] = \
                self.stack[-2], self.stack[-3], self.stack[-1]

    def process_instruction(self, instr: Instruction) -> List[ast.AST]:
        """Process a single instruction, updating the symbolic stack.

        Returns:
            List of AST statement nodes generated (usually empty for
            expression instructions; populated for statements like
            assignments, returns, etc.).
        """
        stmts: List[ast.AST] = []
        opname = instr.opname
        arg = instr.arg
        argval = instr.argval

        # --- Load operations ---
        if opname == "LOAD_CONST":
            self.push(_const_node(argval))

        elif opname in {"LOAD_FAST", "LOAD_NAME", "LOAD_GLOBAL", "LOAD_DEREF"}:
            name = argval or f"<var_{arg}>"
            self.push(_name_node(name))

        elif opname == "LOAD_ATTR":
            obj = self.pop()
            name = argval or f"<attr_{arg}>"
            self.push(_attr_node(obj, name))

        elif opname == "LOAD_METHOD":
            obj = self.peek()  # TOS is the object
            name = argval or f"<method_{arg}>"
            # Push unbound method (attribute of obj)
            self.push(_attr_node(obj, name))

        elif opname == "LOAD_CLOSURE":
            name = argval or f"<closure_{arg}>"
            self.push(_name_node(name))

        elif opname == "LOAD_CLASSDEREF":
            name = argval or f"<classderef_{arg}>"
            self.push(_name_node(name))

        elif opname == "LOAD_BUILD_CLASS":
            self.push(_name_node("__build_class__"))

        elif opname == "LOAD_ASSERTION_ERROR":
            self.push(_name_node("AssertionError"))

        # --- Store operations ---
        elif opname == "STORE_FAST":
            val = self.pop()
            name = argval or f"<var_{arg}>"
            stmts.append(ast.Assign(
                targets=[_name_node(name, ctx=ast.Store)],
                value=val,
            ))

        elif opname == "STORE_NAME":
            val = self.pop()
            name = argval or f"<name_{arg}>"
            stmts.append(ast.Assign(
                targets=[_name_node(name, ctx=ast.Store)],
                value=val,
            ))

        elif opname == "STORE_GLOBAL":
            val = self.pop()
            name = argval or f"<global_{arg}>"
            stmts.append(ast.Assign(
                targets=[_name_node(name, ctx=ast.Store)],
                value=val,
            ))

        elif opname == "STORE_ATTR":
            obj = self.pop()
            val = self.pop()
            name = argval or f"<attr_{arg}>"
            stmts.append(ast.Assign(
                targets=[_attr_node(obj, name, ctx=ast.Store)],
                value=val,
            ))

        elif opname == "STORE_DEREF":
            val = self.pop()
            name = argval or f"<deref_{arg}>"
            stmts.append(ast.Assign(
                targets=[_name_node(name, ctx=ast.Store)],
                value=val,
            ))

        elif opname == "STORE_SUBSCR":
            # Stack: [value, container, key] (TOS=key)
            key = self.pop()
            obj = self.pop()
            val = self.pop()
            stmts.append(ast.Assign(
                targets=[ast.Subscript(
                    value=obj,
                    slice=key,
                    ctx=ast.Store(),
                )],
                value=val,
            ))

        # --- Stack manipulation ---
        elif opname == "POP_TOP":
            self.pop()  # discard; may represent expression statement

        elif opname == "DUP_TOP":
            self.dup_top()

        elif opname == "DUP_TOP_TWO":
            if len(self.stack) >= 2:
                import copy
                self.stack.append(copy.deepcopy(self.stack[-2]))
                self.stack.append(copy.deepcopy(self.stack[-1]))

        elif opname == "ROT_TWO":
            self.rot_two()

        elif opname == "ROT_THREE":
            self.rot_three()

        elif opname == "ROT_FOUR":
            if len(self.stack) >= 4:
                self.stack[-1], self.stack[-2], self.stack[-3], self.stack[-4] = \
                    self.stack[-2], self.stack[-3], self.stack[-4], self.stack[-1]

        # --- Binary operators ---
        elif opname in _BINOP_MAP:
            right = self.pop()
            left = self.pop()
            if opname == "BINARY_SUBSCR":
                self.push(ast.Subscript(
                    value=left,
                    slice=right,
                    ctx=ast.Load(),
                ))
            else:
                op_class = _BINOP_MAP[opname]
                self.push(ast.BinOp(left=left, op=op_class(), right=right))

        # --- In-place operators ---
        elif opname in _INPLACE_OP_MAP:
            right = self.pop()
            left = self.pop()
            op_class = _INPLACE_OP_MAP[opname]
            # Push a BinOp result (e.g. self.used + elapsed).
            # The subsequent STORE_* instruction will create the assignment.
            # This produces `self.x = self.x + y` instead of `self.x += y`,
            # which is semantically equivalent.
            self.push(ast.BinOp(left=left, op=op_class(), right=right))

        # --- Unary operators ---
        elif opname in _UNARY_OP_MAP:
            operand = self.pop()
            op_class = _UNARY_OP_MAP[opname]
            self.push(ast.UnaryOp(op=op_class(), operand=operand))

        # --- Comparison operators ---
        elif opname == "COMPARE_OP":
            right = self.pop()
            left = self.pop()
            cmp_str = argval or "<"
            cmp_class = _CMP_AST_MAP.get(cmp_str)
            if cmp_class:
                self.push(ast.Compare(
                    left=left,
                    ops=[cmp_class()],
                    comparators=[right],
                ))
            else:
                self.push(ast.Compare(
                    left=left,
                    ops=[ast.Eq()],
                    comparators=[right],
                ))

        elif opname == "IS_OP":
            right = self.pop()
            left = self.pop()
            if arg:
                self.push(ast.Compare(
                    left=left, ops=[ast.IsNot()], comparators=[right]))
            else:
                self.push(ast.Compare(
                    left=left, ops=[ast.Is()], comparators=[right]))

        elif opname == "CONTAINS_OP":
            right = self.pop()
            left = self.pop()
            if arg:
                self.push(ast.Compare(
                    left=left, ops=[ast.NotIn()], comparators=[right]))
            else:
                self.push(ast.Compare(
                    left=left, ops=[ast.In()], comparators=[right]))

        # --- Function/method calls ---
        elif opname in {"CALL_FUNCTION", "CALL_METHOD", "CALL"}:
            nargs = arg
            args = []
            for _ in range(nargs):
                args.insert(0, self.pop())

            # Skip PUSH_NULL marker if present (Python 3.11+)
            func_or_self = self.pop()
            if isinstance(func_or_self, ast.Constant) and func_or_self.value is None:
                # This was a NULL from PUSH_NULL - the real function is below
                func = self.pop() if self.stack else ast.Constant(value=None)
            else:
                func = func_or_self

            # For CALL_METHOD: LOAD_METHOD left [self, method] on stack.
            # After popping args and method, the implicit self remains.
            if opname == "CALL_METHOD":
                if self.stack:
                    self.pop()  # discard implicit self

            # Check for KW_NAMES - stored in _pending_kw_names
            keywords = []
            kw_names = getattr(self, '_pending_kw_names', None)
            if kw_names:
                actual_args = []
                kw_values = args[-len(kw_names):] if len(kw_names) <= len(args) else []
                pos_args = args[:-len(kw_names)] if len(kw_names) <= len(args) else args
                for i, kwname in enumerate(kw_names):
                    if i < len(kw_values):
                        keywords.append(ast.keyword(arg=kwname, value=kw_values[i]))
                args = pos_args
                self._pending_kw_names = None

            self.push(ast.Call(func=func, args=args, keywords=keywords))

        elif opname == "KW_NAMES":
            # Store keyword names for the next CALL
            if isinstance(argval, tuple):
                self._pending_kw_names = list(argval)
            else:
                self._pending_kw_names = []

        elif opname == "PUSH_NULL":
            # PUSH_NULL marks function vs method calls in 3.11+
            self.push(ast.Constant(value=None))

        elif opname == "CALL_FUNCTION_KW":
            nargs = arg
            kw_names_const = self.pop()  # tuple of keyword names
            kw_names = kw_names_const.value if isinstance(kw_names_const, ast.Constant) else []
            args = []
            keywords = []
            total = nargs + len(kw_names) if isinstance(kw_names, tuple) else nargs
            for _ in range(total):
                args.insert(0, self.pop())
            if isinstance(kw_names, tuple):
                kw_arg_count = len(kw_names)
                pos_arg_count = nargs - kw_arg_count
                posargs = args[:pos_arg_count] if pos_arg_count > 0 else []
                kwvalues = args[pos_arg_count:] if pos_arg_count < len(args) else []
                for i, kwname in enumerate(kw_names):
                    keywords.append(ast.keyword(arg=kwname, value=kwvalues[i]))
                args = posargs
            func = self.pop()
            self.push(ast.Call(func=func, args=args, keywords=keywords))

        elif opname == "CALL_FUNCTION_EX":
            if arg & 1:  # has **kwargs
                kwargs = self.pop()
                args = self.pop()
            else:
                kwargs = None
                args = self.pop()
            func = self.pop()
            call_args = [args]
            call_kwargs = []
            if isinstance(args, ast.Starred):
                pass  # already starred
            else:
                # If args is a tuple/list, expand
                pass
            self.push(ast.Call(
                func=func,
                args=[ast.Starred(value=args)] if args else [],
                keywords=[ast.keyword(arg=None, value=kwargs)] if kwargs else [],
            ))

        # --- Build operations ---
        elif opname == "BUILD_TUPLE":
            items = []
            for _ in range(arg):
                items.insert(0, self.pop())
            self.push(ast.Tuple(elts=items, ctx=ast.Load()))

        elif opname == "BUILD_LIST":
            items = []
            for _ in range(arg):
                items.insert(0, self.pop())
            self.push(ast.List(elts=items, ctx=ast.Load()))

        elif opname == "BUILD_SET":
            items = []
            for _ in range(arg):
                items.insert(0, self.pop())
            self.push(ast.Set(elts=items))

        elif opname == "BUILD_MAP":
            keys = []
            values = []
            for _ in range(arg):
                v = self.pop()
                k = self.pop()
                values.insert(0, v)
                keys.insert(0, k)
            self.push(ast.Dict(keys=keys, values=values))

        elif opname == "BUILD_CONST_KEY_MAP":
            keys_node = self.pop()
            n = arg
            values = []
            for _ in range(n):
                values.insert(0, self.pop())
            # keys_node can be ast.Tuple (from _const_node) or ast.Constant(tuple)
            if isinstance(keys_node, ast.Tuple):
                keys = list(keys_node.elts)
            elif isinstance(keys_node, ast.Constant) and isinstance(keys_node.value, tuple):
                keys = [_const_node(k) for k in keys_node.value]
            else:
                keys = [_const_node(None)] * n
            self.push(ast.Dict(keys=keys, values=values))

        elif opname == "BUILD_STRING":
            n = arg
            parts = []
            for _ in range(n):
                parts.insert(0, self.pop())
            if n == 1:
                self.push(parts[0])
            elif any(isinstance(p, ast.FormattedValue) for p in parts):
                # f-string: create a JoinedStr node
                values = []
                for p in parts:
                    if isinstance(p, ast.FormattedValue):
                        values.append(p)
                    elif isinstance(p, ast.Constant) and isinstance(p.value, str):
                        values.append(p)
                    else:
                        # Convert non-string expression to FormattedValue
                        values.append(ast.FormattedValue(value=p, conversion=-1))
                self.push(ast.JoinedStr(values=values))
            else:
                # Plain string concatenation
                result = parts[0]
                for p in parts[1:]:
                    result = ast.BinOp(left=result, op=ast.Add(), right=p)
                self.push(result)

        elif opname == "BUILD_SLICE":
            if arg == 3:
                step = self.pop()
                stop = self.pop()
                start = self.pop()
                self.push(ast.Slice(lower=start, upper=stop, step=step))
            elif arg == 2:
                stop = self.pop()
                start = self.pop()
                self.push(ast.Slice(lower=start, upper=stop))
            else:
                # arg == 0: just a slice marker, or unknown form
                pass

        elif opname == "BINARY_SLICE":
            # Python 3.12+: pops TOS(stop), TOS1(start), TOS2(container),
            # pushes container[start:stop] as a combined slice+subscript
            stop = self.pop()
            start = self.pop()
            container = self.pop()
            self.push(ast.Subscript(
                value=container,
                slice=ast.Slice(lower=start, upper=stop),
                ctx=ast.Load(),
            ))

        elif opname == "STORE_SLICE":
            # Python 3.12+: container[start:stop] = value
            # Stack: [value, container, start, stop] (TOS=stop)
            stop = self.pop()
            start = self.pop()
            container = self.pop()
            val = self.pop()
            stmts.append(ast.Assign(
                targets=[ast.Subscript(
                    value=container,
                    slice=ast.Slice(lower=start, upper=stop),
                    ctx=ast.Store(),
                )],
                value=val,
            ))

        # --- Unpacking ---
        elif opname == "UNPACK_SEQUENCE":
            seq = self.pop()
            n = arg
            # Produce n individual items (used by STORE_FAST sequence)
            for i in range(n):
                self.push(ast.Subscript(
                    value=seq,
                    slice=ast.Constant(value=i),
                    ctx=ast.Load(),
                ))

        elif opname == "UNPACK_EX":
            seq = self.pop()
            before = arg & 0xFF
            after = (arg >> 8) & 0xFF
            # Produce before items + starred middle + after items
            middle_start = before
            middle_end = None  # will be -after
            # Push after items
            for _ in range(after):
                pass  # approximate
            # Push starred
            self.push(ast.Starred(
                value=ast.Subscript(
                    value=seq,
                    slice=ast.Slice(
                        lower=ast.Constant(value=before),
                        upper=ast.UnaryOp(op=ast.USub(), operand=ast.Constant(value=after)) if after else None,
                    ),
                    ctx=ast.Load(),
                ),
                ctx=ast.Load(),
            ))
            # Push before items
            for i in reversed(range(before)):
                self.push(ast.Subscript(
                    value=seq,
                    slice=ast.Constant(value=i),
                    ctx=ast.Load(),
                ))

        # --- Imports ---
        elif opname == "IMPORT_NAME":
            fromlist = self.pop()
            level = self.pop()
            name = argval or f"<module_{arg}>"
            if isinstance(fromlist, ast.Constant) and fromlist.value is None:
                self.push(ast.Import(names=[ast.alias(name=name, asname=None)]))
            else:
                # from import
                pass

        elif opname == "IMPORT_FROM":
            name = argval or f"<name_{arg}>"
            self.push(ast.alias(name=name, asname=None))

        elif opname == "IMPORT_STAR":
            pass

        # --- Return ---
        elif opname == "RETURN_VALUE":
            val = self.pop() if self.stack else ast.Constant(value=None)
            stmts.append(ast.Return(value=val))

        # --- Raise ---
        elif opname == "RAISE_VARARGS":
            exc = None
            cause = None
            if arg >= 1:
                exc = self.pop()
            if arg >= 2:
                cause = self.pop()
            stmts.append(ast.Raise(exc=exc, cause=cause))

        elif opname == "RERAISE":
            stmts.append(ast.Raise(exc=None, cause=None))

        # --- Yield ---
        elif opname == "YIELD_VALUE":
            val = self.pop() if self.stack else ast.Constant(value=None)
            stmts.append(ast.Expr(value=ast.Yield(value=val)))

        elif opname == "YIELD_FROM":
            val = self.pop() if self.stack else ast.Constant(value=None)
            stmts.append(ast.Expr(value=ast.YieldFrom(value=val)))

        # --- Function/class creation ---
        elif opname in {"MAKE_FUNCTION", "MAKE_CLOSURE"}:
            flags = arg
            # TOS: qualified name (or code object for closures)
            # For MAKE_FUNCTION with defaults:
            # pop defaults tuple, pop code object, rebuild as FunctionDef
            # For now, push a placeholder
            qualname = self.pop()
            code_obj = self.pop() if self.stack else None
            self.push(ast.Constant(value=f"<function {qualname}>"))

        # --- List/Set/Map append ---
        elif opname == "LIST_APPEND":
            val = self.pop()
            # The list being built is deeper in the stack

        elif opname == "SET_ADD":
            val = self.pop()

        elif opname == "MAP_ADD":
            val = self.pop()
            key = self.pop()

        # --- Format ---
        elif opname == "FORMAT_VALUE":
            # Stack: [value, fmt_spec] (TOS=fmt_spec if arg & 0x04)
            conversion = arg & 0x03
            has_fmt = arg & 0x04

            fmt_spec = None
            if has_fmt:
                fmt_spec = self.pop()  # TOS = format spec
            val = self.pop()  # value to format

            if conversion == 1:
                val = ast.Call(func=_name_node("str"), args=[val], keywords=[])
            elif conversion == 2:
                val = ast.Call(func=_name_node("repr"), args=[val], keywords=[])
            elif conversion == 3:
                val = ast.Call(func=_name_node("ascii"), args=[val], keywords=[])
            if fmt_spec is not None:
                val = ast.FormattedValue(
                    value=val,
                    conversion=conversion,
                    format_spec=fmt_spec,
                )
            self.push(val)

        # --- Iteration ---
        elif opname == "GET_ITER":
            obj = self.pop()
            self.push(ast.Call(
                func=_name_node("iter"),
                args=[obj],
                keywords=[],
            ))

        elif opname == "GET_YIELD_FROM_ITER":
            pass  # internal

        elif opname == "GET_AWAITABLE":
            pass  # internal

        elif opname == "GET_AITER":
            pass  # internal (async iteration)

        elif opname == "GET_ANEXT":
            pass  # internal (async iteration)

        # --- Jump-like (handled by CFG, no stack effect here) ---
        elif opname in {"JUMP_FORWARD", "JUMP_ABSOLUTE", "JUMP_BACKWARD",
                        "FOR_ITER", "SETUP_FINALLY", "SETUP_EXCEPT",
                        "SETUP_WITH", "SETUP_ASYNC_WITH", "SETUP_LOOP",
                        "POP_BLOCK", "POP_EXCEPT", "END_FINALLY",
                        "BEGIN_FINALLY", "CALL_FINALLY", "POP_FINALLY",
                        "SETUP_ANNOTATIONS", "NOP", "EXTENDED_ARG",
                        "RETURN_GENERATOR", "PRINT_EXPR",
                        "LIST_TO_TUPLE", "WITH_CLEANUP_START",
                        "WITH_CLEANUP_FINISH"}:
            pass

        elif opname == "BREAK_LOOP":
            stmts.append(ast.Break())

        elif opname == "CONTINUE_LOOP":
            stmts.append(ast.Continue())

        # --- Conditional jumps with stack effect ---
        elif opname in {"POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE"}:
            # Pop condition value (consumed by the branch)
            if self.stack:
                self.pop()

        elif opname == "JUMP_IF_FALSE_OR_POP":
            # and: TOS is left operand. If false, jump (keep TOS).
            # If true (fallthrough), pop TOS and continue to evaluate right.
            # Simulate truthy fallthrough: pop left, compute right.
            if self.stack:
                self.pop()

        elif opname == "JUMP_IF_TRUE_OR_POP":
            # or: TOS is left operand. If true, jump (keep TOS).
            # If false (fallthrough), pop TOS and continue.
            # Simulate falsy fallthrough: pop left, compute right.
            if self.stack:
                self.pop()

        # --- Delete ---
        elif opname.startswith("DELETE_"):
            pass  # del statement (approximate)

        # --- Default: unknown opcode ---
        else:
            # Unknown opcode - push a placeholder comment
            pass

        return stmts


def simulate_block(
    sim: StackSimulator,
    block: BasicBlock,
) -> Tuple[List[ast.AST], List[ast.AST]]:
    """Run the stack simulator through a basic block.

    Args:
        sim: Current simulator state (modified in place).
        block: The basic block to simulate.

    Returns:
        (statements, final_stack): AST statements generated and final
        stack state.
    """
    stmts: List[ast.AST] = []
    for instr in block.instructions:
        s = sim.process_instruction(instr)
        stmts.extend(s)
    return stmts, list(sim.stack)


class BlockStackSimulator:
    """Manages stack state across basic blocks using CFG structure.

    The existing StackSimulator correctly handles per-instruction
    expression recovery within a single block. This class manages
    state propagation between blocks: at each block entry, the stack
    state is inherited from the immediate dominator; at join points
    (blocks with multiple predecessors), the canonical state comes
    from the dominator path. This avoids the single-path simulation
    issues that cause variable assignments and intermediate expressions
    to be lost across control-flow boundaries.
    """

    def __init__(
        self,
        info: CodeObjectInfo,
        blocks: List[BasicBlock],
        idom: Optional[Dict[int, Optional[int]]] = None,
    ):
        self.info = info
        self.blocks = blocks
        self.idom = idom or {}
        # Per-block cached entry/exit states
        self._entry_states: Dict[int, Optional[List[ast.AST]]] = {}
        self._exit_states: Dict[int, Optional[List[ast.AST]]] = {}
        self._stmts: Dict[int, List[ast.stmt]] = {}

    def simulate_all(self) -> List[ast.stmt]:
        """Simulate all blocks and return combined statement list."""
        if not self.blocks:
            return []

        # Order blocks for simulation: dominator-tree preorder
        ordered = self._order_blocks()

        for block in ordered:
            entry = self._get_entry_state(block)
            sim = StackSimulator(self.info)
            sim.stack = list(entry)  # copy entry state
            stmts, exit_stack = simulate_block(sim, block)
            self._stmts[block.id] = stmts
            self._exit_states[block.id] = exit_stack

        # Collect statements in block order
        all_stmts: List[ast.stmt] = []
        for block in self.blocks:
            all_stmts.extend(self._stmts.get(block.id, []))
        return all_stmts

    def _get_entry_state(self, block: BasicBlock) -> List[ast.AST]:
        """Compute the entry stack state for a block.

        If the block has a single predecessor, use that predecessor's
        exit state. If multiple predecessors (join point), use the
        immediate dominator's exit state — in well-formed CPython
        bytecode, all paths to a join have the same stack depth and
        compatible types. The dominator path is the canonical one.
        """
        if block.id in self._entry_states and self._entry_states[block.id] is not None:
            return self._entry_states[block.id]  # type: ignore[return-value]

        # Use dominator's exit state
        dom = self.idom.get(block.id)
        if dom is not None and dom in self._exit_states and self._exit_states[dom] is not None:
            state = list(self._exit_states[dom])  # type: ignore[arg-type]
        elif block.predecessor_ids:
            # Try first predecessor that has been simulated
            state = []
            for pid in block.predecessor_ids:
                if pid in self._exit_states and self._exit_states[pid] is not None:
                    state = list(self._exit_states[pid])  # type: ignore[arg-type]
                    break
        else:
            state = []

        self._entry_states[block.id] = state
        return state

    def _order_blocks(self) -> List[BasicBlock]:
        """Return blocks in dominator-tree preorder for deterministic simulation."""
        children: Dict[int, List[int]] = {}
        for b in self.blocks:
            pid = self.idom.get(b.id)
            if pid is not None and pid >= 0 and pid < len(self.blocks):
                children.setdefault(pid, []).append(b.id)

        result: List[BasicBlock] = []
        visited: Set[int] = set()

        def dfs(bid: int):
            if bid in visited or bid >= len(self.blocks):
                return
            visited.add(bid)
            result.append(self.blocks[bid])
            for cid in children.get(bid, []):
                dfs(cid)

        entry = next((b for b in self.blocks if b.is_entry), self.blocks[0])
        dfs(entry.id)
        # Append any unreachable blocks
        for b in self.blocks:
            if b.id not in visited:
                result.append(b)
        return result
