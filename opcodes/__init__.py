"""Opcode registry: selects the correct opcode table per Python version."""

from __future__ import annotations

from typing import Any, Tuple

from . import py37, py38, py312


def get_opcode_module(version: Tuple[int, int]) -> Any:
    """Return the opcode module for a given Python version.

    Args:
        version: (major, minor) tuple, e.g., (3, 7).

    Returns:
        The opcode module (with .opname, .opcode, .has_arg, etc. dicts).

    Raises:
        ValueError: if the version is not supported.
    """
    if version == (3, 7):
        return py37
    elif version == (3, 8):
        return py38
    elif version in ((3, 12), (3, 13)):
        return py312
    else:
        available = [(3, 7), (3, 8), (3, 12)]
        if version[0] == 3 and version[1] > 8:
            # For 3.9+, try the closest supported version
            raise ValueError(
                f"Python {version[0]}.{version[1]} is not yet supported. "
                f"Available versions: {available}. "
                f"Please upgrade the decompiler for newer Python versions."
            )
        raise ValueError(
            f"Python {version[0]}.{version[1]} is not supported. "
            f"Available versions: {available}."
        )


def has_instruction(opcode_mod: Any, name: str) -> bool:
    """Check if an opcode by name exists in this version."""
    return name in opcode_mod.opname
