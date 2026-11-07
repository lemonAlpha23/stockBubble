"""ASGI entry point for ``uvicorn stockbubble.api.asgi:app --reload``.

The factory in :func:`stockbubble.api.app.create_app` stays the single way to
build an app; this module only supplies the real, settings-driven wiring.
"""

from __future__ import annotations

from stockbubble.api.app import create_app
from stockbubble.bootstrap import build_services, ui_settings
from stockbubble.config import Settings

_settings = Settings()

app = create_app(
    services=build_services(_settings),
    ui=ui_settings(_settings),
    web_dir=_settings.web_dir,
)

__all__ = ["app"]
