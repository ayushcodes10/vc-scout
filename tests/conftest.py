"""Test-suite guarantees.

The whole suite must pass with no network access and no API key. Both are enforced here
rather than trusted, so a future stage cannot quietly introduce a live call.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail loudly on any attempt to open a socket during a test."""

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "network access is disabled in tests; use a fixture instead of a live request"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    yield


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove provider credentials so nothing can accidentally authenticate."""
    import os

    for name in [key for key in os.environ if key.endswith(("_API_KEY", "_TOKEN"))]:
        monkeypatch.delenv(name, raising=False)
    yield
