"""Control flow graph builder: connect basic blocks with edges.

Builds a control flow graph from basic blocks by analyzing jump instructions,
exception handling setup, and loop structures.

For Python 3.7/3.8 exception handling:
  - SETUP_FINALLY(target): establishes try/finally; handler at target
  - SETUP_EXCEPT(target): establishes try/except; handler at target
  - POP_BLOCK: ends the current try block scope
  - The block stack is implicit in bytecode; we track it syntactically

For Python 3.7 loops:
  - SETUP_LOOP(target): marks loop start; target is loop end
  - BREAK_LOOP: exits the innermost loop
  - CONTINUE_LOOP(target): jumps to loop start

For Python 3.8 loops:
  - No SETUP_LOOP; loops use JUMP_ABSOLUTE to loop back
  - BREAK_LOOP still exists for 'break'
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .types import Instruction, BasicBlock, CodeObjectInfo


class ControlFlowGraph:
    """A control flow graph for a code object."""

    def __init__(self, blocks: List[BasicBlock], ops_mod: Any):
        self.blocks = blocks
        self.ops_mod = ops_mod
        self.offset_to_block: Dict[int, int] = {}
        for block in blocks:
            self.offset_to_block[block.start_offset] = block.id

    def build_edges(self) -> None:
        """Compute successor/predecessor edges between blocks.

        Three edge types:
          1. Fallthrough: block N → block N+1 (no terminator in N)
          2. Jump: block N → target block (unconditional or conditional taken)
          3. Exception: try block → handler block
        """
        jump_abs = self.ops_mod.JUMP_ABSOLUTE_SET
        jump_rel = self.ops_mod.JUMP_RELATIVE
        jump_cond = self.ops_mod.JUMP_CONDITIONAL
        terminator_ops = self.ops_mod.TERMINATOR_OPS

        for i, block in enumerate(self.blocks):
            block.successor_ids = []
            block.predecessor_ids = []

        for i, block in enumerate(self.blocks):
            last = block.last_instruction
            if last is None:
                # Empty block — fall through to next
                if i + 1 < len(self.blocks):
                    self._add_edge(block.id, self.blocks[i + 1].id)
                continue

            opname = last.opname
            opcode = last.opcode

            # --- Unconditional jumps ---
            if opname in {"JUMP_FORWARD", "JUMP_ABSOLUTE", "JUMP_BACKWARD",
                         "BREAK_LOOP", "CONTINUE_LOOP"}:
                target = last.target_offset
                target_block = self._find_block(target)
                if target_block is not None:
                    self._add_edge(block.id, target_block)

            # --- Conditional jumps ---
            elif opcode in jump_cond:
                # Taken branch
                target = last.target_offset
                target_block = self._find_block(target)
                if target_block is not None:
                    self._add_edge(block.id, target_block)

                # Not-taken branch: fallthrough to next block
                if i + 1 < len(self.blocks):
                    self._add_edge(block.id, self.blocks[i + 1].id)

            # --- FOR_ITER ---
            elif opname == "FOR_ITER":
                # Taken: jump to loop body (iteration value loaded)
                target = last.target_offset
                target_block = self._find_block(target)
                if target_block is not None:
                    self._add_edge(block.id, target_block)
                # Not taken: iterator exhausted, fallthrough (exit loop)
                if i + 1 < len(self.blocks):
                    self._add_edge(block.id, self.blocks[i + 1].id)

            # --- RETURN_VALUE, RAISE_VARARGS, RERAISE: no successors ---
            elif opcode in terminator_ops:
                pass  # no outgoing edges

            # --- Fallthrough ---
            else:
                if i + 1 < len(self.blocks):
                    self._add_edge(block.id, self.blocks[i + 1].id)

        # --- Exception edges ---
        self._build_exception_edges()

    def _build_exception_edges(self) -> None:
        """Add edges from try blocks to their exception handlers.

        In Python 3.7/3.8 bytecode:
          SETUP_FINALLY target  — establishes try/finally; handler at target
          SETUP_EXCEPT target   — establishes try/except; handler at target

        The try body starts at the instruction after SETUP_* and continues
        until a POP_BLOCK or JUMP_FORWARD past the handler.

        For try/except, there can be multiple handlers chained.
        """
        for i, block in enumerate(self.blocks):
            for instr in block.instructions:
                if instr.opname in {"SETUP_FINALLY", "SETUP_EXCEPT",
                                    "SETUP_WITH", "SETUP_ASYNC_WITH"}:
                    target = instr.target_offset
                    handler_block = self._find_block(target)
                    if handler_block is not None:
                        # All blocks from here until POP_BLOCK can
                        # jump to the handler
                        for j in range(i, len(self.blocks)):
                            b = self.blocks[j]
                            # Stop when we hit a block that starts at
                            # or after the handler
                            if b.start_offset >= target:
                                break
                            # Don't add edge from the setup instruction's
                            # block unless it's the same as this block
                            if handler_block not in b.successor_ids:
                                self._add_edge(b.id, handler_block)

    def _add_edge(self, from_id: int, to_id: int) -> None:
        """Add a directed edge between two blocks."""
        from_block = self.blocks[from_id]
        to_block = self.blocks[to_id]
        if to_id not in from_block.successor_ids:
            from_block.successor_ids.append(to_id)
        if from_id not in to_block.predecessor_ids:
            to_block.predecessor_ids.append(from_id)

    def _find_block(self, offset: Optional[int]) -> Optional[int]:
        """Find the block ID containing the given bytecode offset."""
        if offset is None or offset < 0:
            return None
        if offset in self.offset_to_block:
            return self.offset_to_block[offset]
        # Find the block whose start is closest to but <= offset
        best = None
        for start, bid in self.offset_to_block.items():
            if start <= offset:
                if best is None or start > best:
                    best = bid
        return best

    def find_loop_headers(self) -> Set[int]:
        """Find loop headers using dominator analysis.

        A loop header has a back edge (successor dominates predecessor).
        """
        dom = self._compute_dominators()
        headers: Set[int] = set()

        for block in self.blocks:
            for succ_id in block.successor_ids:
                # Back edge: succ dominates block
                if block.id in dom.get(succ_id, set()):
                    headers.add(succ_id)

        return headers

    def _compute_dominators(self) -> Dict[int, Set[int]]:
        """Compute dominator sets for each block.

        Returns:
            dict mapping block ID → set of block IDs it dominates.
        """
        if not self.blocks:
            return {}

        entry = self.blocks[0]
        all_blocks = {b.id for b in self.blocks}

        # Each block starts dominated by all blocks (except entry only itself)
        dom: Dict[int, Set[int]] = {}
        for b in self.blocks:
            if b.id == entry.id:
                dom[b.id] = {entry.id}
            else:
                dom[b.id] = all_blocks.copy()

        changed = True
        while changed:
            changed = False
            for b in self.blocks:
                if b.id == entry.id:
                    continue
                preds = b.predecessor_ids
                if not preds:
                    new_dom = set()
                else:
                    new_dom = all_blocks.copy()
                    for pid in preds:
                        new_dom &= dom.get(pid, set())
                new_dom.add(b.id)
                if new_dom != dom[b.id]:
                    dom[b.id] = new_dom
                    changed = True

        return dom

    def mark_loop_headers(self) -> None:
        """Mark blocks that are loop headers."""
        headers = self.find_loop_headers()
        for block in self.blocks:
            if block.id in headers:
                block.is_loop_header = True


def build_cfg(blocks: List[BasicBlock], ops_mod: Any) -> ControlFlowGraph:
    """Build a control flow graph from basic blocks."""
    cfg = ControlFlowGraph(blocks, ops_mod)
    cfg.build_edges()
    cfg.mark_loop_headers()
    return cfg
