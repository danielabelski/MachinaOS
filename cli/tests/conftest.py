"""Shared fixtures for ``company`` tests.

The release-pipeline config tests need to read files that live in
bun workspace members (``client/``, ``server/nodejs/``) without
hardcoding those filesystem paths in every test. The canonical
source for "where does workspace X live" is the root ``package.json``
``workspaces`` array (bun's SSOT — ``pnpm-workspace.yaml`` is gone and
bun has no machine-readable workspace-listing command), so the fixture
parses it directly: expand each entry (globs included), read every
member's ``package.json``, and map package name → absolute path.

Tests then reference workspaces by their npm package name (a stable
identifier defined in each ``package.json``) rather than by path. If
``server/nodejs/`` is later renamed or moved, the test suite keeps
working — the ``workspaces`` array resolves the new location. Parsing
in-process (no subprocess) also removes the old skip-when-PM-missing
hazard: a broken ``workspaces`` array now fails loudly instead of
silently skipping the config tests.

For files that don't live inside a workspace (``.github/workflows``,
``scripts/install.js``), tests resolve via the ``root`` fixture which
points at ``cli.platform_.project_root()`` — the project's own
canonical worktree-aware helper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.platform_ import project_root


@pytest.fixture(scope="session")
def root() -> Path:
    """The project root, resolved via ``cli.platform_.project_root``."""
    return project_root()


@pytest.fixture(scope="session")
def workspace_members(root: Path) -> dict[str, Path]:
    """Map of bun workspace member name → absolute filesystem path.

    Parsed once per session from the root ``package.json`` ``workspaces``
    array. Entries may be globs, so non-literal patterns expand via
    ``Path.glob``. Doubles as a regression lock that ``workspaces`` holds
    real member *paths* (bun's pnpm-lock migrator has been observed to
    write package names instead): a bogus entry yields no ``package.json``
    and the dependent tests fail on the missing member name.
    """
    pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    members: dict[str, Path] = {}
    for pattern in pkg.get("workspaces", []):
        dirs = sorted(root.glob(pattern)) if "*" in pattern else [root / pattern]
        for d in dirs:
            manifest = d / "package.json"
            if manifest.is_file():
                m = json.loads(manifest.read_text(encoding="utf-8"))
                members[m["name"]] = d.resolve()
    return members
