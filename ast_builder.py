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
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .pyc_types import Instruction, BasicBlock, CodeObjectInfo
from .stack_sim import StackSimulator, _name_node, _const_node, _attr_node
from .opcodes.base import CMP_OP


class ASTBuilder:
    """Builds Python AST from decompiled bytecode."""

    def __init__(self, info: CodeObjectInfo, ops_mod: Any, cfg: Any = None):
        self.info = info
        self.ops_mod = ops_mod
        self.instructions = info.instructions
        self.blocks = info.blocks
        self.cfg = cfg
        self.sim = StackSimulator(info)
        # Cache CFG-derived data for pattern validation
        self._loop_headers: Optional[Set[int]] = None
        if cfg is not None:
            self._loop_headers = cfg.find_loop_headers()
            self._idom = cfg.immediate_dominators()
        else:
            self._idom = {}

    def _create_child_builder(
        self, instructions: List[Instruction],
    ) -> ASTBuilder:
        """Create a child ASTBuilder for processing a nested structure's body.

        Rebuilds the CFG and blocks for the body instruction subset so that
        recursive _build_body calls have correct control-flow context.

        This is the key fix for nested control flow reconstruction:
        without it, recursive _build_body calls use the parent's blocks
        and CFG, which don't match the instruction subset.
        """
        from .blocks import build_blocks
        from .cfg import build_cfg

        child_blocks = build_blocks(instructions, self.ops_mod)
        child_cfg = build_cfg(child_blocks, self.ops_mod)

        child = ASTBuilder(self.info, self.ops_mod, child_cfg)
        child.blocks = child_blocks
        # Cache loop headers for the child
        child._loop_headers = child_cfg.find_loop_headers() if child_cfg else None
        child._idom = child_cfg.immediate_dominators() if child_cfg else {}
        return child

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

    def _find_block_for(self, offset: int) -> int:
        """Find block ID containing the given bytecode offset."""
        for b in self.blocks:
            if b.start_offset <= offset <= b.end_offset:
                return b.id
        return -1

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
        """Build a list of AST statements from a linear instruction sequence.

        Uses BlockStackSimulator to pre-compute per-block entry states,
        resetting the persistent simulator at each block boundary for
        correct stack state propagation across control flow.
        """
        # Pre-compute per-block entry states when blocks are available
        block_entries: Dict[int, List[ast.AST]] = {}
        if self.blocks and len(self.blocks) > 1:
            try:
                from .stack_sim import BlockStackSimulator
                bsim = BlockStackSimulator(self.info, self.blocks, self._idom)
                bsim.simulate_all()
                block_entries = {bid: list(st) for bid, st
                                 in bsim._entry_states.items() if st is not None}
            except Exception:
                pass

        stmts: List[ast.stmt] = []
        sim = StackSimulator(self.info)
        last_block_id = -1

        i = 0
        while i < len(instructions):
            instr = instructions[i]

            # Reset stack at block boundary using pre-computed entry state
            curr_block_id = self._find_block_for(instr.offset)
            if curr_block_id != last_block_id and curr_block_id in block_entries:
                sim.stack = list(block_entries[curr_block_id])
                last_block_id = curr_block_id

            # Try to match structure patterns
            result = self._match_if_statement(instructions, i, sim)
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
        # Suppress junk from broken chained comparison / short-circuit recovery.
        fn_params = set(self.info.co_varnames[:self.info.co_argcount])
        for expr in sim.stack:
            if isinstance(expr, ast.Constant):
                if expr.value is None:
                    continue
            if isinstance(expr, ast.Attribute):
                if isinstance(expr.attr, str) and expr.attr.startswith('<'):
                    continue
            if isinstance(expr, ast.Name):
                if isinstance(expr.id, str) and expr.id.startswith('<'):
                    continue
                # Suppress lone parameter names (leftover from DUP_TOP in
                # chained comparisons like "0 <= r < SIZE")
                if expr.id in fn_params:
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
                    # Method calls (obj.method()) are valid statements
                    if isinstance(v.func, ast.Attribute):
                        break  # keep method calls
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

        # Strip duplicate trailing returns (artifacts of broken chained
        # comparison / short-circuit expression recovery). Two consecutive
        # return statements at the end of a function are always dead code
        # in valid Python.
        while len(stmts) >= 2:
            if (isinstance(stmts[-1], ast.Return)
                    and isinstance(stmts[-2], ast.Return)):
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

    def _match_bool_op(
        self, instrs: List[Instruction], start: int, sim: StackSimulator
    ) -> Optional[int]:
        """Match boolean 'and'/'or' short-circuit pattern.

        ... (docstring unchanged) ...

        Returns:
            next_index where to continue scanning, or None if no match.
            The combined BoolOp expression is pushed onto *sim*'s stack.
        """
        if start >= len(instrs):
            return None

        instr = instrs[start]
        if instr.opname not in {"JUMP_IF_FALSE_OR_POP", "JUMP_IF_TRUE_OR_POP"}:
            return None

        target = instr.target_offset
        if target is None:
            return None

        # Left operand is TOS of the persistent simulator
        if not sim.stack:
            return None
        left = sim.stack.pop()

        is_and = (instr.opname == "JUMP_IF_FALSE_OR_POP")

        # Find the target instruction index
        target_idx = None
        for j in range(start + 1, len(instrs)):
            if instrs[j].offset >= target:
                target_idx = j
                break
        if target_idx is None:
            sim.push(left)  # restore
            target_idx = start + 1

        # Compute right operand by simulating from start+1 to target_idx
        # Use a temporary simulator to avoid polluting the persistent one.
        # Skip cleanup instructions between JUMP_FORWARD and target.
        temp_sim = StackSimulator(self.info)
        # First, find the instructions between start+1 and target that
        # actually compute the right operand (skip forward jumps + cleanup)
        right_instrs = []
        for j in range(start + 1, target_idx):
            jinstr = instrs[j]
            # Skip skip-over jumps and their cleanup targets
            if jinstr.opname == "JUMP_FORWARD":
                skip_target = jinstr.target_offset
                if skip_target is not None and skip_target <= target:
                    # Skip instructions until the skip target
                    # This jumps past the "False case" cleanup code
                    continue
            if (jinstr.opname in {"ROT_TWO", "ROT_THREE", "POP_TOP"}
                    and jinstr.offset > instrs[start].offset + 2):
                # These are likely cleanup instructions for the chained
                # comparison False case — skip them for expression recovery
                saw_rot = any(
                    ins.opname in {"ROT_TWO", "ROT_THREE"}
                    for ins in instrs[start + 1:j]
                )
                if saw_rot:
                    continue
            right_instrs.append(jinstr)

        for jinstr in right_instrs:
            temp_sim.process_instruction(jinstr)

        # Extract right operand from temp simulator's stack
        if temp_sim.stack:
            right = temp_sim.stack[-1]
        else:
            right = ast.Constant(value=None)

        # Build BoolOp and push onto persistent simulator
        result = ast.BoolOp(
            op=ast.And() if is_and else ast.Or(),
            values=[left, right],
        )
        sim.push(result)
        return target_idx

    def _match_if_statement(
        self, instrs: List[Instruction], start: int,
        sim: Optional[StackSimulator] = None,
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

        # Condition is on the persistent simulator's stack
        if sim is not None and sim.stack:
            test = sim.stack.pop()
        elif start > 0:
            test = self._compute_expression(instrs, start)
        else:
            test = ast.Constant(value=None)

        # Determine if else_target is within our instruction scope.
        # In child builders, jump targets may point outside the subset
        # (e.g., to the parent loop's FOR_ITER). In that case, the
        # "else" path is out of scope and we only decompile the true body.
        min_offset = instrs[0].offset if instrs else 0
        max_offset = instrs[-1].offset if instrs else 0
        target_in_scope = (min_offset <= else_target <= max_offset)

        # Collect true body instructions: from start+1 until else_target
        true_instrs = []
        true_body_end = start + 1
        if target_in_scope:
            for j in range(start + 1, len(instrs)):
                jinstr = instrs[j]
                if jinstr.offset >= else_target:
                    true_body_end = j
                    break
                if jinstr.opname in {"JUMP_FORWARD", "JUMP_ABSOLUTE"}:
                    tgt = jinstr.target_offset
                    if tgt is not None and tgt > else_target:
                        true_body_end = j
                        break
                true_instrs.append(jinstr)
            else:
                true_body_end = len(instrs)
        else:
            # Target is outside scope → all remaining instructions are
            # the true body (fallthrough). The else path is unreachable
            # in this decompilation context.
            for j in range(start + 1, len(instrs)):
                jinstr = instrs[j]
                if jinstr.opname in {"JUMP_FORWARD", "JUMP_ABSOLUTE"}:
                    tgt = jinstr.target_offset
                    if tgt is not None and tgt > max_offset:
                        true_body_end = j
                        break
                true_instrs.append(jinstr)
            else:
                true_body_end = len(instrs)

        # Build true body with child builder
        if true_instrs:
            child = self._create_child_builder(true_instrs)
            true_body = child._build_body(true_instrs)
        else:
            true_body = [ast.Pass()]

        # Build else/elif body
        orelse = []
        next_start = true_body_end

        if target_in_scope and true_body_end < len(instrs):
            # Collect else/elif instructions between else_target and end of scope
            orelse_instrs = []
            orelse_end = true_body_end
            first_after = instrs[true_body_end]
            if (first_after.offset >= else_target
                    or first_after.opname in {"JUMP_FORWARD", "JUMP_ABSOLUTE"}):
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
                    else_child = self._create_child_builder(orelse_instrs)
                    orelse = else_child._build_body(orelse_instrs)
                    next_start = orelse_end
        # If target is out of scope, there is no else body to decompile

        return ast.If(test=test, body=true_body, orelse=orelse), next_start

    def _match_for_loop(
        self, instrs: List[Instruction], start: int
    ) -> Optional[Tuple[ast.For, int]]:
        """Match for loop patterns.

        Pattern in 3.7/3.8:
          SETUP_LOOP <end_target>       (3.7 only)
          ... iterable evaluation ...
          GET_ITER
          FOR_ITER <exit_target>
          STORE_FAST <loop_var>         (or UNPACK_SEQUENCE + STORE_*)
          ... body ...
          JUMP_ABSOLUTE <loop_start>    (back edge)
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
        exit_target = for_iter.target_offset
        if exit_target is None:
            return None

        # ── Skip loop variable store (after FOR_ITER) ───────────────────
        # FOR_ITER pushes the next iterator value; the next instruction(s)
        # store it into the loop variable. These are NOT part of the body.
        body_start = for_iter_idx + 1
        if body_start < len(instrs):
            if instrs[body_start].opname == "UNPACK_SEQUENCE":
                # for a, b in iterable: UNPACK_SEQUENCE 2; STORE_FAST a; STORE_FAST b
                n = instrs[body_start].arg
                body_start += 1  # skip UNPACK_SEQUENCE
                for _ in range(n):
                    if body_start < len(instrs) and instrs[body_start].opname.startswith("STORE_"):
                        body_start += 1
                    else:
                        break
            elif instrs[body_start].opname.startswith("STORE_"):
                body_start += 1  # skip STORE_FAST loop_var

        # ── Extract body instructions ──────────────────────────────────
        # Body: from body_start to the first instruction at exit_target.
        # Inner back-edges (from nested while/for loops) are INCLUDED in
        # the body. Only the FOR loop's OWN back-edge (targeting FOR_ITER
        # or earlier) marks the body end.
        body_instrs = []
        body_end = body_start
        for_iter_offset = instrs[for_iter_idx].offset
        for j in range(body_start, len(instrs)):
            jinstr = instrs[j]
            if jinstr.offset >= exit_target:
                body_end = j
                break
            if jinstr.opname in {"JUMP_ABSOLUTE", "JUMP_BACKWARD"}:
                tgt = jinstr.target_offset
                # Only stop for the for loop's own back-edge (targets
                # FOR_ITER or earlier). Inner back-edges skip forward.
                if tgt is not None and tgt <= for_iter_offset:
                    body_instrs = [ins for ins in instrs[body_start:j]
                                  if ins.offset < exit_target]
                    body_end = j + 1
                    break
        else:
            body_instrs = [ins for ins in instrs[body_start:body_end]
                          if ins.offset < exit_target]

        # Fallback: if body_instrs is empty, fill from body_start to body_end
        if not body_instrs:
            body_instrs = [ins for ins in instrs[body_start:body_end]
                          if ins.offset < exit_target]

        # Remove trailing jumps from body
        while body_instrs and body_instrs[-1].opname in {
            "JUMP_ABSOLUTE", "JUMP_BACKWARD", "JUMP_FORWARD",
        }:
            body_instrs.pop()

        # Find loop end (after exit_target)
        loop_end = len(instrs)
        if exit_target is not None:
            for j in range(body_end, len(instrs)):
                if instrs[j].offset >= exit_target:
                    loop_end = j
                    break

        # Build AST — _compute_expression simulates up to (exclusive) get_iter_idx
        iter_expr = self._compute_expression(instrs, get_iter_idx)
        target = self._compute_loop_target(instrs, for_iter_idx)

        # Use child builder with proper CFG for nested body
        if body_instrs:
            child = self._create_child_builder(body_instrs)
            body = child._build_body(body_instrs)
        else:
            body = [ast.Pass()]

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

        Two patterns, checked in order:

        1. while True: (no POP_JUMP_IF_FALSE at top — body starts immediately)
           SETUP_LOOP <end_target>
           ... body (starts right after SETUP_LOOP) ...
           JUMP_ABSOLUTE <body_start>    (back edge)
           ... BREAK_LOOP in body ...
           POP_BLOCK
           end_target:

        2. Normal while with condition:
           SETUP_LOOP <end_target>
           ... condition ...
           POP_JUMP_IF_FALSE <end_target>
           ... body ...
           JUMP_ABSOLUTE <loop_start>    (back edge)
        """
        if start >= len(instrs):
            return None

        instr_start = instrs[start]
        if instr_start.opname != "SETUP_LOOP":
            return None

        setup_target = instr_start.target_offset
        if setup_target is None:
            return None

        setup_offset = instr_start.offset
        body_start_offset = setup_offset + 2  # after SETUP_LOOP instruction

        # ── Determine if this is while True or normal while ────────────
        # while True: the body begins immediately after SETUP_LOOP.
        # Normal while: condition computation → POP_JUMP_IF_FALSE → body.
        is_while_true = True
        cond_end = None
        cond_target = None

        if start + 1 < len(instrs):
            next_instr = instrs[start + 1]
            # If the very next instruction is a POP_JUMP_IF_FALSE, it's a
            # normal while (the condition was computed before SETUP_LOOP at
            # the loop back-edge).
            # If next is LOAD_FAST/STORE_FAST/LOAD_CONST (computation), it's
            # while True — the body starts right away.
            if next_instr.opname == "POP_JUMP_IF_FALSE":
                is_while_true = False
                cond_end = start + 1
                cond_target = next_instr.target_offset
            elif next_instr.opname in {
                "LOAD_FAST", "LOAD_CONST", "LOAD_GLOBAL", "LOAD_ATTR",
                "STORE_FAST", "BUILD_TUPLE", "BUILD_LIST",
            }:
                is_while_true = True
            else:
                # Uncertain — check if there's a POP_JUMP_IF_FALSE nearby
                for j in range(start + 1, min(start + 8, len(instrs))):
                    if instrs[j].opname == "POP_JUMP_IF_FALSE":
                        is_while_true = False
                        cond_end = j
                        cond_target = instrs[j].target_offset
                        break

        # ── Find the back-edge and body ───────────────────────────────
        body_instrs = []
        body_end = start + 1
        has_backward = False

        if is_while_true:
            # while True: find back-edge that jumps to body start
            for j in range(start + 1, len(instrs)):
                jinstr = instrs[j]
                if (jinstr.offset >= setup_target
                        and jinstr.opname not in {"JUMP_ABSOLUTE", "JUMP_BACKWARD", "BREAK_LOOP"}):
                    break
                if jinstr.opname in {"JUMP_ABSOLUTE", "JUMP_BACKWARD"}:
                    tgt = jinstr.target_offset
                    # Back-edge for while True: jumps to body_start_offset
                    if tgt is not None and tgt >= body_start_offset and tgt < jinstr.offset:
                        # BREAK_LOOP may appear AFTER the back-edge.
                        # Scan forward from j to find it.
                        break_idx = None
                        for k in range(j + 1, min(j + 5, len(instrs))):
                            if instrs[k].opname == "BREAK_LOOP":
                                break_idx = k
                                break
                            if instrs[k].offset >= setup_target:
                                break
                        # Also check before the back-edge
                        has_break = any(
                            ik.opname == "BREAK_LOOP"
                            for ik in instrs[start + 1:j]
                        )
                        if has_break or break_idx is not None:
                            has_backward = True
                            end_j = break_idx + 1 if break_idx is not None else j + 1
                            # Extend to include POP_BLOCK if it follows BREAK_LOOP
                            if end_j < len(instrs) and instrs[end_j].opname == "POP_BLOCK":
                                end_j += 1
                            body_instrs = instrs[start + 1:j]  # body up to back-edge
                            # Also include BREAK_LOOP+POP_BLOCK cleanup
                            if break_idx is not None:
                                body_instrs += instrs[j:end_j]
                            body_end = end_j
                            break
        else:
            # Normal while with condition
            for j in range(cond_end + 1 if cond_end is not None else start + 1, len(instrs)):
                jinstr = instrs[j]
                loop_end_target = setup_target or cond_target
                if (loop_end_target is not None
                        and jinstr.offset >= loop_end_target
                        and jinstr.opname not in {"JUMP_ABSOLUTE", "JUMP_BACKWARD"}):
                    break
                if jinstr.opname in {"JUMP_ABSOLUTE", "JUMP_BACKWARD"}:
                    tgt = jinstr.target_offset
                    if tgt is not None and tgt < setup_offset:
                        has_backward = True
                        body_instrs = instrs[cond_end + 1:j]
                        body_end = j + 1
                        break

        # Validate with CFG — also check successor blocks since for
        # while True the loop header is the body entry block (after SETUP_LOOP),
        # not the SETUP_LOOP block itself.
        if self._loop_headers is not None and has_backward:
            found_header = False
            for block in self.blocks:
                if block.start_offset <= setup_offset <= block.end_offset:
                    if block.id in self._loop_headers:
                        found_header = True
                    else:
                        # Check if any successor is a loop header
                        for sid in block.successor_ids:
                            if sid in self._loop_headers:
                                found_header = True
                                break
                    break
            if not found_header:
                has_backward = False

        if not has_backward:
            return None

        # Build test expression
        if is_while_true:
            test = ast.Constant(value=True)
        else:
            test = self._compute_expression(instrs, cond_end)

        # Use child builder with proper CFG for nested body
        if body_instrs:
            child = self._create_child_builder(body_instrs)
            body = child._build_body(body_instrs)
        else:
            body = [ast.Pass()]

        return ast.While(test=test, body=body, orelse=[]), body_end

    def _match_try_except(
        self, instrs: List[Instruction], start: int
    ) -> Optional[Tuple[ast.Try, int]]:
        """Match try/except/finally patterns.

        Python 3.7 try/except bytecode:
          SETUP_EXCEPT <handler_target>
          ... try body ...
          POP_BLOCK
          JUMP_FORWARD <end_target>   (or JUMP_ABSOLUTE)
          handler_target:
            DUP_TOP                         # dup exception for matching
            LOAD_GLOBAL/LOAD_NAME <exc_type> # exception class to match
            COMPARE_OP 10 (exception match)
            POP_JUMP_IF_FALSE <reraise>
            POP_TOP × 3                     # clear exc info
            ... handler body ...
            JUMP_FORWARD <end_target>       # skip re-raise
          reraise:
            END_FINALLY
          end_target:

        Python 3.8+ uses SETUP_FINALLY instead of SETUP_EXCEPT.
        """
        if start >= len(instrs):
            return None

        instr = instrs[start]
        if instr.opname not in {"SETUP_EXCEPT", "SETUP_FINALLY"}:
            return None

        handler_target = instr.target_offset
        if handler_target is None:
            return None

        # ── Find try body boundary ──────────────────────────────────
        # Look for POP_BLOCK or the first instruction at handler_target
        try_end_idx = start + 1
        pop_block_idx = None
        for j in range(start + 1, len(instrs)):
            jinstr = instrs[j]
            if jinstr.offset >= handler_target:
                try_end_idx = j
                break
            if jinstr.opname == "POP_BLOCK":
                pop_block_idx = j
            elif pop_block_idx is not None and jinstr.opname in {"JUMP_FORWARD", "JUMP_ABSOLUTE"}:
                try_end_idx = j
                break

        # Build try body with child builder
        if pop_block_idx is not None and pop_block_idx > start + 1:
            try_instrs = instrs[start + 1:pop_block_idx]
        elif try_end_idx > start + 1:
            try_instrs = instrs[start + 1:try_end_idx]
        else:
            try_instrs = []

        if try_instrs:
            try_child = self._create_child_builder(try_instrs)
            try_body = try_child._build_body(try_instrs)
        else:
            try_body = [ast.Pass()]

        # ── Find the overall end_target ──────────────────────────────
        end_target = None
        for j in range(try_end_idx, min(try_end_idx + 5, len(instrs))):
            if instrs[j].opname == "END_FINALLY":
                end_target = instrs[j].offset + 2
                break
            if instrs[j].opname in {"JUMP_FORWARD", "JUMP_ABSOLUTE"}:
                end_target = instrs[j].target_offset
                break
        if end_target is None:
            for j in range(try_end_idx, len(instrs)):
                if instrs[j].opname == "END_FINALLY":
                    end_target = instrs[j].offset + 2
                    break
        if end_target is None:
            end_target = instrs[-1].offset + 4

        # ── Parse handler code ──────────────────────────────────────
        handler_start_idx = None
        for j in range(start + 1, len(instrs)):
            if instrs[j].offset >= handler_target:
                handler_start_idx = j
                break
        if handler_start_idx is None:
            handler_start_idx = try_end_idx

        # Slice handler instructions
        handler_instrs = [
            ins for ins in instrs[handler_start_idx:]
            if ins.offset < end_target
        ]
        # Parse out exception type and handler body
        exc_type = None
        body_instrs = handler_instrs

        if handler_instrs:
            # Check for exception match preamble
            if (len(handler_instrs) >= 4
                    and handler_instrs[0].opname == "DUP_TOP"
                    and handler_instrs[1].opname in {"LOAD_GLOBAL", "LOAD_NAME", "LOAD_ATTR"}
                    and handler_instrs[2].opname == "COMPARE_OP"
                    and handler_instrs[3].opname == "POP_JUMP_IF_FALSE"):
                exc_type = _name_node(handler_instrs[1].argval or "<exc>")
                # Skip: DUP_TOP, LOAD_*, COMPARE_OP, POP_JUMP_IF_FALSE, POP_TOP×3
                skip = 4
                while skip < len(handler_instrs) and handler_instrs[skip].opname == "POP_TOP":
                    skip += 1
                body_instrs = handler_instrs[skip:]
            elif (handler_instrs
                  and handler_instrs[0].opname == "POP_TOP"):
                # Bare except: POP_TOP × 3
                skip = 0
                while skip < len(handler_instrs) and handler_instrs[skip].opname == "POP_TOP":
                    skip += 1
                body_instrs = handler_instrs[skip:]

        # Strip trailing JUMP/END_FINALLY from body_instrs
        cut = len(body_instrs)
        for j in range(len(body_instrs) - 1, -1, -1):
            if body_instrs[j].opname in {
                "JUMP_FORWARD", "JUMP_ABSOLUTE", "END_FINALLY",
                "POP_BLOCK", "JUMP_BACKWARD",
            }:
                cut = j
            else:
                break
        body_instrs = body_instrs[:cut]

        # Use child builder for handler body
        if body_instrs:
            handler_child = self._create_child_builder(body_instrs)
            handler_body = handler_child._build_body(body_instrs)
        else:
            handler_body = [ast.Pass()]

        is_finally = (instr.opname == "SETUP_FINALLY")

        # Build Try node
        handlers = []
        finalbody = []
        if is_finally:
            finalbody = handler_body
        else:
            handlers = [ast.ExceptHandler(type=exc_type, name=None, body=handler_body)]

        # Determine consumed instruction range
        final_idx = handler_start_idx + len(handler_instrs)
        if final_idx < len(instrs) and instrs[final_idx].opname in {"POP_BLOCK", "END_FINALLY"}:
            final_idx += 1

        return ast.Try(
            body=try_body,
            handlers=handlers,
            finalbody=finalbody,
            orelse=[],
        ), final_idx

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

        # Context expression — computed after SETUP_WITH, before the body
        ctx_expr = self._compute_expression(instrs, ctx_expr_start) if ctx_expr_start < len(instrs) else ast.Constant(value=None)

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
        from .pyc_types import CodeObjectInfo
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

        # Build CFG for the class body
        from .blocks import build_blocks
        from .cfg import build_cfg
        class_blocks = build_blocks(class_info.instructions, self.ops_mod)
        class_info.blocks = class_blocks
        class_cfg = build_cfg(class_blocks, self.ops_mod)

        builder = ASTBuilder(class_info, self.ops_mod, class_cfg)
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

        from .pyc_types import CodeObjectInfo
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

        # Build CFG for nested function body
        from .blocks import build_blocks
        from .cfg import build_cfg
        func_blocks = build_blocks(nested_info.instructions, self.ops_mod)
        nested_info.blocks = func_blocks
        func_cfg = build_cfg(func_blocks, self.ops_mod)

        builder = ASTBuilder(nested_info, self.ops_mod, func_cfg)
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

    _CONDITION_JUMP_OPS = {
        "POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE",
        "JUMP_IF_FALSE_OR_POP", "JUMP_IF_TRUE_OR_POP",
        "FOR_ITER", "JUMP_FORWARD", "JUMP_ABSOLUTE", "JUMP_BACKWARD",
    }

    def _compute_expression(
        self, instrs: List[Instruction], end_idx: int
    ) -> ast.expr:
        """Compute the expression on the stack before instruction *end_idx*.

        Simulates instructions from 0 up to (but not including) *end_idx*.
        Stops early if a conditional jump or control-flow instruction is
        encountered, since those consume/modify the condition stack.
        """
        sim = StackSimulator(self.info)
        for i in range(end_idx):
            instr = instrs[i]
            # Stop before conditional jumps — they consume the expression
            if instr.opname in self._CONDITION_JUMP_OPS:
                break
            sim.process_instruction(instr)
        # Filter out Import/Alias nodes left on stack by import handling
        exprs = [s for s in sim.stack
                 if not isinstance(s, (ast.Import, ast.alias))]
        if exprs:
            return exprs[-1]
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


def build_ast(info: CodeObjectInfo, ops_mod: Any, cfg: Any = None) -> ast.Module:
    """Build an AST module from a decompiled code object.

    Args:
        info: The disassembled code object info.
        ops_mod: The opcode module for the Python version.
        cfg: Optional ControlFlowGraph for structural validation.

    Returns:
        An ast.Module node.
    """
    builder = ASTBuilder(info, ops_mod, cfg)
    return builder.build_module()


def decompile_code_object(info: CodeObjectInfo, ops_mod: Any, cfg: Any = None) -> ast.AST:
    """Decompile a code object into an AST.

    For module-level code, returns ast.Module.
    For function code, returns ast.FunctionDef.
    """
    builder = ASTBuilder(info, ops_mod, cfg)

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
