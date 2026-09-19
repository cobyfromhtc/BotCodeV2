# -*- coding: utf-8 -*-
"""FactionBot ticket system — engine, views and commands.

    engine.py    TicketToolSystem, panel processing, reopen/close, SLA tasks
    views.py     panel/controls/modals/category views
    commands.py  ticket + panel commands (register(bot))

Members are exposed lazily (PEP 562) so importing one submodule never
forces the others to load — keeps the package cycle-free at import time.
"""
import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only, never imported at runtime
    from discord.ext import commands

_LAZY_MEMBERS = ("engine", "views", "commands")


def __getattr__(name: str):
    if name in _LAZY_MEMBERS:
        mod = importlib.import_module(f"modules.support.tickets.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def register(bot: "commands.Bot") -> None:
    """Register ticket commands on the bot instance."""
    _commands = importlib.import_module("modules.support.tickets.commands")
    _commands.register(bot)
