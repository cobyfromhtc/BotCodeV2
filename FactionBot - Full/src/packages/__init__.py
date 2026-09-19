# -*- coding: utf-8 -*-
"""FactionBot feature packages — the two bundled premium suites.

    packages/tickettool/     premium ticket system (26 modules)
    packages/reactionroles/  reaction roles (5 modules)

Contract (identical for both)::

    package.commands.register(bot)          prefix command registration
    package.wiring.on_setup_hook(dm, bot)   schema install (idempotent)
    package.wiring.on_*                    runtime event hooks

Both packages keep their internal imports relative, so they can be lifted
out of ``src/packages/`` as standalone drop-ins.
"""
import importlib

_PACKAGES = ("tickettool", "reactionroles")


def __getattr__(name: str):
    if name in _PACKAGES:
        mod = importlib.import_module(f"packages.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_PACKAGES)
