"""Shared fixtures: on-disk source tree, output tree and a test engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from isg.config import Config
from isg.engine import Engine


PAGE_TEMPLATE = (
    "<html><body>@include('_nav.html')"
    "<h1>{{title}}</h1><main>{{{body_html}}}</main>"
    "@foreach(p in posts)<span>{{p.title}}</span>@end"
    "</body></html>"
)
NAV_TEMPLATE = "<nav>{{site_name}}:{{route}}</nav>"


def write_site(root: Path, *, pages: dict[str, str] | None = None,
               page_template: str = PAGE_TEMPLATE,
               nav: str = NAV_TEMPLATE,
               posts: str = '[{"title":"p1"},{"title":"p2"}]') -> Path:
    (root / "content").mkdir(parents=True, exist_ok=True)
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "templates" / "page.html").write_text(page_template)
    (root / "templates" / "_nav.html").write_text(nav)
    (root / "data" / "posts.json").write_text(posts)
    pages = pages if pages is not None else {
        "index.md": ("---\ntitle: Home\ndata: posts.json\n---\n# Home\n\nhello"),
        "a.md": ("---\ntitle: A\ndata: posts.json\n---\n# A\n\nalpha"),
        "b.md": ("---\ntitle: B\ndata: posts.json\n---\n# B\n\nbravo"),
    }
    for rel, text in pages.items():
        path = root / "content" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


@pytest.fixture()
def site(tmp_path: Path) -> Path:
    return write_site(tmp_path / "site")


@pytest.fixture()
def config() -> Config:
    return Config(concurrency=4, page_timeout=5.0, batch_size=50,
                  full_rebuild_threshold=1.0, keep_releases=1,
                  batch_idle_seconds=0.01)


@pytest.fixture()
def engine(tmp_path: Path, site: Path, config: Config) -> Engine:
    eng = Engine(site, tmp_path / "build", config=config)
    eng.start()
    yield eng
    eng.stop()


@pytest.fixture()
def started_engine(engine: Engine) -> Engine:
    engine.rebuild_now()
    return engine
