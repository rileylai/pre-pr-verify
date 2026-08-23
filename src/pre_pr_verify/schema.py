from __future__ import annotations

import json
from pathlib import Path

from pre_pr_verify.discovery_models import DiscoveryResult
from pre_pr_verify.models import ChangeSet


def render_changeset_schema() -> str:
    return (
        json.dumps(
            ChangeSet.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_changeset_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_changeset_schema(), encoding="utf-8")


def render_discovery_schema() -> str:
    return (
        json.dumps(
            DiscoveryResult.model_json_schema(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_discovery_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_discovery_schema(), encoding="utf-8")


if __name__ == "__main__":
    write_changeset_schema(Path("schemas/changeset-1.0.0.schema.json"))
    write_discovery_schema(Path("schemas/discovery-1.0.0.schema.json"))
