"""Command-line interface for the Python decompiler."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Tuple

from .scanner import scan_directory, scan_single_file
from .loader import load_pyc
from .magics import format_version, get_python_version
from .opcodes import get_opcode_module
from .disassembler import disassemble_all
from .blocks import build_blocks
from .cfg import build_cfg
from .ast_builder import build_ast
from .codegen import generate_source
from .writer import ProjectWriter, print_summary
from .pyc_types import ModuleInfo, DecompileResult


def decompile_file(
    pyc_path: str,
    output_py_path: str,
) -> ModuleInfo:
    """Decompile a single .pyc file.

    Args:
        pyc_path: Path to the .pyc file.
        output_py_path: Path where the .py output should go.

    Returns:
        ModuleInfo with decompilation result.
    """
    module = ModuleInfo(
        source_path=output_py_path,
        pyc_path=pyc_path,
        python_version=(0, 0),
        magic_number=0,
        code=None,
    )

    try:
        # Load
        info = load_pyc(pyc_path)

        # Detect version from magic
        with open(pyc_path, "rb") as f:
            import struct
            magic = struct.unpack_from("<I", f.read(4))[0] & 0xFFFF

        version = get_python_version(magic)
        if version is None:
            module.errors.append(f"Unknown Python version (magic: {magic:#06x})")
            return module

        module.python_version = version
        module.magic_number = magic
        module.code = info

        # Get opcode module
        ops_mod = get_opcode_module(version)

        # Disassemble
        disassemble_all(info, ops_mod)

        if not info.instructions:
            module.errors.append("No instructions found (empty bytecode)")
            module.source_code = "# Empty module\n"
            return module

        # Build basic blocks
        blocks = build_blocks(info.instructions, ops_mod)
        info.blocks = blocks

        # Build CFG
        cfg = build_cfg(blocks, ops_mod)

        # Build AST — pass CFG for structural validation
        try:
            ast_node = build_ast(info, ops_mod, cfg)
            info.ast_node = ast_node
        except Exception as e:
            module.errors.append(f"AST construction failed: {e}")
            return module

        # Generate source
        try:
            source = generate_source(ast_node)
            module.source_code = source
        except Exception as e:
            module.errors.append(f"Source generation failed: {e}")
            return module

    except Exception as e:
        module.errors.append(f"Decompilation failed: {e}")

    return module


def decompile_project(
    input_path: str,
    output_dir: str,
    verbose: bool = False,
    dry_run: bool = False,
) -> DecompileResult:
    """Decompile an entire project directory.

    Args:
        input_path: Directory or single .pyc file.
        output_dir: Output directory for .py files.
        verbose: Print detailed progress.
        dry_run: Don't write files, just show what would be done.

    Returns:
        DecompileResult with all modules and statistics.
    """
    result = DecompileResult()

    # Determine if input is a directory or single file
    if os.path.isfile(input_path) and input_path.endswith(".pyc"):
        pyc_path, output_py = scan_single_file(input_path)
        if not output_py.startswith(output_dir):
            output_py = os.path.join(output_dir, os.path.basename(output_py))
        entries = [(pyc_path, output_py, "")]
    elif os.path.isdir(input_path):
        entries = scan_directory(input_path)
        # Adjust output paths to be under output_dir
        adjusted = []
        for pyc_path, py_src, rel_dir in entries:
            if rel_dir == ".":
                rel_dir = ""
            adjusted_path = os.path.join(output_dir, rel_dir, os.path.basename(py_src))
            adjusted.append((pyc_path, adjusted_path, rel_dir))
        entries = adjusted
    else:
        result.errors.append(f"Invalid input: {input_path}")
        return result

    if not entries:
        result.errors.append(f"No .pyc files found in: {input_path}")
        return result

    if verbose:
        print(f"Found {len(entries)} .pyc file(s) to decompile")

    writer = ProjectWriter(output_dir) if not dry_run else None

    for pyc_path, output_py, _ in entries:
        result.files_processed += 1

        if verbose:
            rel = os.path.relpath(pyc_path, input_path)
            print(f"  Processing: {rel}")

        module = decompile_file(pyc_path, output_py)
        result.modules.append(module)

        if module.errors:
            result.files_failed += 1
            result.errors.extend(module.errors)
            if verbose:
                for err in module.errors:
                    print(f"    Error: {err}")
        else:
            result.files_succeeded += 1

            # Write output
            if writer:
                writer.write(module)
            elif not dry_run:
                writer_temp = ProjectWriter(output_dir)
                writer_temp.write(module)

    return result


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point.

    Usage: python -m pyc_decompiler <input> <output> [options]
    """
    parser = argparse.ArgumentParser(
        prog="pyc_decompiler",
        description="Decompile Python .pyc files back to source code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m pyc_decompiler input_project/ output_project/
  python -m pyc_decompiler single_file.pyc output.py
  python -m pyc_decompiler --dry-run input_project/ output_project/
        """,
    )

    parser.add_argument(
        "input",
        help="Input .pyc file or directory containing .pyc files",
    )
    parser.add_argument(
        "output",
        help="Output .py file or directory",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without writing files",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="pyc_decompiler 0.1.0",
    )

    args = parser.parse_args(argv)

    input_path = os.path.abspath(args.input)
    output_path = os.path.abspath(args.output)

    if not os.path.exists(input_path):
        print(f"Error: Input path does not exist: {input_path}", file=sys.stderr)
        return 1

    # Run decompilation
    try:
        result = decompile_project(
            input_path,
            output_path,
            verbose=args.verbose,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        return 1

    if not args.verbose and not result.modules:
        print("No .pyc files found to decompile.")
        return 1

    print_summary(result)

    if result.files_failed > 0:
        return 1

    return 0
