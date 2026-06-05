"""Project scanner: find .pyc files and reconstruct project directory structure.

Handles two .pyc storage layouts:

1. Legacy (PEP 3147): `<dir>/foo.pyc` alongside `foo.py`
2. Modern (PEP 3147, default since 3.2): `<dir>/__pycache__/foo.cpython-3X.pyc`

Reference: PEP 3147
"""

from __future__ import annotations

import os
import re
from typing import List, Tuple


# Regex for __pycache__ pyc files: module.cpython-3X.pyc (optimization optional)
_PYCACHE_PYC_RE = re.compile(
    r"^(.+)\.cpython-(\d+)(?:\.opt-\d)?\.pyc$"
)

# Older __pycache__ pattern without cpython prefix
_PYCACHE_PYC_ALT_RE = re.compile(
    r"^(.+)\.cpython-(\d{2})(?:\.opt-\d)?\.pyc$"
)


def _is_pyc_file(filename: str) -> bool:
    """Check if a filename looks like a .pyc file."""
    return filename.endswith(".pyc")


def _reconstruct_module_name(pyc_filename: str) -> str:
    """Extract the Python module name from a .pyc filename.

    Examples:
        'foo.cpython-37.pyc' → 'foo'
        'foo.cpython-38.opt-1.pyc' → 'foo'
        'foo.pyc' → 'foo'
    """
    # Handle __pycache__ naming: module.cpython-3X(.opt-N)?.pyc
    # The regex includes the .pyc suffix
    match = _PYCACHE_PYC_RE.match(pyc_filename)
    if match:
        return match.group(1)

    # Legacy .pyc: just strip suffix
    if pyc_filename.endswith(".pyc"):
        return pyc_filename[:-4]

    return pyc_filename


def _is_package(pyc_path: str, module_name: str) -> bool:
    """Determine if a .pyc represents a package (__init__.py)."""
    return module_name == "__init__"


def _should_skip(filename: str) -> bool:
    """Check if a .pyc file should be skipped."""
    # Skip non-.pyc files
    return not filename.endswith(".pyc")


def scan_directory(
    input_dir: str,
) -> List[Tuple[str, str, str]]:
    """Scan a directory for .pyc files and determine output paths.

    Walks the input directory recursively, finds all .pyc files, and
    determines where the decompiled .py files should be written.

    Args:
        input_dir: Root directory to scan.

    Returns:
        List of (pyc_path, py_source_path, relative_dir) tuples.
        - pyc_path: absolute path to the .pyc file
        - py_source_path: absolute path where the .py should be written
        - relative_dir: relative directory from input_dir to the package dir
    """
    results: List[Tuple[str, str, str]] = []

    for root, dirs, files in os.walk(input_dir):
        dir_basename = os.path.basename(root)

        # Skip __pycache__ directories; we find .pyc inside them
        # but the output goes to the parent directory
        if dir_basename == "__pycache__":
            parent_dir = os.path.dirname(root)
            rel_dir = os.path.relpath(parent_dir, input_dir)

            for filename in files:
                if not _is_pyc_file(filename):
                    continue

                module_name = _reconstruct_module_name(filename)
                pyc_path = os.path.join(root, filename)
                py_source = os.path.join(parent_dir, f"{module_name}.py")

                results.append((pyc_path, py_source, rel_dir))
        else:
            # Legacy .pyc files (same directory as .py)
            for filename in files:
                if not filename.endswith(".pyc"):
                    continue

                module_name = _reconstruct_module_name(filename)
                if module_name == "__pycache__":
                    continue

                pyc_path = os.path.join(root, filename)
                py_source = os.path.join(root, f"{module_name}.py")
                rel_dir = os.path.relpath(root, input_dir)

                results.append((pyc_path, py_source, rel_dir))

    # Sort for deterministic output
    results.sort(key=lambda x: x[0])
    return results


def scan_single_file(pyc_path: str) -> Tuple[str, str]:
    """Handle a single .pyc file instead of a directory.

    Returns:
        (pyc_path, output_py_path)
    """
    pyc_path = os.path.abspath(pyc_path)
    pyc_dir = os.path.dirname(pyc_path)
    pyc_name = os.path.basename(pyc_path)
    module_name = _reconstruct_module_name(pyc_name)

    # If inside __pycache__, output to parent
    if os.path.basename(pyc_dir) == "__pycache__":
        parent = os.path.dirname(pyc_dir)
        output_path = os.path.join(parent, f"{module_name}.py")
    else:
        output_path = os.path.join(pyc_dir, f"{module_name}.py")

    return pyc_path, output_path
