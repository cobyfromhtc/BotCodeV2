# -*- coding: utf-8 -*-
"""Moderation domain — automod.

Members: automod. Each is a discord.py extension exposing
``async def setup(bot)``; loaded via ``modules.EXTENSIONS`` from
``src/bot.py`` setup_hook.
"""
import importlib

_MEMBERS = ("automod",)


def __getattr__(name: str):
    if name in _MEMBERS:
        mod = importlib.import_module(f"modules.moderation.verification")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_MEMBERS)
