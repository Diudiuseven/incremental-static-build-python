"""Renderer: templates, includes, data injection, markdown, dependencies."""

import pytest

from isg.render import (
    page_key_to_output,
    page_source_to_key,
    render_markdown,
    render_page,
)


BASE_SNAPSHOT = {
    "content/a.md": b"{{template:layouts/post}}\n# Title\n\nhello\n- one\n- two\n",
    "templates/layouts/post.html":
        b"<html>{{include:partials/head.html}}<body>{{content}}"
        b"{{data:data/x.json:name}}</body></html>",
    "templates/partials/head.html": b"<head>{{data:data/site.json:title}}</head>",
    "content/data/x.json": b'{"name": "Alice"}',
    "content/data/site.json": b'{"title": "My Site"}',
}


def test_page_renders_includes_data_markdown_and_records_deps():
    result = render_page("content/a.md", BASE_SNAPSHOT)
    html = result.html.decode()
    assert "<h1>Title</h1>" in html
    assert "<li>one</li>" in html and "<li>two</li>" in html
    assert "Alice" in html and "My Site" in html
    assert result.dependencies == frozenset({
        "content/a.md",
        "templates/layouts/post.html",
        "templates/partials/head.html",
        "content/data/x.json",
        "content/data/site.json",
    })


def test_default_template_is_page_html():
    snapshot = {
        "content/index.md": b"# Hi",
        "templates/page.html": b"{{content}}",
    }
    result = render_page("content/index.md", snapshot)
    assert result.html == b"<h1>Hi</h1>"
    assert result.dependencies == frozenset(
        {"content/index.md", "templates/page.html"})


def test_missing_template_and_data_raise():
    with pytest.raises(Exception):
        render_page("content/a.md", {"content/a.md": b"# x"})
    snapshot = {
        "content/a.md": b"# x",
        "templates/page.html": b"{{data:missing.json:x}}{{content}}",
    }
    with pytest.raises(Exception):
        render_page("content/a.md", snapshot)


def test_include_cycle_is_detected():
    snapshot = {
        "content/a.md": b"# x",
        "templates/page.html": b"{{include:a.html}}{{content}}",
        "templates/a.html": b"{{include:b.html}}",
        "templates/b.html": b"{{include:a.html}}",
    }
    with pytest.raises(Exception):
        render_page("content/a.md", snapshot)


def test_data_is_html_escaped():
    snapshot = {
        "content/a.md": b"# x",
        "templates/page.html": b"{{data:d.json:v}}{{content}}",
        "content/d.json": b'{"v": "<script>alert(1)</script>"}',
    }
    result = render_page("content/a.md", snapshot)
    assert b"<script>" not in result.html
    assert b"&lt;script&gt;" in result.html


def test_markdown_supported_subset():
    md = "# H1\n\ntext\n\n- a\n- b\n\n## H2\n"
    html = render_markdown(md)
    assert "<h1>H1</h1>" in html
    assert "<p>text</p>" in html
    assert "<li>a</li>" in html and "<li>b</li>" in html
    assert "<h2>H2</h2>" in html


def test_page_key_and_output_mapping():
    assert page_source_to_key("content/index.md") == ""
    assert page_source_to_key("content/a.md") == "a"
    assert page_source_to_key("content/guides/install/index.md") == "guides/install"
    assert page_key_to_output("") == "index.html"
    assert page_key_to_output("a") == "a/index.html"
