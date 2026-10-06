"""Tiny deterministic template renderer.

Supported syntax (intentionally small, enough to model real static site
dependency semantics)::

    @include('_partial.html')      include another template (partial)
    {{ key }} / {{ a.b }} / {{ xs[0] }}   HTML-escaped variable
    @foreach(item in items) ... @end       repeat block with ``item`` bound

Content bodies are minimal markdown: ``#``/``##`` headings, blank-line
separated paragraphs and ``- `` bullet lists.
"""

from __future__ import annotations

import html
import re
from typing import Any

INCLUDE_RE = re.compile(r"@include\(\s*(['\"])([^'\"]+)\1\s*\)")
RAW_VAR_RE = re.compile(r"\{\{\{\s*([A-Za-z_][\w]*(?:[.\[][\w\]]*)*)\s*\}\}\}")
VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][\w]*(?:[.\[][\w\]]*)*)\s*\}\}")
FOREACH_RE = re.compile(
    r"@foreach\(\s*([A-Za-z_]\w*)\s+in\s+"
    r"([A-Za-z_][\w]*(?:[.\[][\w\]]*)*)\s*\)(.*?)@end",
    re.DOTALL,
)
MAX_INCLUDE_DEPTH = 32


class RenderError(Exception):
    pass


def markdown_to_html(body: str) -> str:
    """Convert the supported markdown subset to HTML fragments."""
    out: list[str] = []
    paragraphs: list[str] = []

    def flush_paragraph() -> None:
        if paragraphs:
            text = " ".join(line.strip() for line in paragraphs)
            out.append(f"<p>{_inline(text)}</p>")
            paragraphs.clear()

    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
        elif stripped.startswith("## "):
            flush_paragraph()
            out.append(f"<h2>{_inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            flush_paragraph()
            out.append(f"<h1>{_inline(stripped[2:])}</h1>")
        elif stripped.startswith("- "):
            flush_paragraph()
            items: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(f"<li>{_inline(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        else:
            paragraphs.append(stripped)
        i += 1
    flush_paragraph()
    return "\n".join(out)


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  r'<a href="\2">\1</a>', text)
    return text


def _lookup(path: str, context: dict[str, Any]) -> Any:
    tokens = re.findall(r"[A-Za-z_]\w*|\d+", path)
    if not tokens:
        raise RenderError(f"empty variable path: {path!r}")
    value: Any = context
    for tok in tokens:
        if isinstance(value, dict):
            if tok not in value:
                raise RenderError(f"missing key {tok!r} in {path!r}")
            value = value[tok]
        elif isinstance(value, (list, tuple)):
            try:
                value = value[int(tok)]
            except (ValueError, IndexError) as exc:
                raise RenderError(f"bad index {tok!r} in {path!r}") from exc
        else:
            # Attribute access for e.g. object fields is not needed; treat
            # missing attribute as error rather than silently emitting None.
            raise RenderError(f"cannot resolve {tok!r} in {path!r}")
    return value


class Renderer:
    def __init__(self, templates: dict[str, bytes],
                 partial_prefix: str = "_"):
        self._templates = {
            name: blob.decode("utf-8") for name, blob in templates.items()
        }
        self._partial_prefix = partial_prefix

    # -- public ----------------------------------------------------------
    def has_template(self, name: str) -> bool:
        return name in self._templates

    def render(self, template_name: str, context: dict[str, Any],
               depth: int = 0) -> str:
        if depth > MAX_INCLUDE_DEPTH:
            raise RenderError("template include cycle or too deep")
        if template_name not in self._templates:
            raise RenderError(f"missing template: {template_name}")
        text = self._templates[template_name]
        text = self._expand_includes(text, context, depth)
        text = self._expand_foreach(text, context, depth)
        text = self._expand_vars(text, context)
        return text

    def partials(self) -> set[str]:
        """Names that act as shared partials (fan-out dependency)."""
        return {name for name in self._templates
                if Path_name(name).startswith(self._partial_prefix)}

    def discover_includes(self, template_name: str) -> set[str]:
        """Transitive set of templates included by ``template_name``."""
        seen: set[str] = set()
        stack = [template_name]
        while stack:
            current = stack.pop()
            source = self._templates.get(current)
            if source is None:
                continue
            for name in INCLUDE_RE.findall(source):
                inc = name[1]
                if inc not in seen:
                    seen.add(inc)
                    stack.append(inc)
        return seen

    # -- expansion passes ------------------------------------------------
    def _expand_includes(self, text: str, context: dict[str, Any],
                         depth: int) -> str:
        def repl(match: re.Match[str]) -> str:
            name = match.group(2)
            return self.render(name, context, depth + 1)

        # Repeat to allow an include to itself contain an include; cycles
        # are bounded by MAX_INCLUDE_DEPTH.
        previous = None
        while previous != text:
            previous = text
            text = INCLUDE_RE.sub(repl, text)
        return text

    def _expand_foreach(self, text: str, context: dict[str, Any],
                        depth: int) -> str:
        def repl(match: re.Match[str]) -> str:
            var, list_path, body = match.group(1), match.group(2), match.group(3)
            sequence = _lookup(list_path, context)
            if not isinstance(sequence, (list, tuple)):
                raise RenderError(f"{list_path!r} is not iterable in foreach")
            chunks: list[str] = []
            for element in sequence:
                inner = dict(context)
                inner[var] = element
                inner["loop"] = {"item": element}
                expanded = self._expand_foreach(body, inner, depth)
                expanded = self._expand_vars(expanded, inner)
                chunks.append(expanded)
            return "".join(chunks)

        # Handle innermost blocks first to support nesting.
        previous = None
        while previous != text:
            previous = text
            text = FOREACH_RE.sub(repl, text)
        return text

    def _expand_vars(self, text: str, context: dict[str, Any]) -> str:
        def raw_repl(match: re.Match[str]) -> str:
            value = _lookup(match.group(1), context)
            return str(value)

        def esc_repl(match: re.Match[str]) -> str:
            value = _lookup(match.group(1), context)
            return html.escape(str(value))

        text = RAW_VAR_RE.sub(raw_repl, text)
        return VAR_RE.sub(esc_repl, text)


def Path_name(name: str) -> str:
    """Basename of a template name (partials are prefix-checked on basename)."""
    return name.rsplit("/", 1)[-1]
