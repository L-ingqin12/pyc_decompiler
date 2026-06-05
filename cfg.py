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

    def immediate_dominators(self) -> Dict[int, Optional[int]]:
        """Compute the immediate dominator for each block.

        Returns a dict mapping block_id → immediate_dominator_id.
        The entry block's immediate dominator is None.
        """
        dom = self._compute_dominators()
        idom: Dict[int, Optional[int]] = {}

        for block in self.blocks:
            bid = block.id
            if block.is_entry:
                idom[bid] = None
            else:
                # Immediate dominator = the node in dom[bid]-{bid}
                # that is dominated by all other nodes in dom[bid]
                candidates = dom[bid] - {bid}
                if not candidates:
                    # Should not happen for well-structured code;
                    # fall back to first predecessor
                    if block.predecessor_ids:
                        idom[bid] = block.predecessor_ids[0]
                    else:
                        idom[bid] = None
                elif len(candidates) == 1:
                    idom[bid] = next(iter(candidates))
                else:
                    # Pick the candidate that is dominated by all others
                    found = False
                    for c in candidates:
                        if all(d == c or c in dom.get(d, set())
                               for d in candidates):
                            idom[bid] = c
                            found = True
                            break
                    if not found:
                        idom[bid] = next(iter(candidates))
        return idom

    def post_dominators(self) -> Dict[int, Set[int]]:
        """Compute post-dominator sets for each block.

        A block P post-dominates block B if all paths from B to the exit
        must go through P. Computed by running dominator analysis on the
        reverse CFG (all edges reversed).
        """
        if not self.blocks:
            return {}

        # Find exit blocks (blocks with no successors)
        exit_blocks = {b.id for b in self.blocks
                       if not b.successor_ids or b.is_exit}

        all_blocks = {b.id for b in self.blocks}

        # Build reverse CFG: for each edge A→B, add reverse edge B→A
        reverse_preds: Dict[int, Set[int]] = {}
        for b in self.blocks:
            if b.id not in reverse_preds:
                reverse_preds[b.id] = set()
            for succ_id in b.successor_ids:
                if succ_id not in reverse_preds:
                    reverse_preds[succ_id] = set()
                reverse_preds[succ_id].add(b.id)

        # If no exit blocks found, use the last block
        actual_exits = exit_blocks or {self.blocks[-1].id}

        # Iterative dataflow on reverse graph
        pdom: Dict[int, Set[int]] = {}
        for b in self.blocks:
            if b.id in actual_exits:
                pdom[b.id] = {b.id}
            else:
                pdom[b.id] = all_blocks.copy()

        changed = True
        while changed:
            changed = False
            for b in self.blocks:
                if b.id in actual_exits:
                    continue
                succs = b.successor_ids
                if not succs:
                    # Dead-end block: skip or set to itself
                    new_pdom = {b.id}
                else:
                    new_pdom = all_blocks.copy()
                    for sid in succs:
                        new_pdom &= pdom.get(sid, {sid})
                new_pdom.add(b.id)
                if new_pdom != pdom[b.id]:
                    pdom[b.id] = new_pdom
                    changed = True

        return pdom

    def get_loop_body(self, header_id: int) -> Set[int]:
        """Return the set of block IDs in the natural loop of a header.

        Natural loop: all blocks dominated by the header that have a path
        to at least one predecessor of the header without going through
        the header itself.
        """
        if header_id >= len(self.blocks):
            return set()

        header = self.blocks[header_id]
        dom = self._compute_dominators()

        # Find latch blocks: predecessors of header dominated by header
        latches = {pid for pid in header.predecessor_ids
                   if header_id in dom.get(pid, set())}

        if not latches:
            return set()

        # Breadth-first backward search from latches
        # All blocks reached without going through header
        body: Set[int] = set()
        worklist = list(latches)
        visited = set()
        while worklist:
            bid = worklist.pop()
            if bid in visited or bid == header_id:
                continue
            visited.add(bid)
            # Must be dominated by header to be in the loop
            if bid in dom and header_id in dom[bid]:
                body.add(bid)
                block = self.blocks[bid]
                for pid in block.predecessor_ids:
                    if pid not in visited and pid != header_id:
                        worklist.append(pid)

        return body


def build_cfg(blocks: List[BasicBlock], ops_mod: Any) -> ControlFlowGraph:
    """Build a control flow graph from basic blocks."""
    cfg = ControlFlowGraph(blocks, ops_mod)
    cfg.build_edges()
    cfg.mark_loop_headers()
    return cfg
