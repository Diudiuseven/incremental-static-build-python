"""Release manifest: output index, content hashes and dependency graph.

The manifest is the single source of truth for what a release contains:
``pages`` maps page keys (e.g. "guides/a", "" for the home page) to the
blake2b hash of the published file; ``deps`` maps each page key to the
ordered, de-duplicated list of source paths used to render it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"


@dataclass
class Manifest:
    release_id: str
    pages: dict[str, str] = field(default_factory=dict)
    deps: dict[str, list[str]] = field(default_factory=dict)
    assets: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "schema": SCHEMA_VERSION,
            "release_id": self.release_id,
            "pages": {k: self.pages[k] for k in sorted(self.pages)},
            "deps": {k: sorted(set(self.deps.get(k, ())))
                     for k in sorted(self.deps)},
            "assets": {k: self.assets[k] for k in sorted(self.assets)},
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        payload = json.loads(text)
        if payload.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema: {payload.get('schema')!r}")
        return cls(
            release_id=str(payload["release_id"]),
            pages=dict(payload.get("pages", {})),
            deps={k: sorted(set(v)) for k, v in payload.get("deps", {}).items()},
            assets=dict(payload.get("assets", {})),
        )

    def clone(self) -> "Manifest":
        return Manifest(
            release_id=self.release_id,
            pages=dict(self.pages),
            deps={k: list(v) for k, v in self.deps.items()},
            assets=dict(self.assets),
        )
