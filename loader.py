"""PYC file loader: parse header, extract code objects via marshal.

Handles the .pyc file format for Python 3.7+:
  - 4 bytes: magic number (little-endian u32)
  - 4 bytes: flags (PEP 552 hash-based .pyc)
  - 4 bytes: timestamp or hash (depends on flags)
  - 4 bytes: source file size
  - remaining: marshaled code object

Reference: PEP 552, PEP 3147

Python's marshal format is version-specific (not backward-compatible).
When the host Python version differs from the .pyc version, we delegate
unmarshaling to the matching Python interpreter via xmarshal.
"""

from __future__ import annotations

import marshal
import struct
import sys
import types
from typing import Tuple

from .magics import format_version, get_python_version, is_supported
from .pyc_types import CodeObjectInfo


class LoadError(Exception):
    """Raised when a .pyc file cannot be loaded."""


def _extract_code_info(co: types.CodeType) -> CodeObjectInfo:
    """Extract metadata and bytecode from a code object."""
    return CodeObjectInfo(
        co_name=co.co_name,
        co_filename=co.co_filename,
        co_firstlineno=co.co_firstlineno,
        co_argcount=co.co_argcount,
        co_kwonlyargcount=co.co_kwonlyargcount,
        co_nlocals=co.co_nlocals,
        co_stacksize=co.co_stacksize,
        co_flags=co.co_flags,
        co_consts=co.co_consts,
        co_names=co.co_names,
        co_varnames=co.co_varnames,
        co_cellvars=co.co_cellvars,
        co_freevars=co.co_freevars,
        co_lnotab=co.co_lnotab,
        raw_bytecode=co.co_code,
    )


def _collect_nested_code_objects(info: CodeObjectInfo) -> None:
    """Recursively find all nested code objects (functions, classes,
    comprehensions, lambdas, generators) in co_consts."""
    for const in info.co_consts:
        if isinstance(const, types.CodeType):
            nested_info = _extract_code_info(const)
            _collect_nested_code_objects(nested_info)
            info.nested.append(nested_info)


def _read_pyc_header(data: bytes) -> Tuple[int, int, int, int]:
    """Parse the 16-byte .pyc file header.

    Returns:
        (magic_word, flags, timestamp_or_hash, source_size)

    Raises:
        LoadError: if the header is too short or invalid.
    """
    if len(data) < 16:
        raise LoadError(
            f"File too short ({len(data)} bytes), expected at least 16-byte header"
        )

    magic_word = struct.unpack_from("<I", data, 0)[0] & 0xFFFF
    flags = struct.unpack_from("<I", data, 4)[0]
    timestamp_or_hash = struct.unpack_from("<I", data, 8)[0]
    source_size = struct.unpack_from("<I", data, 12)[0]

    return magic_word, flags, timestamp_or_hash, source_size


def load_pyc(filepath: str) -> CodeObjectInfo:
    """Load and parse a .pyc file.

    Args:
        filepath: Path to the .pyc file.

    Returns:
        CodeObjectInfo for the top-level module code object, with nested
        code objects populated.

    Raises:
        LoadError: if the file cannot be loaded or the version is unsupported.
    """
    with open(filepath, "rb") as f:
        data = f.read()

    if len(data) < 16:
        raise LoadError(f"File too short: {len(data)} bytes, expected >= 16")

    magic_word, _flags, _timestamp_or_hash, _source_size = _read_pyc_header(data)

    # Look up Python version (exact match + range fallback)
    version = get_python_version(magic_word)
    if version is None:
        raise LoadError(
            f"Unknown magic number: {magic_word:#06x} ({magic_word}). "
            f"The .pyc file may be from an unsupported Python version."
        )

    if not is_supported(version):
        raise LoadError(
            f"Python {format_version(version)} (magic {magic_word:#06x}) "
            f"is not yet supported. Currently supports: Python 3.7, 3.8."
        )

    # Use cross-version unmarshaling if the host Python differs from the
    # .pyc version. Python's marshal format is not backward-compatible.
    host_ver = (sys.version_info.major, sys.version_info.minor)
    if version != host_ver:
        try:
            from .xmarshal import load_pyc_cross_version
            return load_pyc_cross_version(filepath, version)
        except Exception as e:
            raise LoadError(
                f"Cross-version load failed for Python {format_version(version)}: {e}"
            ) from e

    # Unmarshal the code object from after the header (same-version path)
    header_size = 16
    try:
        co = marshal.loads(data[header_size:])
    except Exception as e:
        raise LoadError(f"Failed to unmarshal code object: {e}") from e

    if not isinstance(co, types.CodeType):
        raise LoadError(
            f"Expected a code object, got {type(co).__name__}"
        )

    # Build CodeObjectInfo tree
    info = _extract_code_info(co)
    _collect_nested_code_objects(info)

    return info


def load_pyc_metadata(filepath: str) -> Tuple[int, int, int, int, Tuple[int, int]]:
    """Read only the .pyc header without unmarshaling the code object.

    Returns:
        (magic_word, flags, timestamp_or_hash, source_size, python_version)
    """
    with open(filepath, "rb") as f:
        header = f.read(16)

    magic_word, flags, timestamp_or_hash, source_size = _read_pyc_header(header)
    version = get_python_version(magic_word)

    if version is None:
        raise LoadError(
            f"Unknown magic number: {magic_word:#06x} ({magic_word})"
        )

    return magic_word, flags, timestamp_or_hash, source_size, version
