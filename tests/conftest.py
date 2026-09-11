"""pytest configuration for CourtListener MCP tests."""

import os
from pathlib import Path
from typing import Any

import pytest
from _pytest.config import Config
from fastmcp import Client
from loguru import logger

from app.server import ensure_setup, mcp

# Configure test logging
test_log_path = Path(__file__).parent / "test_logs" / "test.log"
test_log_path.parent.mkdir(exist_ok=True)
logger.add(test_log_path, rotation="10 MB", retention="1 week")

# Ensure server tools are set up before any tests run
ensure_setup()


@pytest.fixture(autouse=True)
def _offline_api_key(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Provide a dummy API key so offline tests pass with no real key set.

    Offline ([T]) tests must pass when COURT_LISTENER_API_KEY is unset
    (requirements.md C-3): respx mocks intercept every HTTP request, so the
    dummy key is never used against the real API. Tests marked
    ``integration`` keep the real key when present and skip when it is not.

    """
    if request.node.get_closest_marker("integration") is not None:
        if not os.getenv("COURT_LISTENER_API_KEY"):
            pytest.skip("Integration test requires COURT_LISTENER_API_KEY to be set")
        return
    monkeypatch.setenv("COURT_LISTENER_API_KEY", "test-key-offline-dummy")


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path) -> Any:
    """Bind each test to a tmp_path-rooted cache (keeps repo .cache/ untouched)."""
    from app.cache import FileCache, set_cache

    set_cache(FileCache(root=tmp_path / "cache"))
    yield
    set_cache(None)


@pytest.fixture
def client() -> Client[Any]:
    """Create a test client connected to the MCP server.

    Returns:
        A FastMCP test client connected to the server instance.

    """
    return Client(mcp)


def pytest_configure(config: Config) -> None:
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
