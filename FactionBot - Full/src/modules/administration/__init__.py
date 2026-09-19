# -*- coding: utf-8 -*-
"""Administration domain — owner tools + tutorial, setup wizard, branding, channel config.

Members: owner, setup, branding, channels, admin. Each exposes ``register(bot)`` (except
``events``, which exposes ``register_events(bot)``); imported via
``modules.EXTENSIONS`` from ``src/bot.py``.
"""
import importlib

_MEMBERS = ("owner", "setup", "branding", "channels", "admin",)


def __getattr__(name: str):
    if name in _MEMBERS:
        mod = importlib.import_module(f"modules.administration.verification")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_MEMBERS)
