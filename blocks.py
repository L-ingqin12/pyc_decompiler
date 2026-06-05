"""Basic block detection: partition instruction list into basic blocks.

A basic block is a maximal sequence of instructions where:
  1. Entry is only at the first instruction (via jump target or fallthrough)
  2. Exit is only at the last instruction (jump, return, raise, or fallthrough)

For Python 3.7/3.8, the key structures are:
  - SETUP_FINALLY / SETUP_EXCEPT: marks the start of a try block, jumps to handler
  - SETUP_LOOP (3.7): marks loop start
  - POP_BLOCK: ends a block established by SETUP_*
  - JUMP_ABSOLUTE: used for 'break', 'continue', loop back edges
  - JUMP_FORWARD: used for 'if', 'else' branches
"""

from __future__ import annotations

from typing import Any, List, Optional, Set, Tuple

from .types import Instruction, BasicBlock, CodeObjectInfo


class BlockBuilder:
    """Builds basic blocks from a linear instruction list."""

    def __init__(self, instructions: List[Instruction], ops_mod: Any):
        self.instructions = instructions
        self.ops_mod = ops_mod
        self.offset_to_idx: dict = {}
        for i, instr in enumerate(instructions):
            self.offset_to_idx[instr.offset] = i

    def _get_leader_offsets(self) -> Set[int]:
        """Find all instruction offsets that start a basic block.

        Leaders are:
          1. First instruction
          2. Jump/branch targets
          3. Instructions after a jump/terminator
          4. Instructions after SETUP_FINALLY / SETUP_EXCEPT (exception handler)
        """
        leaders: Set[int] = set()
        if not self.instructions:
            return leaders

        # First instruction
        leaders.add(self.instructions[0].offset)

        for i, instr in enumerate(self.instructions):
            # Exception handler targets
            if instr.opname in {"SETUP_FINALLY", "SETUP_EXCEPT",
                               "SETUP_WITH", "SETUP_ASYNC_WITH"}:
                target = instr.target_offset
                if target is not None and target >= 0:
                    leaders.add(target)
                # If the setup falls through, the handler starts right after
                # (for SETUP_EXCEPT the handler is at the target; for
                # SETUP_FINALLY the body continues inline and handler at target)

            # Jump targets
            if instr.is_jump or instr.is_conditional_jump:
                target = instr.target_offset
                if target is not None and target >= 0:
                    leaders.add(target)

            # Instruction after a terminator (fallthrough from
            # unconditional jump, return, raise)
            if not instr.falls_through:
                if i + 1 < len(self.instructions):
                    leaders.add(self.instructions[i + 1].offset)

            # Instructions after conditional jumps are also leaders
            # (they represent the fallthrough path)
            if instr.is_conditional_jump:
                if i + 1 < len(self.instructions):
                    leaders.add(self.instructions[i + 1].offset)

            # SETUP_* blocks: the handler and the code after the setup
            # both start new blocks
            if instr.opname in {"SETUP_FINALLY", "SETUP_EXCEPT",
                               "SETUP_WITH", "SETUP_ASYNC_WITH"}:
                if i + 1 < len(self.instructions):
                    leaders.add(self.instructions[i + 1].offset)

        return leaders

    def build(self) -> List[BasicBlock]:
        """Partition instructions into basic blocks.

        Returns:
            List of BasicBlock objects in instruction order.
        """
        if not self.instructions:
            return []

        leaders = self._get_leader_offsets()
        leaders_sorted = sorted(leaders)

        blocks: List[BasicBlock] = []
        block_id = 0

        for i, start_offset in enumerate(leaders_sorted):
            # Find end of block: this block goes up to the next leader
            if i + 1 < len(leaders_sorted):
                end_offset = leaders_sorted[i + 1]
            else:
                end_offset = None  # last block goes to end

            # Collect instructions in [start_offset, end_offset)
            block_instructions: List[Instruction] = []
            for instr in self.instructions:
                if instr.offset < start_offset:
                    continue
                if end_offset is not None and instr.offset >= end_offset:
                    break
                block_instructions.append(instr)

            if block_instructions:
                block = BasicBlock(
                    id=block_id,
                    instructions=block_instructions,
                    is_entry=(block_id == 0),
                    is_exit=False,
                )
                blocks.append(block)
                block_id += 1

        # Mark blocks that end with RETURN_VALUE or RAISE as exit blocks
        for block in blocks:
            last = block.last_instruction
            if last and (last.is_return or last.is_raise):
                block.is_exit = True

        return blocks


def build_blocks(instructions: List[Instruction], ops_mod: Any) -> List[BasicBlock]:
    """Convenience function to build basic blocks."""
    return BlockBuilder(instructions, ops_mod).build()
