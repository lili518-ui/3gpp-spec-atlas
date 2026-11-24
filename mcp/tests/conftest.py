"""Shared test fixtures: the Atlas page path and one Atlas instance over the built database."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PAGE = MCP.parent / "index.html"


@pytest.fixture(scope="session")
def atlas():
    from atlas_core.atlas import Atlas

    db = MCP / "dist" / "atlas.db"
    if not db.exists():
        pytest.skip("mcp/dist/atlas.db is missing: run python -m atlas_core.build first")
    return Atlas(db)


@pytest.fixture(scope="session")
def slim_atlas():
    from atlas_core.atlas import Atlas

    db = MCP / "dist" / "atlas-slim.db"
    if not db.exists():
        pytest.skip("mcp/dist/atlas-slim.db is missing: run python -m atlas_core.export first")
    return Atlas(db)
