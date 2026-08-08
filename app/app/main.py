"""Backward-compatible ASGI import; process construction lives in bootstrap.api."""

from app.bootstrap.api import app, settings

__all__ = ["app", "settings"]
