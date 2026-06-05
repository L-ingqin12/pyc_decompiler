"""Cross-version marshal reader.

Python's marshal format is not backward-compatible across major versions.
This module provides a version-independent way to load code objects from
.pyc files by delegating to the matching Python interpreter via subprocess.
"""

from __future__ import annotations

import base64
import json
import subprocess
import types
from typing import Any, Tuple

from .types import CodeObjectInfo


def _find_python(version: Tuple[int, int]) -> str | None:
    """Find a Python interpreter matching the given version."""
    candidates = [f"python{version[0]}.{version[1]}"]
    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


# Script that runs under the target Python version to extract code object data
_EXTRACT_SCRIPT = r"""
import base64, json, marshal, sys, types as _types

def _extract(co):
    nested = []
    for const in co.co_consts:
        if isinstance(const, _types.CodeType):
            nested.append(_extract(const))
    return {
        "co_name": co.co_name,
        "co_filename": co.co_filename,
        "co_firstlineno": co.co_firstlineno,
        "co_argcount": co.co_argcount,
        "co_kwonlyargcount": co.co_kwonlyargcount,
        "co_nlocals": co.co_nlocals,
        "co_stacksize": co.co_stacksize,
        "co_flags": co.co_flags,
        "co_consts": [_encode_const(v) for v in co.co_consts],
        "co_names": list(co.co_names),
        "co_varnames": list(co.co_varnames),
        "co_cellvars": list(co.co_cellvars),
        "co_freevars": list(co.co_freevars),
        "co_lnotab_b64": base64.b64encode(co.co_lnotab).decode("ascii"),
        "raw_bytecode_b64": base64.b64encode(co.co_code).decode("ascii"),
        "nested": nested,
    }

def _encode_const(val):
    if isinstance(val, _types.CodeType):
        return {"__code__": _extract(val)}
    if isinstance(val, bytes):
        return {"__bytes__": base64.b64encode(val).decode("ascii")}
    if isinstance(val, (int, float, str, bool, type(None))):
        return val
    if isinstance(val, tuple):
        return {"__tuple__": [_encode_const(v) for v in val]}
    if isinstance(val, frozenset):
        return {"__frozenset__": [_encode_const(v) for v in val]}
    if val is ...:
        return {"__ellipsis__": True}
    if isinstance(val, complex):
        return {"__complex__": [val.real, val.imag]}
    return {"__unknown__": repr(val)}

filepath = sys.argv[1]
with open(filepath, "rb") as f:
    data = f.read()
header_size = 16
co = marshal.loads(data[header_size:])
result = _extract(co)
print(json.dumps(result))
"""


def _decode_const(val: Any) -> Any:
    """Decode a constant value from the JSON representation."""
    if isinstance(val, dict):
        if "__code__" in val:
            info = _dict_to_codeinfo(val["__code__"])
            # Return CodeObjectInfo directly instead of reconstructing
            # types.CodeType (which is fragile across Python versions).
            return info
        if "__bytes__" in val:
            return base64.b64decode(val["__bytes__"])
        if "__tuple__" in val:
            return tuple(_decode_const(v) for v in val["__tuple__"])
        if "__frozenset__" in val:
            return frozenset(_decode_const(v) for v in val["__frozenset__"])
        if "__ellipsis__" in val:
            return ...
        if "__complex__" in val:
            return complex(val["__complex__"][0], val["__complex__"][1])
        if "__unknown__" in val:
            return val["__unknown__"]
    return val


def _dict_to_codeinfo(d: dict) -> CodeObjectInfo:
    """Convert a dict (from JSON extraction) to CodeObjectInfo."""
    decoded_consts = tuple(
        _decode_const(c) for c in d["co_consts"]
    )

    nested = [
        _dict_to_codeinfo(n) for n in d.get("nested", [])
    ]

    return CodeObjectInfo(
        co_name=d["co_name"],
        co_filename=d["co_filename"],
        co_firstlineno=d["co_firstlineno"],
        co_argcount=d["co_argcount"],
        co_kwonlyargcount=d["co_kwonlyargcount"],
        co_nlocals=d["co_nlocals"],
        co_stacksize=d["co_stacksize"],
        co_flags=d["co_flags"],
        co_consts=decoded_consts,
        co_names=tuple(d["co_names"]),
        co_varnames=tuple(d["co_varnames"]),
        co_cellvars=tuple(d["co_cellvars"]),
        co_freevars=tuple(d["co_freevars"]),
        co_lnotab=base64.b64decode(d["co_lnotab_b64"]),
        raw_bytecode=base64.b64decode(d["raw_bytecode_b64"]),
        nested=nested,
    )


def _make_code_object(info: CodeObjectInfo) -> types.CodeType:
    """Reconstruct a real Python code object from CodeObjectInfo.

    Uses the host Python's types.CodeType constructor (3.11+ signature).
    Python 3.12+ validates internal invariants (e.g. nlocals == len(varnames)).
    We must ensure consistency or use a more lenient approach.
    """
    import types as _types

    consts = []
    for c in info.co_consts:
        if isinstance(c, _types.CodeType):
            consts.append(c)
        elif isinstance(c, CodeObjectInfo):
            consts.append(_make_code_object(c))
        else:
            consts.append(c)

    # Python 3.12+ requires co_nlocals == len(co_varnames).
    # If the counts don't match (can happen with reconstructed code objects
    # from older Python versions where invariants differ), pad or truncate.
    varnames = list(info.co_varnames)
    nlocals = info.co_nlocals
    while len(varnames) < nlocals:
        varnames.append(f"<local_{len(varnames)}>")
    varnames = tuple(varnames[:nlocals])

    # Python 3.12+ requires co_kwonlyargcount <= len(co_varnames) - co_argcount
    # when co_posonlyargcount == 0
    kwonlyargcount = info.co_kwonlyargcount
    max_kwonly = max(0, nlocals - info.co_argcount)
    if kwonlyargcount > max_kwonly:
        kwonlyargcount = max_kwonly

    return _types.CodeType(
        info.co_argcount,
        0,  # posonlyargcount
        kwonlyargcount,
        nlocals,
        info.co_stacksize,
        info.co_flags,
        info.raw_bytecode,
        tuple(consts),
        info.co_names,
        varnames,
        info.co_filename,
        info.co_name,
        info.co_name,  # qualname
        info.co_firstlineno,
        info.co_lnotab,
        b"",  # exceptiontable
        info.co_freevars,
        info.co_cellvars,
    )


def load_pyc_cross_version(filepath: str, version: Tuple[int, int]) -> CodeObjectInfo:
    """Load a .pyc file using a matching Python interpreter for unmarshaling.

    Args:
        filepath: Path to the .pyc file.
        version: (major, minor) tuple of the Python version that created the .pyc.

    Returns:
        CodeObjectInfo with fully populated nested code objects.

    Raises:
        RuntimeError: if no matching Python interpreter is found.
    """
    python_bin = _find_python(version)
    if python_bin is None:
        raise RuntimeError(
            f"Cannot find Python {version[0]}.{version[1]} interpreter. "
            f"Install it to decompile .pyc files from this version."
        )

    proc = subprocess.run(
        [python_bin, "-c", _EXTRACT_SCRIPT, filepath],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Python {version[0]}.{version[1]} extraction failed: "
            f"{proc.stderr.strip()}"
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Failed to parse extraction output: {e}\n"
            f"stdout: {proc.stdout[:500]}\n"
            f"stderr: {proc.stderr[:500]}"
        )

    return _dict_to_codeinfo(data)
