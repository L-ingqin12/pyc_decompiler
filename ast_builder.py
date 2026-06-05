"""AST builder: convert CFG structure + stack simulation into Python AST.

This is the core decompilation engine. It walks bytecode instructions
in CFG order, uses the stack simulator to recover expressions, and
pattern-matches bytecode sequences to identify Python constructs.

Supported constructs:
  - Module/function/class bodies
  - if/elif/else, for/while loops
  - try/except/finally, with statements
  - Assignments, augmented assignments
  - Function/class definitions (with decorators)
  - Imports (import x, from x import y)
  - Comprehensions, lambdas
  - Return, yield, raise
  - Expressions (calls, binary ops, comparisons, attributes, subscripts)
"""

from __future__ import annotations

import ast
from typing import Any, List, Optional, Tuple, Union

from .types import Instruction, BasicBlock, CodeObjectInfo
from .stack_sim import StackSimulator, _name_node, _const_node, _attr_node
from .opcodes.base import CMP_OP


class ASTBuilder:
    """Builds Python AST from decompiled bytecode."""

    def __init__(self, info: CodeObjectInfo, ops_mod: Any):
        self.info = info
        self.ops_mod = ops_mod
        self.instructions = info.instructions
        self.blocks = info.blocks
        self.sim = StackSimulator(info)

    def build_module(self) -> ast.Module:
        """Build an ast.Module from the top-level code object."""
        body = self._build_body(self.instructions)
        return ast.Module(body=body, type_ignores=[])

    def build_function(self) -> ast.FunctionDef:
        """Build an ast.FunctionDef from a function code object."""
        body = self._build_body(self.instructions)
        args = self._build_args()
        decorator_list = self._detect_decorators()

        return ast.FunctionDef(
            name=self.info.co_name,
            args=args,
            body=body,
            decorator_list=decorator_list,
            returns=None,
            type_comment=None,
            lineno=self.info.co_firstlineno,
        )

    def build_lambda(self) -> ast.Lambda:
        """Build an ast.Lambda from a lambda code object."""
        # Lambdas have a single expression body
        body_expr = self._build_expression_body()
        args = self._build_args()
        return ast.Lambda(args=args, body=body_expr)

    def _sanitize_name(self, name: str) -> str:
        """Sanitize a Python identifier name (fixes genexpr .0, etc.)."""
        if not name:
            return "_"
        # Replace invalid starting characters
        if name[0] == '.':
            name = '_dot' + name[1:]
        # Replace other invalid characters
        import re
        name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
        if name[0].isdigit():
            name = '_' + name
        return name or "_"

    def _build_args(self) -> ast.arguments:
        """Build function arguments from code object metadata."""
        nargs = self.info.co_argcount
        nkwonly = self.info.co_kwonlyargcount
        nlocals = self.info.co_nlocals
        varnames = self.info.co_varnames

        # Positional args
        pos_args = []
        for i in range(nargs):
            if i < len(varnames):
                pos_args.append(ast.arg(
                    arg=self._sanitize_name(varnames[i]), annotation=None, type_comment=None))

        # Kw-only args
        kwonly_args = []
        for i in range(nargs, nargs + nkwonly):
            if i < len(varnames):
                kwonly_args.append(ast.arg(
                    arg=self._sanitize_name(varnames[i]), annotation=None, type_comment=None))

        # Vararg (*args) and kwarg (**kwargs) detection via flags
        flags = self.info.co_flags
        vararg = None
        kwarg = None
        # CO_VARARGS = 0x04, CO_VARKEYWORDS = 0x08
        co_varargs = 0x04
        co_varkeywords = 0x08

        vararg_idx = nargs + nkwonly
        if flags & co_varargs:
            if vararg_idx < len(varnames):
                vararg = ast.arg(
                    arg=self._sanitize_name(varnames[vararg_idx]), annotation=None, type_comment=None)
                vararg_idx += 1
        if flags & co_varkeywords:
            if vararg_idx < len(varnames):
                kwarg = ast.arg(
                    arg=self._sanitize_name(varnames[vararg_idx]), annotation=None, type_comment=None)

        # Defaults are not stored in code object — we approximate
        defaults = []

        return ast.arguments(
            posonlyargs=[],
            args=pos_args,
            vararg=vararg,
            kwonlyargs=kwonly_args,
            kw_defaults=[],
            kwarg=kwarg,
            defaults=defaults,
        )

    def _detect_decorators(self) -> List[ast.expr]:
        """Detect decorators from bytecode preceding MAKE_FUNCTION."""
        # In bytecode, decorators are CALL_FUNCTION instructions that
        # consume the function object right after MAKE_FUNCTION.
        # We'd need context from the parent frame to detect these.
        # For now, return empty.
        return []

    def _build_body(self, instructions: List[Instruction]) -> List[ast.stmt]:
        """Build a list of AST statements from a linear instruction sequence."""
        stmts: List[ast.stmt] = []
        sim = StackSimulator(self.info)

        i = 0
        while i < len(instructions):
            instr = instructions[i]

            # Try to match structure patterns
            result = self._match_if_statement(instructions, i)
            if result:
                stmts.append(result[0])
                i = result[1]
                continue

            result = self._match_for_loop(instructions, i)
            if result:
                stmts.append(result[0])
                i = result[1]
                continue

            result = self._match_while_loop(instructions, i)
            if result:
                stmts.append(result[0])
                i = result[1]
                continue

            result = self._match_try_except(instructions, i)
            if result:
                stmts.append(result[0])
                i = result[1]
                continue

            result = self._match_with_statement(instructions, i)
            if result:
                stmts.append(result[0])
                i = result[1]
                continue

            result = self._match_class_def(instructions, i)
            if result:
                stmts.append(result[0])
                i = result[1]
                continue

            result = self._match_function_def(instructions, i)
            if result:
                stmts.extend(result[0])
                i = result[1]
                continue

            result = self._match_import(instructions, i)
            if result:
                stmts.append(result[0])
                i = result[1]
                continue

            # Process single instruction through the persistent simulator
            new_stmts = sim.process_instruction(instr)
            if new_stmts:
                stmts.extend(new_stmts)
            i += 1

        # Flush remaining stack as expression statements.
        # Only emit "top-level" nodes that look like complete expressions.
        for expr in sim.stack:
            if isinstance(expr, ast.Constant):
                if expr.value is None:
                    continue
            if isinstance(expr, ast.Attribute):
                if isinstance(expr.attr, str) and expr.attr.startswith('<'):
                    continue
            if isinstance(expr, ast.Name) and isinstance(expr.id, str) and expr.id.startswith('<'):
                continue
            # Only emit expression statements for non-compound expressions
            if isinstance(expr, ast.AST) and not isinstance(expr, (ast.Import, ast.ImportFrom)):
                stmts.append(ast.Expr(value=expr))

        # Strip trailing implicit "return None" (CPython adds this to every module)
        while stmts:
            last = stmts[-1]
            if isinstance(last, ast.Return) and (
                last.value is None
                or (isinstance(last.value, ast.Constant) and last.value.value is None)
            ):
                stmts.pop()
            else:
                break

        # Strip trailing expression statements that are residual stack junk
        while stmts:
            last = stmts[-1]
            if isinstance(last, ast.Expr):
                v = last.value
                # Bare names, compares, tuples, constants, lists — obvious junk
                if isinstance(v, (ast.Name, ast.Compare, ast.Tuple,
                                  ast.List, ast.Set, ast.Dict)):
                    stmts.pop()
                elif isinstance(v, ast.Constant):
                    if v.value is not None and not isinstance(v.value, str):
                        stmts.pop()
                    else:
                        break
                elif isinstance(v, ast.Call):
                    # Bare function calls without assignment are often junk
                    stmts.pop()
                elif isinstance(v, ast.BinOp):
                    stmts.pop()
                else:
                    break
            else:
                break

        # Strip trailing "return None" at module level
        while stmts:
            last = stmts[-1]
            if isinstance(last, ast.Return) and (
                last.value is None
                or (isinstance(last.value, ast.Constant) and last.value.value is None)
            ):
                stmts.pop()
            else:
                break

        return stmts

    def _build_expression_body(self) -> ast.expr:
        """Build a single expression from the lambda body."""
        sim = StackSimulator(self.info)
        for instr in self.instructions:
            stmts = sim.process_instruction(instr)
            if stmts and isinstance(stmts[-1], ast.Return):
                return stmts[-1].value
        # Fallback: last value on stack
        if sim.stack:
            return sim.stack[-1]
        return ast.Constant(value=None)

    def _match_if_statement(
        self, instrs: List[Instruction], start: int
    ) -> Optional[Tuple[ast.If, int]]:
        """Match if/elif/else patterns.

        Pattern in 3.7/3.8:
          ... condition evaluation ...
          POP_JUMP_IF_FALSE <else_target>
          ... true body ...
          JUMP_FORWARD <end_target>   (or JUMP_ABSOLUTE)
          else_target:
          ... else/elif body ...
          end_target:
        """
        if start >= len(instrs):
            return None
        cond_instr = instrs[start]
        if cond_instr.opname != "POP_JUMP_IF_FALSE":
            return None

        else_target = cond_instr.target_offset
        if else_target is None:
            return None

        # Compute condition from instructions BEFORE the POP_JUMP_IF_FALSE.
        # POP_JUMP_IF_FALSE pops the condition, so we compute up to start-1.
        test = self._compute_expression(instrs, start - 1) if start > 0 else ast.Constant(value=None)

        # Collect true body instructions: from start+1 until else_target
        true_instrs = []
        true_body_end = start + 1
        for j in range(start + 1, len(instrs)):
            jinstr = instrs[j]
            if jinstr.offset >= else_target:
                true_body_end = j
                break
            # Check for JUMP_FORWARD that jumps past else_target (indicates else/elif)
            if jinstr.opname in {"JUMP_FORWARD", "JUMP_ABSOLUTE"}:
                tgt = jinstr.target_offset
                if tgt is not None and tgt > else_target:
                    true_body_end = j
                    break
            true_instrs.append(jinstr)
        else:
            true_body_end = len(instrs)

        # Build true body
        true_body = self._build_body(true_instrs)
        if not true_body:
            true_body = [ast.Pass()]

        # Build else/elif body
        orelse = []
        end_target = else_target
        next_start = true_body_end

        # Check if there's an else/elif body between else_target and end_target
        # Find the end of the entire if/elif/else chain
        for j in range(true_body_end, len(instrs)):
            jinstr = instrs[j]
            if jinstr.offset >= else_target and jinstr.opname not in {
                "JUMP_FORWARD", "JUMP_ABSOLUTE", "POP_BLOCK",
                "RETURN_VALUE", "POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE",
            }:
                # Check if this is part of else body
                pass

        # Collect else/elif instructions
        orelse_instrs = []
        orelse_end = true_body_end
        if true_body_end < len(instrs):
            first_after = instrs[true_body_end]
            if (first_after.offset >= else_target
                    or first_after.opname in {"JUMP_FORWARD", "JUMP_ABSOLUTE"}):
                # Find the end of else block
                jump_tgt = first_after.target_offset if first_after.opname in {
                    "JUMP_FORWARD", "JUMP_ABSOLUTE"} else None
                orelse_start = true_body_end + (1 if first_after.opname in {
                    "JUMP_FORWARD", "JUMP_ABSOLUTE"} else 0)
                for j in range(orelse_start, len(instrs)):
                    jinstr = instrs[j]
                    if jump_tgt and jinstr.offset >= jump_tgt:
                        orelse_end = j
                        break
                    orelse_instrs.append(jinstr)
                else:
                    orelse_end = len(instrs)

                if orelse_instrs:
                    orelse = self._build_body(orelse_instrs)
                    next_start = orelse_end
                else:
                    next_start = true_body_end
            else:
                next_start = true_body_end
        else:
            next_start = true_body_end

        return ast.If(test=test, body=true_body, orelse=orelse), next_start

    def _match_for_loop(
        self, instrs: List[Instruction], start: int
    ) -> Optional[Tuple[ast.For, int]]:
        """Match for loop patterns.

        Pattern in 3.7/3.8:
          SETUP_LOOP <end_target>       (3.7 only)
          ... iterable evaluation ...
          GET_ITER
          FOR_ITER <body_target>
          ... body ...
          JUMP_ABSOLUTE <loop_start>    (back edge)
          body_target:
          ... body ...
          POP_BLOCK                    (3.7)
          end_target:
        """
        if start >= len(instrs):
            return None

        # Look for SETUP_LOOP (3.7) or just GET_ITER/FOR_ITER
        i = start
        setup_target = None
        if instrs[i].opname == "SETUP_LOOP":
            setup_target = instrs[i].target_offset
            i += 1

        # Find GET_ITER then FOR_ITER
        get_iter_idx = None
        for_iter_idx = None
        for j in range(i, len(instrs)):
            if instrs[j].opname == "GET_ITER":
                get_iter_idx = j
                break

        if get_iter_idx is None:
            return None

        for j in range(get_iter_idx + 1, len(instrs)):
            if instrs[j].opname == "FOR_ITER":
                for_iter_idx = j
                break

        if for_iter_idx is None:
            return None

        for_iter = instrs[for_iter_idx]
        body_target = for_iter.target_offset
        if body_target is None:
            return None

        # The iterable is computed before GET_ITER
        # The loop variable is stored at body_target

        # Find body instructions
        body_start = for_iter_idx + 1
        body_instrs = []
        body_end = for_iter_idx + 1
        for j in range(body_start, len(instrs)):
            if instrs[j].offset >= body_target:
                body_instrs = [ins for ins in instrs[body_start:j]
                              if ins.offset < body_target]
                body_end = j
                break

        # Find loop end
        loop_end = len(instrs)
        if setup_target:
            for j in range(body_end, len(instrs)):
                if instrs[j].offset >= setup_target:
                    loop_end = j
                    break

        # Build AST
        iter_expr = self._compute_expression(instrs, get_iter_idx - 1)
        target = self._compute_loop_target(instrs, for_iter_idx)
        body = self._build_body(body_instrs)

        return ast.For(
            target=target,
            iter=iter_expr,
            body=body,
            orelse=[],
            type_comment=None,
        ), loop_end

    def _match_while_loop(
        self, instrs: List[Instruction], start: int
    ) -> Optional[Tuple[Union[ast.While, ast.For], int]]:
        """Match while loop patterns.

        A while loop has a backward jump from the body to the condition.
        If there's no backward jump, it's an if statement (handled elsewhere).
        """
        if start >= len(instrs):
            return None

        # While loops start with SETUP_LOOP in 3.7
        i = start
        setup_target = None
        if instrs[i].opname == "SETUP_LOOP":
            setup_target = instrs[i].target_offset
            i += 1

        # The condition is evaluated, then POP_JUMP_IF_FALSE
        cond_end = None
        cond_target = None
        for j in range(i, len(instrs)):
            if instrs[j].opname == "POP_JUMP_IF_FALSE":
                target = instrs[j].target_offset
                if target and target > instrs[j].offset:
                    cond_end = j
                    cond_target = target
                    break

        if cond_end is None:
            return None

        # Find the backward jump that defines this as a while loop.
        # A while loop MUST have a backward jump to before the condition.
        body_instrs = []
        body_end = cond_end + 1
        has_backward = False
        for j in range(cond_end + 1, len(instrs)):
            jinstr = instrs[j]
            # Stop if we've passed the loop exit (cond_target or setup_target)
            loop_end_target = setup_target or cond_target
            if (loop_end_target is not None
                    and jinstr.offset >= loop_end_target
                    and jinstr.opname not in {"JUMP_ABSOLUTE", "JUMP_BACKWARD"}):
                break
            if jinstr.opname in {"JUMP_ABSOLUTE", "JUMP_BACKWARD"}:
                tgt = jinstr.target_offset
                if tgt is not None and tgt < instrs[start].offset:
                    # Found backward jump to start — this IS a while loop
                    has_backward = True
                    body_instrs = instrs[cond_end + 1:j]
                    body_end = j + 1
                    break

        # No backward jump = not a while loop (it's an if statement)
        if not has_backward:
            return None

        # Build test expression
        test = self._compute_expression(instrs, cond_end)

        body = self._build_body(body_instrs)
        if not body:
            body = [ast.Pass()]

        return ast.While(test=test, body=body, orelse=[]), body_end

    def _match_try_except(
        self, instrs: List[Instruction], start: int
    ) -> Optional[Tuple[ast.Try, int]]:
        """Match try/except/finally patterns.

        3.7 pattern:
          SETUP_EXCEPT <handler_target>
          ... try body ...
          POP_BLOCK
          JUMP_FORWARD <end_target>
          handler_target:
          ... except body ...
          end_target:

        3.8 pattern:
          SETUP_FINALLY <handler_target>
          ... try body ...
          POP_BLOCK
          JUMP_FORWARD <end_target>
          handler_target:
          ... handler body ...
          END_FINALLY
          end_target:
        """
        if start >= len(instrs):
            return None

        instr = instrs[start]
        if instr.opname not in {"SETUP_EXCEPT", "SETUP_FINALLY"}:
            return None

        handler_target = instr.target_offset
        if handler_target is None:
            return None

        # Find POP_BLOCK → JUMP_FORWARD
        pop_block_idx = None
        jump_forward_idx = None
        end_target = None
        for j in range(start + 1, len(instrs)):
            jinstr = instrs[j]
            if jinstr.offset >= handler_target:
                break
            if jinstr.opname == "POP_BLOCK":
                pop_block_idx = j
            elif pop_block_idx and jinstr.opname in {"JUMP_FORWARD", "JUMP_ABSOLUTE"}:
                jump_forward_idx = j
                end_target = jinstr.target_offset
                break

        if pop_block_idx is None:
            pop_block_idx = start + 1
            for j in range(start + 1, len(instrs)):
                if instrs[j].offset >= handler_target:
                    pop_block_idx = j - 1
                    break

        # Try body
        try_instrs = [ins for ins in instrs[start + 1:pop_block_idx + 1]
                      if ins.offset < handler_target]
        try_body = self._build_body(try_instrs)
        if not try_body:
            try_body = [ast.Pass()]

        # Handler body
        handler_end = pop_block_idx + 1
        if end_target:
            for j in range(pop_block_idx + 1, len(instrs)):
                if instrs[j].offset >= end_target:
                    handler_end = j
                    break

        handler_instrs = [ins for ins in instrs
                         if handler_target <= ins.offset < (end_target or handler_target + 100)]
        handler_body = self._build_body(handler_instrs)
        if not handler_body:
            handler_body = [ast.Pass()]

        # Determine if finally (SETUP_FINALLY) or except (SETUP_EXCEPT)
        if instr.opname == "SETUP_FINALLY":
            return ast.Try(
                body=try_body,
                handlers=[],
                finalbody=handler_body,
                orelse=[],
            ), max(handler_end, pop_block_idx + 1)

        # Try/except
        handlers = [ast.ExceptHandler(
            type=None,
            name=None,
            body=handler_body,
        )]

        return ast.Try(
            body=try_body,
            handlers=handlers,
            finalbody=[],
            orelse=[],
        ), max(handler_end, pop_block_idx + 1)

    def _match_with_statement(
        self, instrs: List[Instruction], start: int
    ) -> Optional[Tuple[ast.With, int]]:
        """Match with statement patterns.

        Pattern:
          SETUP_WITH <end_target>
          ... context expression ...
          ... body ...
          WITH_CLEANUP_START / WITH_CLEANUP_FINISH
          POP_BLOCK
          end_target:
        """
        if start >= len(instrs):
            return None

        instr = instrs[start]
        if instr.opname != "SETUP_WITH":
            return None

        end_target = instr.target_offset
        if end_target is None:
            return None

        # Find context expression (after SETUP_WITH, before body starts)
        ctx_expr_start = start + 1

        # Find WITH_CLEANUP + POP_BLOCK
        body_end = start + 1
        for j in range(ctx_expr_start, len(instrs)):
            jinstr = instrs[j]
            if jinstr.opname in {"WITH_CLEANUP_START", "WITH_CLEANUP_FINISH",
                                 "POP_BLOCK"}:
                body_end = j
                break
            if jinstr.offset >= end_target:
                body_end = j
                break

        body_instrs = instrs[ctx_expr_start:body_end]
        body = self._build_body(body_instrs)

        # Context expression
        ctx_expr = self._compute_expression(instrs, ctx_expr_start)

        return ast.With(
            items=[ast.withitem(context_expr=ctx_expr, optional_vars=None)],
            body=body,
            type_comment=None,
        ), body_end + 1

    def _match_class_def(
        self, instrs: List[Instruction], start: int
    ) -> Optional[Tuple[ast.ClassDef, int]]:
        """Match class definition patterns.

        3.7/3.8 pattern (no base):
          LOAD_BUILD_CLASS
          LOAD_CONST <class body code object>
          LOAD_CONST '<ClassName>'
          MAKE_FUNCTION 0
          LOAD_CONST '<ClassName>'
          CALL_FUNCTION 2            ← __build_class__(body, name)

        3.7/3.8 pattern (with bases like unittest.TestCase):
          LOAD_BUILD_CLASS
          LOAD_CONST <class body code object>
          LOAD_CONST '<ClassName>'
          MAKE_FUNCTION 0
          LOAD_CONST '<ClassName>'
          LOAD_NAME <base_module>    ← e.g. 'unittest'
          LOAD_ATTR <base_name>      ← e.g. 'TestCase'
          CALL_FUNCTION 3            ← __build_class__(body, name, base)

        The pattern length varies; we detect the mandatory prefix
        (LOAD_BUILD_CLASS + LOAD_CONST + LOAD_CONST + MAKE_FUNCTION)
        then scan forward to find CALL_FUNCTION + STORE_NAME.
        """
        if start >= len(instrs):
            return None
        i0 = instrs[start]
        if i0.opname != "LOAD_BUILD_CLASS":
            return None

        # Need at least 7 instructions for minimum pattern
        if start + 6 >= len(instrs):
            return None

        i1 = instrs[start + 1] if start + 1 < len(instrs) else None
        i2 = instrs[start + 2] if start + 2 < len(instrs) else None
        i3 = instrs[start + 3] if start + 3 < len(instrs) else None

        # Mandatory prefix: LOAD_CONST(code), LOAD_CONST(name), MAKE_FUNCTION
        if not (i1 and i1.opname == "LOAD_CONST"
                and i2 and i2.opname == "LOAD_CONST"
                and i3 and i3.opname in {"MAKE_FUNCTION", "MAKE_CLOSURE"}):
            return None

        import types
        from .types import CodeObjectInfo
        from .loader import _extract_code_info, _collect_nested_code_objects

        class_info = None
        if isinstance(i1.argval, types.CodeType):
            class_info = _extract_code_info(i1.argval)
            _collect_nested_code_objects(class_info)
        elif isinstance(i1.argval, CodeObjectInfo):
            class_info = i1.argval
        if class_info is None:
            return None

        class_name = i2.argval if isinstance(i2.argval, str) else ""

        # Scan forward from i3+1 to find CALL_FUNCTION + STORE_NAME
        # Collect base classes from LOAD_NAME/LOAD_ATTR pairs before CALL
        call_idx = None
        store_idx = None
        bases = []
        base_instrs = []
        for j in range(start + 4, min(start + 12, len(instrs))):
            jinstr = instrs[j]
            if jinstr.opname in {"CALL_FUNCTION", "CALL"}:
                call_idx = j
                break
            base_instrs.append(jinstr)

        if call_idx is None or call_idx + 1 >= len(instrs):
            return None
        store_instr = instrs[call_idx + 1]
        if not store_instr.opname.startswith("STORE_"):
            return None
        store_idx = call_idx + 1

        # Use stack simulator to compute base class expressions.
        # base_instrs has instructions between MAKE_FUNCTION and CALL_FUNCTION.
        # The first instruction is always LOAD_CONST (class name).
        # After that, instructions compute bases. We simulate them on a stack.
        base_sim = StackSimulator(self.info)
        for bi in base_instrs:
            base_sim.process_instruction(bi)
        # The stack after simulation has [Const(name), ...bases]
        # Pop the first item (repeated class name), remainder are bases
        if base_sim.stack:
            # First item is the LOAD_CONST name
            name_item = base_sim.stack[0]
            if isinstance(name_item, ast.Constant):
                base_sim.stack.pop(0)
        bases = list(base_sim.stack)

        # Disassemble class body
        if not class_info.instructions:
            from .disassembler import disassemble_all
            disassemble_all(class_info, self.ops_mod)

        builder = ASTBuilder(class_info, self.ops_mod)
        class_body = builder._build_body(class_info.instructions)
        if not class_body:
            class_body = [ast.Pass()]

        return ast.ClassDef(
            name=class_name,
            bases=bases,
            keywords=[],
            body=class_body,
            decorator_list=[],
        ), store_idx + 1

    def _match_function_def(
        self, instrs: List[Instruction], start: int
    ) -> Optional[Tuple[List[ast.stmt], int]]:
        """Match function definition patterns.

        Python 3.7/3.8 pattern:
          LOAD_CONST <code object>
          LOAD_CONST '<name>'
          MAKE_FUNCTION <flags>
          STORE_FAST / STORE_NAME <funcname>

        Python 3.12 pattern:
          LOAD_CONST <code object>
          MAKE_FUNCTION <flags>
          STORE_FAST / STORE_NAME <funcname>
        """
        if start + 1 >= len(instrs):
            return None

        import types

        from .types import CodeObjectInfo
        from .loader import _extract_code_info, _collect_nested_code_objects

        i0 = instrs[start]
        i1 = instrs[start + 1] if start + 1 < len(instrs) else None
        i2 = instrs[start + 2] if start + 2 < len(instrs) else None
        nested_info = None
        func_name = "<function>"
        store_idx = start + 2

        # Check for 3.7/3.8 pattern: LOAD_CONST (code) → LOAD_CONST (name) → MAKE_FUNCTION
        if (i0.opname == "LOAD_CONST" and i1 and i1.opname == "LOAD_CONST"
                and i2 and i2.opname in {"MAKE_FUNCTION", "MAKE_CLOSURE"}):
            if isinstance(i0.argval, types.CodeType):
                nested_info = _extract_code_info(i0.argval)
                _collect_nested_code_objects(nested_info)
                func_name = i1.argval if isinstance(i1.argval, str) else "<function>"
                store_idx = start + 3
            elif isinstance(i0.argval, CodeObjectInfo):
                nested_info = i0.argval
                func_name = i1.argval if isinstance(i1.argval, str) else "<function>"
                store_idx = start + 3
        # Check for 3.12 pattern: LOAD_CONST (code) → MAKE_FUNCTION
        elif (i0.opname == "LOAD_CONST" and i1
                and i1.opname in {"MAKE_FUNCTION", "MAKE_CLOSURE"}):
            if isinstance(i0.argval, types.CodeType):
                nested_info = _extract_code_info(i0.argval)
                _collect_nested_code_objects(nested_info)
                func_name = nested_info.co_name
                store_idx = start + 2
            elif isinstance(i0.argval, CodeObjectInfo):
                nested_info = i0.argval
                func_name = nested_info.co_name
                store_idx = start + 2
        else:
            return None

        if nested_info is None:
            return None

        # Check if there's a STORE instruction after MAKE_FUNCTION
        if store_idx < len(instrs) and instrs[store_idx].opname.startswith("STORE_"):
            func_name = instrs[store_idx].argval or func_name
            store_idx += 1

        from .disassembler import disassemble_all
        # For CodeObjectInfo from xmarshal, nested code objects are already populated
        # and instructions may already be set. Only disassemble if needed.
        if not nested_info.instructions:
            disassemble_all(nested_info, self.ops_mod)

        builder = ASTBuilder(nested_info, self.ops_mod)
        func_def = builder.build_function()
        # Sanitize function name: handle lambdas, genexprs, listcomps
        # which have names like "Parent.<locals>.<lambda>"
        clean_name = func_name
        if '<' in clean_name or '.' in clean_name:
            clean_name = clean_name.replace('<', '_').replace('>', '_')
            clean_name = clean_name.replace('.', '_')
        func_def.name = clean_name
        # Ensure non-empty body
        if not func_def.body:
            func_def.body = [ast.Pass()]

        return [func_def], store_idx

    def _match_import(
        self, instrs: List[Instruction], start: int
    ) -> Optional[Tuple[ast.Import | ast.ImportFrom, int]]:
        """Match import statements.

        Pattern (import X):
          LOAD_CONST 0 (level)
          LOAD_CONST None (fromlist)
          IMPORT_NAME 'X'
          STORE_NAME 'X'

        Pattern (from X import Y):
          LOAD_CONST 0 (level)
          LOAD_CONST ('Y',) (fromlist)
          IMPORT_NAME 'X'
          IMPORT_FROM 'Y'
          STORE_NAME 'Y'
        """
        if start + 4 >= len(instrs):
            return None

        i0, i1, i2 = instrs[start], instrs[start + 1], instrs[start + 2]
        if not (i0.opname == "LOAD_CONST" and i1.opname == "LOAD_CONST"
                and i2.opname == "IMPORT_NAME"):
            return None

        level = i0.argval
        fromlist = i1.argval
        module_name = i2.argval

        end = start + 3

        if fromlist is None:
            # Simple import: "import X" or "import X as Y"
            if end < len(instrs) and instrs[end].opname == "STORE_NAME":
                alias_name = instrs[end].argval
                if alias_name and alias_name != module_name:
                    return ast.Import(names=[ast.alias(name=module_name, asname=alias_name)]), end + 1
                return ast.Import(names=[ast.alias(name=module_name, asname=None)]), end + 1
            return ast.Import(names=[ast.alias(name=module_name, asname=None)]), end
        elif isinstance(fromlist, tuple) and len(fromlist) > 0:
            # from X import Y
            names = []
            for _ in range(len(fromlist)):
                if end < len(instrs) and instrs[end].opname == "IMPORT_FROM":
                    alias_name = instrs[end].argval
                    store_end = end + 1
                    # Check if STORE_NAME follows (could rename)
                    if store_end < len(instrs) and instrs[store_end].opname == "STORE_NAME":
                        imported_name = instrs[store_end].argval or alias_name
                        asname = None if imported_name == alias_name else alias_name
                        names.append(ast.alias(name=alias_name, asname=asname))
                        end = store_end + 1
                    else:
                        names.append(ast.alias(name=alias_name, asname=None))
                        end = store_end
                else:
                    break

            return ast.ImportFrom(
                module=module_name,
                names=names,
                level=level if isinstance(level, int) else 0,
            ), end

        return None

    def _process_single_instruction(
        self, sim: StackSimulator, instrs: List[Instruction], idx: int
    ) -> Optional[ast.stmt]:
        """Process a single instruction that doesn't start a compound statement.

        Returns an AST statement node or None.
        """
        instr = instrs[idx]
        sim = StackSimulator(self.info)

        # Look at a small window of instructions
        window = instrs[idx:idx + 5]
        stmts = []
        temp_sim = StackSimulator(self.info)

        for winstr in window:
            s = temp_sim.process_instruction(winstr)
            stmts.extend(s)

        # If no statements generated, it's a pure expression
        if not stmts and temp_sim.stack:
            return ast.Expr(value=temp_sim.stack[-1])

        return stmts[0] if stmts else None

    def _compute_expression(
        self, instrs: List[Instruction], end_idx: int
    ) -> ast.expr:
        """Compute the expression computed up to (and including) end_idx."""
        sim = StackSimulator(self.info)
        for i in range(end_idx + 1):
            sim.process_instruction(instrs[i])
        if sim.stack:
            return sim.stack[-1]
        return ast.Constant(value=None)

    def _compute_loop_target(
        self, instrs: List[Instruction], for_iter_idx: int
    ) -> ast.expr:
        """Compute the loop variable from the for loop setup.

        In Python 3.7/3.8 bytecode:
          GET_ITER
          FOR_ITER <exit_target>   # pushes next item, or jumps to exit
          STORE_FAST <var>         # loop variable (right after FOR_ITER)
          ... body ...

        The loop variable is stored in the instruction AFTER FOR_ITER.
        Handles UNPACK_SEQUENCE for tuple unpacking like 'for a, b in ...'.
        """
        if for_iter_idx + 1 < len(instrs):
            next_instr = instrs[for_iter_idx + 1]
            if next_instr.opname == "UNPACK_SEQUENCE":
                # for a, b in iterable →
                #   UNPACK_SEQUENCE 2
                #   STORE_FAST a
                #   STORE_FAST b
                n = next_instr.arg
                names = []
                for k in range(for_iter_idx + 2, min(for_iter_idx + 2 + n, len(instrs))):
                    si = instrs[k]
                    if si.opname in {"STORE_FAST", "STORE_NAME", "STORE_DEREF"}:
                        names.append(_name_node(si.argval or f"<v{si.arg}>", ctx=ast.Store))
                    else:
                        break
                if len(names) == n:
                    return ast.Tuple(elts=names, ctx=ast.Store())
                elif len(names) > 0:
                    return names[0]
                return _name_node(f"<unpack_{n}>")
            if next_instr.opname in {"STORE_FAST", "STORE_NAME", "STORE_DEREF"}:
                return _name_node(next_instr.argval or f"<var_{next_instr.arg}>")
        return _name_node("<loop_var>")


def build_ast(info: CodeObjectInfo, ops_mod: Any) -> ast.Module:
    """Build an AST module from a decompiled code object.

    Args:
        info: The disassembled code object info.
        ops_mod: The opcode module for the Python version.

    Returns:
        An ast.Module node.
    """
    builder = ASTBuilder(info, ops_mod)
    return builder.build_module()


def decompile_code_object(info: CodeObjectInfo, ops_mod: Any) -> ast.AST:
    """Decompile a code object into an AST.

    For module-level code, returns ast.Module.
    For function code, returns ast.FunctionDef.
    """
    builder = ASTBuilder(info, ops_mod)

    # Check if this is a function/class body or module
    flags = info.co_flags
    # CO_NEWLOCALS = 0x02 (function scope)
    # CO_NOFREE = 0x40 (no free variables)
    co_newlocals = 0x02
    co_nofree = 0x40

    if flags & co_newlocals:
        # Function or class body
        return builder.build_function()
    return builder.build_module()
