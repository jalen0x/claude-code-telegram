"""Shared path utilities."""

from pathlib import Path


def is_path_within(path: Path, root: Path) -> bool:
    """Check if *path* is within *root* directory.

    Both arguments are used as-is (no implicit ``resolve()``).  Callers
    that need resolved paths should resolve before calling.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
