# -*- coding: utf-8 -*-
"""FactionBot utils — shared toolkit for the feature modules.

    botkit  thread-cached SQLite connections (same DB as the core
            DataManager), embed styling, permission/hierarchy helpers,
            duration parsing.
"""
import importlib

_MEMBERS = ("botkit",)


def __getattr__(name: str):
    if name in _MEMBERS:
        mod = importlib.import_module(f"utils.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_MEMBERS)
