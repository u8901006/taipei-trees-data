"""Append-only file helpers for auditable raw-data snapshots."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal


class ImmutableSnapshotError(RuntimeError):
    """Raised when an existing snapshot does not match the new content."""


def atomic_write_immutable(path: Path, content: bytes) -> Literal["created", "unchanged"]:
    """Create *path* exactly once, or prove that it already contains *content*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output:
            temporary_path = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary_path, path)
    except FileExistsError:
        if path.read_bytes() == content:
            return "unchanged"
        raise ImmutableSnapshotError(f"immutable snapshot already exists: {path}") from None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return "created"
