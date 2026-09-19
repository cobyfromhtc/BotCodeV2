# -*- coding: utf-8 -*-
"""FactionBot feature packages — the bundled premium + platform suites.

    packages/tickettool/      premium ticket system (26 modules)
    packages/reactionroles/   reaction roles (5 modules)
    packages/faction_access/  multi-guild licensing + identity + gating (8 modules)

Contract (identical for all three)::

    package.commands.register(bot)          prefix command registration
    package.wiring.on_setup_hook(dm, bot)   schema install (idempotent)
    package.wiring.on_*                    runtime event hooks

Both premium packages keep their internal imports relative, so they can be
lifted out of ``src/packages/`` as standalone drop-ins. faction_access adds
hooks beyond that contract — ``gating.install(bot)`` (global command gate)
and ``wiring.register_events(bot)`` / ``wiring.on_ready_hook(bot)`` — which
Bot.py calls at the documented lifecycle points. The faction_access files
are byte-identical across the Full and Short editions (verified by md5).
"""
import importlib

_PACKAGES = ("tickettool", "reactionroles", "faction_access")


def __getattr__(name: str):
    if name in _PACKAGES:
        mod = importlib.import_module(f"packages.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_PACKAGES)
