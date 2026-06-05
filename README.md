# pyc_decompiler — Python Bytecode Decompiler

A cross-version Python `.pyc` decompiler that reconstructs source code from
compiled bytecode. Supports Python 3.7, 3.8, and 3.12 bytecode formats.

## Features

- **Cross-version support** — decompile `.pyc` files from Python 3.7 / 3.8 / 3.12
  regardless of the host Python version
- **Project-level decompilation** — reconstruct entire project directory structures
  from `__pycache__` directories
- **Structural recovery** — recovers functions, classes, if/elif/else, for/while
  loops, try/except/finally, with statements, imports, and expressions
- **Cross-version marshal reader** — delegates unmarshaling to the matching Python
  interpreter when host version differs from `.pyc` version

## Installation

```bash
# Clone the repository
git clone https://github.com/L-ingqin12/pyc_decompiler.git
cd pyc_decompiler

# No additional dependencies required (stdlib only)
# Requires Python 3.12+ to run

# For cross-version decompilation, install target Python versions:
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt install python3.7 python3.8
```

## Usage

### Command Line

```bash
# Decompile a single .pyc file
python -m pyc_decompiler input.pyc output.py

# Decompile an entire project directory
python -m pyc_decompiler project_with_pyc/ output_project/

# Preview without writing files
python -m pyc_decompiler --dry-run project_with_pyc/ output/

# Verbose output
python -m pyc_decompiler --verbose project_with_pyc/ output/
```

### Python API

```python
from pyc_decompiler.cli import decompile_file, decompile_project

# Decompile a single file
result = decompile_file("module.cpython-37.pyc", "module.py")
print(result.source_code)

# Decompile a project
result = decompile_project("input_dir/", "output_dir/")
print(f"Success: {result.files_succeeded}/{result.files_processed}")
```

## Architecture

The decompilation pipeline has 7 stages:

```
.pyc file
   │
   ▼
┌─────────────┐
│ 1. Loader    │  Parse .pyc header (magic, flags, timestamp, size)
│              │  Extract code object via marshal or cross-version subprocess
└──────┬───────┘
       ▼
┌─────────────┐
│ 2. Magic     │  Detect Python version from magic number (e.g. 3394→3.7)
│    Detection │  Route to correct opcode table
└──────┬───────┘
       ▼
┌─────────────┐
│ 3. Disasm    │  Decode wordcode (2 bytes/instruction)
│              │  Resolve EXTENDED_ARG, jump targets, variable names
└──────┬───────┘
       ▼
┌─────────────┐
│ 4. Blocks    │  Partition instructions into basic blocks
│              │  Identify leaders by jump targets and terminators
└──────┬───────┘
       ▼
┌─────────────┐
│ 5. CFG       │  Build control flow graph with edges
│              │  Mark loop headers via dominator analysis
└──────┬───────┘
       ▼
┌─────────────┐
│ 6. AST       │  Pattern-match bytecode sequences to Python constructs
│  Builder     │  Stack simulator recovers expression trees
│              │  Builds ast.Module / ast.FunctionDef / ast.ClassDef
└──────┬───────┘
       ▼
┌─────────────┐
│ 7. Codegen   │  ast.unparse() → formatted Python source
│              │  Fallback pretty-printer for edge cases
└─────────────┘
```

### Cross-Version Marshal Reader (`xmarshal.py`)

Python's `marshal` format is **not backward-compatible** across major versions.
For example, Python 3.12's `marshal.loads()` cannot read code objects marshaled
by Python 3.7 or 3.8.

**Solution**: When the host Python version differs from the `.pyc` version, the
loader launches the **matching Python interpreter** as a subprocess to unmarshal
the code object. The extracted metadata (bytecode, variable names, constants,
nested code objects) is serialized to JSON with base64-encoded binary data and
sent back to the host process. This avoids the complexity of implementing a
version-specific marshal parser for every target version.

```
Host (3.12)                    Subprocess (3.7)
    │                               │
    │  python3.7 -c "extract.pyc"   │
    │──────────────────────────────>│
    │                               │ marshal.loads(data[16:])
    │                               │ recursive code object walk
    │   JSON {co_name, co_code_b64, │
    │         co_consts, nested...} │
    │<──────────────────────────────│
    │                               │
    │  _dict_to_codeinfo(json)      │
    │  → CodeObjectInfo tree        │
```

### Bytecode Pattern Matching

The AST Builder uses pattern matching on instruction sequences to recognize
Python constructs. Key patterns:

