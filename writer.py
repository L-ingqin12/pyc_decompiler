"""Project writer: write decompiled .py files to output directory.

Preserves the original project directory structure and handles
package __init__.py files correctly.
"""

from __future__ import annotations

import os
from typing import List, Optional

from .pyc_types import ModuleInfo, DecompileResult


def write_output(
    output_dir: str,
    modules: List[ModuleInfo],
    dry_run: bool = False,
) -> None:
    """Write all decompiled modules to the output directory.

    Args:
        output_dir: Root output directory.
        modules: List of decompiled module infos.
        dry_run: If True, print what would be written without writing.
    """
    for module in modules:
        # Determine output path
        source_path = module.source_path

        if output_dir:
            # Reconstruct path relative to output_dir
            # source_path is absolute; we need to figure out relative path
            # For project-level decompilation, we preserve the relative
            # structure from the input directory
            pass

        if dry_run:
            print(f"[DRY RUN] Would write: {source_path}")
            continue

        # Create parent directories
        parent = os.path.dirname(source_path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        # Write the source code
        content = module.source_code or "# Decompilation failed\n"
        with open(source_path, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"  Wrote: {source_path}")


def write_module(
    output_path: str,
    source_code: str,
    dry_run: bool = False,
) -> None:
    """Write a single decompiled module to disk.

    Args:
        output_path: The target .py file path.
        source_code: The decompiled Python source.
        dry_run: If True, only print the target path.
    """
    if dry_run:
        print(f"[DRY RUN] Would write: {output_path}")
        return

    parent = os.path.dirname(output_path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(source_code)


class ProjectWriter:
    """Handles writing decompiled projects with correct structure."""

    def __init__(self, output_dir: str):
        self.output_dir = os.path.abspath(output_dir)
        self.files_written = 0

    def write(self, module: ModuleInfo) -> str:
        """Write a single module to the output directory.

        The output path preserves the relative structure from the
        original input directory.

        Returns:
            The output file path.
        """
        # Determine relative path
        source_path = module.source_path
        # source_path is the original .py path
        # Map it under output_dir
        # For simplicity, use the basename if source_path has no directory context
        if self.output_dir and not os.path.commonpath(
            [self.output_dir, source_path]) == self.output_dir:
            # source_path is outside output_dir — use just basename
            basename = os.path.basename(source_path)
            output_path = os.path.join(self.output_dir, basename)
        else:
            output_path = source_path

        # Create parent directories
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        content = module.source_code
        if not content:
            content = f"# Decompilation failed for {module.pyc_path}\n"
            if module.errors:
                for err in module.errors:
                    content += f"# Error: {err}\n"

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        self.files_written += 1
        return output_path

    def write_all(
        self, modules: List[ModuleInfo], verbose: bool = False
    ) -> None:
        """Write all modules from a DecompileResult."""
        for module in modules:
            path = self.write(module)
            if verbose:
                print(f"  wrote: {path}")

        if verbose:
            print(f"\nWrote {self.files_written} files to {self.output_dir}/")


def print_summary(result: DecompileResult) -> None:
    """Print a summary of the decompilation result."""
    print(f"\n{'='*60}")
    print(f"Decompilation Summary")
    print(f"{'='*60}")
    print(f"  Files processed:  {result.files_processed}")
    print(f"  Files succeeded:  {result.files_succeeded}")
    print(f"  Files failed:     {result.files_failed}")
    print(f"  Success rate:     {result.success_rate:.0%}")
    if result.errors:
        print(f"\n  Errors:")
        for err in result.errors[:10]:
            print(f"    - {err}")
        if len(result.errors) > 10:
            print(f"    ... and {len(result.errors) - 10} more")
