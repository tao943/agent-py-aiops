"""HTTP API application for Super AI backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from super_ai.api.app import create_app

__all__ = ["create_app"]


def __getattr__(name: str) -> object:
    """Load the application factory without eagerly importing every router."""

    if name == "create_app":
        from super_ai.api.app import create_app

        return create_app
    raise AttributeError(name)
