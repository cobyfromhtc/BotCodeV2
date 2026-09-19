# -*- coding: utf-8 -*-
"""FactionBot FactionAccess — multi-guild licensing, identity and feature gating.

The package that turns the single-server FactionBot into a licensable
multi-guild service while keeping the purchaser in full control:

    guild licensing      pending → licensed → suspended/revoked lifecycle,
                        optional expiry, join-request notifications
    feature bundles      per-guild grants (verification, tickets, moderation,
                        engagement, leveling, …) enforced by a global check
    guild identity       per-guild gang tag / gang name / bot display nickname
                        (the home faction's configured names at home, each
                        allied faction's own names in its guild)
    authority            application owner + a runtime-editable allowlist
                        (never a hardcoded user id)
    audit trail          every licensing action recorded and reviewable

Dependency order: db -> identity -> catalog -> service -> gating -> commands
-> wiring. The package is edition-agnostic: it only touches the shared
``core.state`` singletons through deferred imports and receives the
DataManager/Bot instances as arguments, so the exact same files run in the
Full and Short editions (same contract as tickettool/reactionroles).

Bot.py call sites (both editions, identical)::

    FactionAccess.commands.register(bot)            after premium registration
    FactionAccess.gating.install(bot)               right after that
    FactionAccess.wiring.on_setup_hook(dm, bot)     inside setup_hook
    FactionAccess.wiring.register_events(bot)       after event registration
    FactionAccess.wiring.on_ready_hook(bot)         inside on_ready (once)
"""
from . import db, identity, catalog, service, gating, commands, wiring

__all__ = ["db", "identity", "catalog", "service", "gating", "commands", "wiring"]