| Construct | 3.7/3.8 Bytecode Pattern |
|-----------|--------------------------|
| `if cond: body` | `POP_JUMP_IF_FALSE <else>` → body → else target |
| `while cond: body` | `POP_JUMP_IF_FALSE <exit>` → body → `JUMP_ABSOLUTE` backedge |
| `for x in iter:` | `GET_ITER` → `FOR_ITER <exit>` → `STORE_FAST` → body |
| `try/except` | `SETUP_EXCEPT <handler>` → body → `POP_BLOCK` → handler |
| `try/finally` | `SETUP_FINALLY <handler>` → body → handler |
| `with ctx:` | `SETUP_WITH <exit>` → body → `WITH_CLEANUP` |
| `def f():` | `LOAD_CONST <code>` → `LOAD_CONST 'name'` → `MAKE_FUNCTION` |
| `class C:` | `LOAD_BUILD_CLASS` → `LOAD_CONST <code>` → ... → `CALL_FUNCTION` |
| `import X` | `LOAD_CONST 0` → `LOAD_CONST None` → `IMPORT_NAME` |
| `from X import Y` | `LOAD_CONST 0` → `LOAD_CONST (Y,)` → `IMPORT_NAME` → `IMPORT_FROM` |

### Stack Simulator (`stack_sim.py`)

The stack simulator maintains a **symbolic stack** of AST expression nodes.
Each bytecode instruction pops operands and pushes results:

- `LOAD_CONST` → pushes `ast.Constant(value)`
- `LOAD_FAST` → pushes `ast.Name(id)`
- `BINARY_ADD` → pops right/left, pushes `ast.BinOp(left, Add, right)`
- `STORE_FAST` → pops value, emits `ast.Assign([Name], value)`
- `CALL_FUNCTION` → pops args/function, pushes `ast.Call(func, args)`

Jump instructions and control flow are handled by the AST Builder at a higher
level; the stack simulator only handles expression evaluation within a single
basic block.

## Supported Python Versions

| Target Version | Magic Range | Status |
|---------------|-------------|--------|
| 3.7 | 3390–3394 | ✅ Supported |
| 3.8 | 3410–3413 | ✅ Supported |
| 3.9 | 3420–3425 | ⚠️ Magic recognized, opcodes not implemented |
| 3.10 | 3430–3439 | ⚠️ Magic recognized, opcodes not implemented |
| 3.11 | 3450–3459 | ⚠️ Magic recognized, opcodes not implemented |
| 3.12 | 3469–3482 | ✅ Supported |
| 3.13 | 3490–3493 | ⚠️ Magic recognized, opcodes not implemented |

## Limitations

- **Compound conditions** (`and`/`or`) are not fully reconstructed as `BoolOp` nodes
- **Comprehensions** (list/dict/set/genexpr) may produce invalid function names
- **Decorators** are not detected (returned as empty lists)
- **Deeply nested** class method bodies may not be fully reconstructed
- Source comments, docstrings, and formatting are **lost** (bytecode doesn't store them)
- Numeric literal formatting (`100_000` vs `100000`) is not preserved

## Project Structure

```
pyc_decompiler/
├── __init__.py        # Package entry
├── __main__.py        # CLI entry: python -m pyc_decompiler
├── cli.py             # CLI argument parsing, orchestration
├── loader.py          # .pyc header parsing, marshal loading
├── xmarshal.py        # Cross-version marshal reader (subprocess delegation)
├── magics.py          # Magic number → Python version mapping
├── opcodes/
│   ├── __init__.py    # Opcode registry (selects table by version)
│   ├── base.py        # Shared constants (CMP_OP, BINARY_OPS, etc.)
│   ├── py37.py        # Python 3.7 opcode table
│   ├── py38.py        # Python 3.8 opcode table
│   └── py312.py       # Python 3.12 opcode table (with specialized opcodes)
├── disassembler.py    # Bytecode → Instruction list
├── blocks.py          # Basic block partitioning
├── cfg.py             # Control flow graph construction
├── stack_sim.py       # Symbolic stack simulator for expression recovery
├── ast_builder.py     # Pattern matching & AST construction engine
├── codegen.py         # AST → Python source (ast.unparse + fallback)
├── scanner.py         # Directory scanning for .pyc files
├── writer.py          # Output writer with project structure preservation
└── types.py           # Data types (Instruction, BasicBlock, CodeObjectInfo, etc.)
```

## License

MIT
