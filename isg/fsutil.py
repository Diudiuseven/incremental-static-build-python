"""Filesystem helpers: atomic writes, digests, temp directories."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

_FILE_MODE = 0o644


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write a file atomically within its directory.

    The destination is never observed half-written: a uniquely named
    temporary file is fsynced and renamed into place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_tree_atomic(root: Path, files: dict[str, bytes]) -> None:
    """Materialize ``files`` (relative path -> bytes) under ``root``.

    All directories are created before any file is written and every file
    goes through :func:`atomic_write_bytes`, so a failure never leaves a
    partially written target file.
    """
    root = Path(root)
    rel_dirs = {str(Path(rel).parent) for rel in files}
    for d in rel_dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        atomic_write_bytes(root / rel, data)


def iter_files(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            yield p


def copytree_contents(src: Path, dst: Path) -> None:
    """Copy every regular file of ``src`` into ``dst`` atomically."""
    src = Path(src)
    files: dict[str, bytes] = {}
    for p in iter_files(src):
        files[str(p.relative_to(src))] = p.read_bytes()
    write_tree_atomic(dst, files)


def remove_path(path: Path) -> None:
    """Remove a file or directory tree, tolerating its absence."""
    path = Path(path)
    if path.is_symlink() or path.exists():
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)


def ensure_clean_dir(path: Path) -> Path:
    """Create ``path`` empty, removing any prior content."""
    path = Path(path)
    if path.is_symlink() or path.exists():
        remove_path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so renames are durable."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_symlink(target: Path, link: Path) -> None:
    """Create/replace a symlink atomically via a temporary name."""
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.parent / f".{link.name}.tmp-{os.getpid()}"
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, link)
    fsync_dir(link.parent)
