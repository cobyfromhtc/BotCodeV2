# -*- coding: utf-8 -*-
"""Engagement domain — giveaways, invite tracking, leveling, polls.

Members: giveaways, invites, leveling, polls. Each exposes ``register(bot)`` (except
``events``, which exposes ``register_events(bot)``); imported via
``modules.EXTENSIONS`` from ``src/bot.py``.
"""
import importlib

_MEMBERS = ("giveaways", "invites", "leveling", "polls",)


def __getattr__(name: str):
    if name in _MEMBERS:
        mod = importlib.import_module(f"modules.engagement.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_MEMBERS)
