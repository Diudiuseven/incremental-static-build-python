"""Tiny template renderer that records dependencies while rendering.

Site model
----------
* Pages are ``content/**/*.md`` files. A page may select its template with a
  first line ``{{template:NAME}}`` (default template: ``page``).
* Templates live in ``templates/`` and may contain:
    - ``{{include:partials/header.html}}``  include another template/snippet
    - ``{{content}}``                        rendered page body
    - ``{{data:data/items.json:path.to}}``   inject JSON data (under content/)
* Every touched source file (page, template, snippet, data file) is returned
  as a dependency so changes invalidate the right pages.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass

from .fsutil import normalize_rel

DIRECTIVE_RE = re.compile(r"^\{\{template:([^}:]+)\}\}\s*$")
INCLUDE_RE = re.compile(r"\{\{\s*include:([^}]+?)\s*\}\}")
CONTENT_RE = re.compile(r"\{\{\s*content\s*\}\}")
DATA_RE = re.compile(r"\{\{\s*data:([^}]+?)\s*\}\}")


class RenderError(Exception):
    """Raised when a page cannot be rendered (missing template/data etc.)."""


@dataclass(frozen=True)
class RenderResult:
    html: bytes
    dependencies: frozenset[str]


def page_source_to_key(page_path: str) -> str:
    """content/guides/a.md -> guides/a ; content/guides/index.md -> guides/"""
    rel = page_path
    if rel.startswith("content/"):
        rel = rel[len("content/"):]
    if rel.endswith(".md"):
        rel = rel[:-3]
    if rel == "index":
        return ""
    if rel.endswith("/index"):
        rel = rel[: -len("/index")]
    return rel + "/" if False else rel


def page_key_to_output(key: str) -> str:
    """guides/a -> guides/a/index.html ; '' -> index.html"""
    if key == "":
        return "index.html"
    return f"{key}/index.html"


def _template_file(name: str) -> str:
    name = name.strip()
    if not name:
        raise RenderError("empty template name")
    if "." not in name.rsplit("/", 1)[-1]:
        name += ".html"
    return f"templates/{normalize_rel(name)}"


def _expand_includes(text: str, snapshot: dict[str, bytes],
                     stack: tuple[str, ...], deps: set[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        tpl = _template_file(match.group(1))
        if tpl in stack:
            raise RenderError(f"template include cycle at {tpl}")
        raw = snapshot.get(tpl)
        if raw is None:
            raise RenderError(f"missing template include: {tpl}")
        deps.add(tpl)
        body = raw.decode("utf-8")
        return _expand_includes(body, snapshot, stack + (tpl,), deps)

    return INCLUDE_RE.sub(repl, text)


def _lookup(payload: object, dotted: str) -> object:
    cur = payload
    if dotted:
        for part in dotted.split("."):
            if isinstance(cur, dict):
                if part not in cur:
                    raise RenderError(f"data key not found: {part}")
                cur = cur[part]
            elif isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                except (ValueError, IndexError) as exc:
                    raise RenderError(f"bad list index: {part}") from exc
            else:
                raise RenderError(f"cannot index scalar with {part}")
    if isinstance(cur, str):
        return cur
    if isinstance(cur, bool | int | float) or cur is None:
        return "null" if cur is None else str(cur)
    return json.dumps(cur, ensure_ascii=False, sort_keys=True)


def _inject_data(text: str, snapshot: dict[str, bytes], deps: set[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        spec = match.group(1).strip()
        bits = spec.split(":", 1)
        data_rel = normalize_rel(bits[0])
        dotted = bits[1].strip() if len(bits) == 2 else ""
        data_path = f"content/{data_rel}"
        raw = snapshot.get(data_path)
        if raw is None:
            raise RenderError(f"missing data file: {data_path}")
        deps.add(data_path)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RenderError(f"invalid JSON data: {data_path}: {exc}") from exc
        return html.escape(_lookup(payload, dotted))

    return DATA_RE.sub(repl, text)


def render_markdown(md: str) -> str:
    """Render the small markdown subset used in pages."""
    out: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            body = "<br>".join(html.escape(line) for line in paragraph)
            out.append(f"<p>{body}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            out.append("<ul>" + "".join(f"<li>{html.escape(i)}</li>"
                                        for i in list_items) + "</ul>")
            list_items.clear()

    for raw_line in md.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{html.escape(heading.group(2))}</h{level}>")
        elif line.startswith("- "):
            flush_paragraph()
            list_items.append(line[2:].strip())
        else:
            flush_list()
            paragraph.append(line.strip())
    flush_paragraph()
    flush_list()
    return "\n".join(out)


def render_page(page_path: str, snapshot: dict[str, bytes]) -> RenderResult:
    """Render one ``content/**/*.md`` page from the given source snapshot."""
    page_path = normalize_rel(page_path)
    raw = snapshot.get(page_path)
    if raw is None:
        raise RenderError(f"missing page source: {page_path}")
    deps: set[str] = {page_path}
    text = raw.decode("utf-8")

    template_name = "page"
    lines = text.splitlines()
    if lines:
        m = DIRECTIVE_RE.match(lines[0])
        if m:
            template_name = m.group(1).strip()
            text = "\n".join(lines[1:])

    tpl_path = _template_file(template_name)
    tpl_raw = snapshot.get(tpl_path)
    if tpl_raw is None:
        raise RenderError(f"missing template: {tpl_path}")
    deps.add(tpl_path)
    body = _expand_includes(tpl_raw.decode("utf-8"), snapshot, (tpl_path,), deps)
    body = _inject_data(body, snapshot, deps)
    body = CONTENT_RE.sub(lambda _m: render_markdown(text), body)
    return RenderResult(html=body.encode("utf-8"), dependencies=frozenset(deps))
