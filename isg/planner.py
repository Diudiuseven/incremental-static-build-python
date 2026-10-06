"""Dependency graph and invalidation planning.

A page depends on zero or more *dependency keys*:

* ``content:<relpath>``  its own markdown content file;
* ``template:<name>``    the page template;
* ``partial:<name>``     every included (transitively) shared partial;
* ``data:<relpath>``     declared data files (frontmatter ``data:``).

When any of those objects changes, the page is marked stale and rebuilt.
The planner is purely functional over a source snapshot and the current
graph, so repeated, reordered or coalesced events produce exactly the same
plan as a fresh full analysis -- the only thing that matters is the final
source state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from isg.model import ChangeEvent, ChangeKind
from isg.sources import (CONTENT_DIR, DATA_DIR, TEMPLATE_DIR, SourceSnapshot,
                         content_route)

DEP_CONTENT = "content"
DEP_TEMPLATE = "template"
DEP_PARTIAL = "partial"
DEP_DATA = "data"


def content_key(relpath: str) -> str:
    return f"{DEP_CONTENT}:{relpath}"


def template_key(name: str) -> str:
    return f"{DEP_TEMPLATE}:{name}"


def partial_key(name: str) -> str:
    return f"{DEP_PARTIAL}:{name}"


def data_key(relpath: str) -> str:
    return f"{DEP_DATA}:{relpath}"


def parse_key(key: str) -> tuple[str, str]:
    kind, _, value = key.partition(":")
    return kind, value


def event_dependency_key(relpath: str) -> str | None:
    """Map an arbitrary source-relative change path to a dependency key."""
    if relpath.startswith(f"{CONTENT_DIR}/"):
        return content_key(relpath)
    if relpath.startswith(f"{DATA_DIR}/"):
        return data_key(relpath)
    if relpath.startswith(f"{TEMPLATE_DIR}/"):
        name = relpath[len(TEMPLATE_DIR) + 1:]
        base = name.rsplit("/", 1)[-1]
        if base.startswith("_"):
            return partial_key(name)
        return template_key(name)
    return None


@dataclass
class Plan:
    routes_to_build: set[str] = field(default_factory=set)
    routes_to_delete: set[str] = field(default_factory=set)
    full_rebuild: bool = False
    changed_keys: set[str] = field(default_factory=set)
    deleted_keys: set[str] = field(default_factory=set)


class DependencyGraph:
    """Bidirectional route <-> dependency-key index."""

    def __init__(self) -> None:
        self._deps: dict[str, set[str]] = {}
        self._reverse: dict[str, set[str]] = {}

    def set_deps(self, route: str, deps: set[str]) -> None:
        self.remove_route(route)
        self._deps[route] = set(deps)
        for key in deps:
            self._reverse.setdefault(key, set()).add(route)

    def remove_route(self, route: str) -> None:
        for key in self._deps.pop(route, set()):
            routes = self._reverse.get(key)
            if routes:
                routes.discard(route)
                if not routes:
                    del self._reverse[key]

    def dependents(self, key: str) -> set[str]:
        return set(self._reverse.get(key, set()))

    def all_routes(self) -> set[str]:
        return set(self._deps)

    def deps_of(self, route: str) -> set[str]:
        return set(self._deps.get(route, set()))

    @classmethod
    def from_manifest(cls, deps: dict[str, list[str]]) -> "DependencyGraph":
        graph = cls()
        for route, keys in deps.items():
            graph.set_deps(route, set(keys))
        return graph


@dataclass
class _ContentAnalysis:
    route: str
    content_path: str
    template: str
    data_files: list[str]


class Planner:
    def __init__(self, graph: DependencyGraph) -> None:
        self.graph = graph

    # -- full analysis ---------------------------------------------------
    def analyze_snapshot(self, snapshot: SourceSnapshot) -> list[_ContentAnalysis]:
        """Extract (route, content, template, data) for every content file."""
        analyses: list[_ContentAnalysis] = []
        for relpath in sorted(snapshot.contents):
            text = snapshot.contents[relpath].decode("utf-8", "replace")
            meta, _body = _safe_frontmatter(text)
            analyses.append(_ContentAnalysis(
                route=content_route(relpath),
                content_path=relpath,
                template=str(meta.get("template", "page.html")),
                data_files=[str(d) for d in meta.get("data", [])],
            ))
        return analyses

    def discover_dependencies(self, analysis: _ContentAnalysis,
                              includes: set[str],
                              shared_partials: set[str]) -> set[str]:
        deps = {content_key(analysis.content_path),
                template_key(analysis.template)}
        for included in includes:
            deps.add(partial_key(included)
                     if included in shared_partials
                     else template_key(included))
        for data_file in analysis.data_files:
            deps.add(data_key(_normalize_data_path(data_file)))
        return deps

    # -- planning --------------------------------------------------------
    def plan(self, events: list[ChangeEvent],
             snapshot: SourceSnapshot,
             total_pages: int,
             threshold: float) -> Plan:
        """Compute the build plan for one batch.

        The plan reconciles the change set against the *current* source
        snapshot rather than trusting event contents, so repeated and
        out-of-order events converge on the same result.
        """
        plan = Plan()
        current_routes = {content_route(p) for p in snapshot.contents}

        affected: set[str] = set()
        for event in events:
            key = event_dependency_key(event.path)
            if key is None:
                continue
            kind, value = parse_key(key)
            if event.kind is ChangeKind.DELETE:
                plan.deleted_keys.add(key)
            else:
                plan.changed_keys.add(key)

            if kind == DEP_CONTENT:
                # Content routes are reconciled against the snapshot:
                # existing content builds, vanished content is deleted.
                route = content_route(value)
                if route in current_routes and event.kind is not ChangeKind.DELETE:
                    affected.add(route)
                elif route in self.graph.all_routes() or route in current_routes:
                    if route not in current_routes:
                        plan.routes_to_delete.add(route)
            else:
                # Template / partial / data: fan out to every dependent.
                affected |= self.graph.dependents(key)

        # A page whose content still exists is always (re)built when
        # affected; de-duplicate with the delete set.
        plan.routes_to_build |= affected & current_routes
        plan.routes_to_delete -= current_routes

        # Reconcile deletes even if the delete event arrived early/late:
        # any content key reported deleted whose route has no current
        # content must disappear.
        for key in plan.deleted_keys:
            kind, value = parse_key(key)
            if kind == DEP_CONTENT:
                route = content_route(value)
                if route not in current_routes and route in self.graph.all_routes():
                    plan.routes_to_delete.add(route)

        # Full rebuild fallback: if the affected fraction reaches the
        # configured threshold, rebuild everything. Both paths render from
        # the same snapshot, so outputs are identical.
        if self._should_rebuild_all(plan, total_pages, threshold):
            plan.full_rebuild = True
            plan.routes_to_build = set(current_routes)
            # Deletes are implicit: routes not in the snapshot are absent.
            stale = self.graph.all_routes() - current_routes
            plan.routes_to_delete = stale
        return plan

    @staticmethod
    def _should_rebuild_all(plan: Plan, total_pages: int,
                            threshold: float) -> bool:
        if total_pages <= 0:
            return True  # first publication is always a "full" build
        touched = len(plan.routes_to_build | plan.routes_to_delete)
        if touched == 0:
            return False
        return (touched / total_pages) >= threshold


def _normalize_data_path(name: str) -> str:
    if name.startswith(f"{DATA_DIR}/"):
        return name
    return f"{DATA_DIR}/{name}"


def _safe_frontmatter(text: str):
    from isg.sources import parse_frontmatter
    try:
        return parse_frontmatter(text)
    except Exception:
        return {}, text
