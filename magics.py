"""Python magic number definitions for version detection.

Magic numbers are 4 bytes (2 for the magic + 2 for flags in the stored
.pyc header). We decode them as little-endian 16-bit unsigned ints and
map them to (major, minor) Python versions.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# Python version → list of magic numbers (as stored in .pyc, little-endian decoded)
# Each Python micro release potentially changes the magic number.
MAGIC_TO_VERSION: Dict[int, Tuple[int, int]] = {
    # Python 3.7
    3390: (3, 7), 3391: (3, 7), 3392: (3, 7), 3393: (3, 7), 3394: (3, 7),
    # Python 3.8
    3410: (3, 8), 3411: (3, 8), 3412: (3, 8), 3413: (3, 8),
    # Python 3.9
    3420: (3, 9), 3421: (3, 9), 3422: (3, 9), 3423: (3, 9), 3424: (3, 9),
    3425: (3, 9),
    # Python 3.10
    3430: (3, 10), 3437: (3, 10), 3438: (3, 10), 3439: (3, 10),
    # Python 3.11
    3450: (3, 11), 3451: (3, 11), 3452: (3, 11), 3453: (3, 11),
    3454: (3, 11), 3455: (3, 11), 3456: (3, 11), 3457: (3, 11),
    3458: (3, 11), 3459: (3, 11),
    # Python 3.12
    3469: (3, 12), 3470: (3, 12), 3471: (3, 12), 3472: (3, 12),
    3473: (3, 12), 3474: (3, 12), 3475: (3, 12), 3476: (3, 12),
    3477: (3, 12), 3478: (3, 12), 3479: (3, 12), 3480: (3, 12),
    3481: (3, 12), 3482: (3, 12),
    # Python 3.13
    3490: (3, 13), 3491: (3, 13), 3492: (3, 13), 3493: (3, 13),
}

# Ranges: (start, end) → version, for unknown magics within a known series.
_MAGIC_RANGES: Dict[Tuple[int, int], Tuple[int, int]] = {
    (3390, 3399): (3, 7),
    (3410, 3419): (3, 8),
    (3420, 3429): (3, 9),
    (3430, 3449): (3, 10),
    (3450, 3469): (3, 11),
    (3470, 3499): (3, 12),
    (3500, 3520): (3, 13),
    (3521, 3550): (3, 12),  # later 3.12.x point releases
    (3550, 3600): (3, 13),  # later 3.13.x
}

# Supported versions for decompilation
SUPPORTED_VERSIONS = {(3, 7), (3, 8), (3, 12)}


def get_python_version(magic: int) -> Optional[Tuple[int, int]]:
    """Get the Python version for a given magic number.

    Args:
        magic: The 16-bit magic number from the .pyc header (little-endian).

    Returns:
        (major, minor) tuple or None if unknown.
    """
    # Exact match first
    if magic in MAGIC_TO_VERSION:
        return MAGIC_TO_VERSION[magic]

    # Range-based fallback
    for (lo, hi), ver in _MAGIC_RANGES.items():
        if lo <= magic <= hi:
            return ver

    return None


def is_supported(version: Tuple[int, int]) -> bool:
    """Check whether a Python version is supported for decompilation."""
    return version in SUPPORTED_VERSIONS


def format_version(version: Tuple[int, int]) -> str:
    """Format a version tuple as a string."""
    return f"Python {version[0]}.{version[1]}"
