# CLAUDE.md — pyc_decompiler

Python bytecode decompiler. Reconstructs `.py` source from `.pyc` files.
Supports Python 3.7, 3.8, and 3.12 bytecode targets, running on Python 3.12 host.

## Commands

```bash
# Run decompiler
python -m pyc_decompiler <input.pyc|dir> <output.py|dir> [--verbose] [--dry-run]

# Type-check (Pyright)
pyright *.py opcodes/*.py

# Test round-trip (compile + decompile + diff against original)
python3.7 -m compileall -f project/      # compile with target version
python -m pyc_decompiler project/ out/    # decompile
```

No pip dependencies required (stdlib only). Type-checking uses Pyright.

## Architecture — 7-stage pipeline

```
.pyc → loader → disassembler → blocks → CFG → AST builder → codegen → .py
```

### 1. loader.py — parse .pyc header + marshal
- Reads 16-byte PEP 552 header: magic, flags, timestamp/hash, source_size
- **Same-version path**: `marshal.loads(data[16:])` directly
- **Cross-version path**: delegates to `xmarshal.py` which runs the matching
  Python interpreter via subprocess to unmarshal, returns JSON with base64
  bytecode. This exists because Python's marshal format is NOT
  backward-compatible — 3.12 cannot read 3.7/3.8 marshal data.

### 2. magics.py + opcodes/*.py — version detection + opcode tables
- `magics.py`: magic number → (major, minor) via exact match then range fallback
- `opcodes/__init__.py`: routes (3,7)→py37, (3,8)→py38, (3,12)→py312
- Each opcode module exports: `opname` (name→num), `opcode` (num→name),
  `has_arg`, `JUMP_RELATIVE`, `JUMP_ABSOLUTE_SET`, `JUMP_CONDITIONAL`,
  `TERMINATOR_OPS`, `JUMP_OPS`

### 3. disassembler.py — bytecode → Instruction list
- Wordcode format: 2 bytes per instruction (opcode + arg)
- Handles EXTENDED_ARG for args > 255
- Resolves arguments: var names from `co_varnames`, constants from `co_consts`,
  names from `co_names`, compare ops from `CMP_OP`
- `disassemble_all()` recursively processes nested code objects

### 4. blocks.py — partition into basic blocks
- Leaders: first instruction, jump targets, fallthrough after terminators,
  exception handler targets (SETUP_FINALLY/SETUP_EXCEPT)

### 5. cfg.py — control flow graph
- Builds edges: fallthrough, unconditional jump, conditional jump (taken +
  not-taken), exception edges
- Dominator-based loop header detection

### 6. ast_builder.py — bytecode pattern matching → Python AST
- **Stack simulator** (`stack_sim.py`): symbolic stack of AST expression nodes.
  Each instruction pops operands, pushes results. `LOAD_FAST`→`Name`,
  `BINARY_ADD`→`BinOp`, `CALL_FUNCTION`→`Call`, etc.
- **Pattern matchers** (`_match_*` methods): scan instruction list for
  bytecode patterns that correspond to Python constructs. Key discriminators:
  - `while` has a backward jump (JUMP_ABSOLUTE/JUMP_BACKWARD to before cond)
  - `if` has only forward POP_JUMP_IF_FALSE with no backedge
  - `class` uses LOAD_BUILD_CLASS before MAKE_FUNCTION
  - `for` uses GET_ITER→FOR_ITER→STORE_FAST sequence
- **Function/class bodies**: nested `CodeObjectInfo` trees are recursively
  decompiled by creating a new ASTBuilder on the nested object
- **Cross-version code objects**: `CodeObjectInfo` stored directly in
  `co_consts` (not reconstructed `types.CodeType`) to avoid segfault from
  3.12's strict `CodeType` constructor validation

### 7. codegen.py — AST → source string
- Primary: `ast.unparse()` (Python 3.9+)
- Fallback: `_fallback_unparse()` for edge cases and when unparse fails
- `_fix_missing_locations()`: iterative (not recursive) traversal to set lineno
  on every node — `ast.unparse()` in 3.12 requires this

## Key invariants and gotchas

- **`types.CodeType` constructor is version-specific**: 3.11+ requires
  `posonlyargcount`, `qualname`, `exceptiontable`. 3.12 validates
  `co_nlocals == len(co_varnames)`. NEVER reconstruct `CodeType` cross-version;
  store `CodeObjectInfo` directly instead.
- **`xmarshal._EXTRACT_SCRIPT`** must run under the TARGET Python version.
  Keep it compatible with Python 3.7 syntax (no f-strings, no walrus operator).
- **`_normalize_opname()`**: maps specialized opcodes (Python 3.12) to generic
  names via `_SPECIALIZED_MAP` (opnum→generic_name) then fallback prefix
  matching. For 3.7/3.8 (no specialized opcodes), returns opname as-is.
- **Stack flushing**: after `_build_body` processes all instructions, any
  remaining stack items are flushed as expression statements. Filter these
  aggressively — skip PUSH_NULL markers, unresolved attributes (`<attr_*`),
  placeholder names (`<*>`), bare Names, bare Compares.
- **Empty bodies**: always inject `ast.Pass()` when a body list is empty.
  `ast.unparse()` produces invalid syntax for empty bodies otherwise.
- **Instruction offsets vs indices**: `Instruction.offset` is the bytecode
  offset (2× instruction index in wordcode). Jump targets are offsets.
  `target_offset` property computes absolute target from arg for each jump type.

## Types

- `Instruction`: offset, opcode, opname, arg, argval (resolved), lineno,
  is_jump_target
- `BasicBlock`: id, instructions list, successor_ids, predecessor_ids, flags
  (is_entry, is_exit, is_loop_header, is_exception_handler)
- `CodeObjectInfo`: all code object metadata + instructions, blocks, ast_node,
  nested CodeObjectInfo list
- `ModuleInfo`: source_path, pyc_path, python_version, code, source_code, errors
- `DecompileResult`: modules list, errors, files_processed/succeeded/failed
