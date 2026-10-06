"""Small filesystem helpers: safe paths, atomic writes, hashing."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import tempfile


class UnsafePathError(ValueError):
    """Raised when a relative path escapes its base directory."""


def normalize_rel(path: str) -> str:
    """Normalize a site-relative posix path; reject traversal/absolute paths."""
    p = path.replace("\\", "/")
    if p.startswith("/"):
        raise UnsafePathError(f"absolute paths are not allowed: {path!r}")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise UnsafePathError(f"parent traversal is not allowed: {path!r}")
    return "/".join(parts)


def safe_join(root: str, rel: str) -> str:
    """Join a normalized relative path onto root without escaping it."""
    rel = normalize_rel(rel)
    return os.path.normpath(os.path.join(root, *rel.split("/"))) if rel else os.path.normpath(root)


def hash_bytes(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def hash_file(path: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_files(root: str) -> list[str]:
    """List regular files under root as normalized posix-relative paths."""
    found: list[str] = []
    if not os.path.isdir(root):
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            found.append(normalize_rel(rel))
    return found


def atomic_write(path: str, data: bytes) -> None:
    """Durably write a file via temp file + fsync + atomic rename."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(directory)


def remove_file(path: str) -> bool:
    """Remove a file if present; fsync its parent directory."""
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    fsync_dir(os.path.dirname(path) or ".")
    return True


def hardlink_or_copy(src: str, dst: str) -> None:
    """Link dst to src when possible, otherwise copy; never writes in place."""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    try:
        os.link(src, dst)
    except OSError as exc:
        if exc.errno not in (errno.EEXIST, errno.EXDEV, errno.EPERM, errno.EMLINK):
            raise
        if exc.errno == errno.EEXIST:
            os.remove(dst)
            return hardlink_or_copy(src, dst)
        shutil.copy2(src, dst)


def fsync_dir(path: str) -> None:
    """Best effort directory fsync so renames/unlinks survive a crash."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
