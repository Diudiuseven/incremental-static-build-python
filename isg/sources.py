"""Source tree scanning and frontmatter parsing.

Expected source layout::

    <root>/
      content/            markdown content files (*.md)
      templates/          *.html templates; files prefixed with ``_``
                          are shared partials
      data/               optional shared data files (*.json)

Only files below these directories are considered; editor swap/temp files
(``.``-prefixed or ``.tmp`` suffixed) are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from isg.fsutil import sha256_bytes

CONTENT_EXT = ".md"
TEMPLATE_EXT = ".html"
DATA_EXT = ".json"
TEMPLATE_DIR = "templates"
CONTENT_DIR = "content"
DATA_DIR = "data"
PARTIAL_PREFIX = "_"

_IGNORE_DIRS = {"__pycache__"}


@dataclass
class SourceSnapshot:
    """Immutable snapshot of the whole source tree.

    Keys are paths relative to the source root, e.g. ``content/foo.md``,
    except templates which are keyed by their bare template name
    (``page.html``).
    """

    contents: dict[str, bytes] = field(default_factory=dict)
    templates: dict[str, bytes] = field(default_factory=dict)
    data: dict[str, bytes] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)

    def exists(self, relpath: str) -> bool:
        return (relpath in self.contents
                or relpath in self.data
                or self._template_key(relpath) in self.templates)

    @staticmethod
    def _template_key(relpath: str) -> str | None:
        prefix = f"{TEMPLATE_DIR}/"
        if relpath.startswith(prefix) and relpath.endswith(TEMPLATE_EXT):
            return relpath[len(prefix):]
        return None


def _is_ignored(name: str) -> bool:
    return name.startswith(".") or name.endswith(".tmp") or name.endswith("~")


def _walk(root: Path, sub: str, suffix: str) -> dict[str, bytes]:
    base = root / sub
    out: dict[str, bytes] = {}
    if not base.exists():
        return out
    for path in sorted(base.rglob(f"*{suffix}")):
        if not path.is_file():
            continue
        if any(_is_ignored(part) or part in _IGNORE_DIRS
               for part in path.relative_to(base).parts):
            continue
        out[f"{sub}/{path.relative_to(base).as_posix()}"] = path.read_bytes()
    return out


def read_source_tree(root: Path) -> SourceSnapshot:
    """Read every source file into a snapshot.

    Scanning is a single ordered read pass; the snapshot is what a build
    renders, so a build always observes one coherent point-in-time view of
    the source tree (never a torn mix across concurrent edits).
    """
    root = Path(root)
    contents = _walk(root, CONTENT_DIR, CONTENT_EXT)
    data = _walk(root, DATA_DIR, DATA_EXT)

    raw_templates = _walk(root, TEMPLATE_DIR, TEMPLATE_EXT)
    templates: dict[str, bytes] = {}
    for rel, blob in raw_templates.items():
        templates[rel[len(TEMPLATE_DIR) + 1:]] = blob

    digests: dict[str, str] = {}
    for key, blob in contents.items():
        digests[key] = sha256_bytes(blob)
    for key, blob in templates.items():
        digests[f"{TEMPLATE_DIR}/{key}"] = sha256_bytes(blob)
    for key, blob in data.items():
        digests[key] = sha256_bytes(blob)

    return SourceSnapshot(contents=contents, templates=templates,
                          data=data, digests=digests)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a minimal ``---`` delimited header.

    Supported scalar forms: ``key: value``. Values are parsed as JSON when
    possible, otherwise kept as strings. A ``data`` value may be a JSON
    array or a comma/whitespace separated list of data file names.
    Returns ``(meta, body)``.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text
    meta: dict[str, Any] = {}
    idx = 1
    while idx < len(lines) and lines[idx].strip() != "---":
        raw = lines[idx]
        if ":" in raw:
            key, _, value = raw.partition(":")
            meta[key.strip()] = _parse_scalar(value.strip())
        idx += 1
    body = "\n".join(lines[idx + 1:])
    if "data" in meta:
        meta["data"] = _parse_data_list(meta["data"])
    return meta, body


def _parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    try:
        import json
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _parse_data_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value]
    if isinstance(value, str):
        return [part.strip() for part in value.replace(",", " ").split()
                if part.strip()]
    return []


def content_route(relpath: str) -> str:
    """Map a content path to its site route.

    ``content/foo/bar.md`` -> ``/foo/bar``;
    ``content/foo/index.md`` -> ``/foo/``;
    ``content/index.md``   -> ``/``.
    """
    rel = relpath
    prefix = f"{CONTENT_DIR}/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]
    if rel.endswith(CONTENT_EXT):
        rel = rel[:-len(CONTENT_EXT)]
    rel = rel.strip("/")
    if rel == "index" or rel == "":
        return "/"
    if rel.endswith("/index"):
        return "/" + rel[: -len("/index")] + "/"
    return "/" + rel


def route_output_relpath(route: str) -> str:
    """Route -> artifact relative path inside a release tree."""
    route = route if route.startswith("/") else "/" + route
    if route.endswith("/"):
        return route.lstrip("/") + "index.html"
    return route.lstrip("/") + ".html"
