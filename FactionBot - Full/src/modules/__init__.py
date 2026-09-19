# -*- coding: utf-8 -*-
"""FactionBot feature modules — one package per functional domain.

Every module follows the family extension contract::

    def register(bot: commands.Bot) -> None: ...

``src/bot.py`` calls :func:`register_all` once, right after the bot
instance is created. ``modules.runtime.events`` uses
``register_events(bot)`` for the ``@bot.event`` handlers instead.

Domains
-------
administration  owner tools + tutorial, setup wizard, branding, channel config
engagement      giveaways, invite tracking, leveling, polls
factions        gang rules
moderation      moderation suite, message log, sticky roles
runtime         gateway event routing (on_message / on_ready / …)
support         ticket system (tickets subpackage), info commands
verification    member verification flow
"""
import importlib
import logging

EXTENSIONS = (
    "modules.administration.owner", "modules.administration.setup",
    "modules.verification.verification", "modules.support.tickets",
    "modules.moderation.moderation", "modules.factions.rules",
    "modules.administration.channels", "modules.engagement.giveaways",
    "modules.engagement.leveling", "modules.engagement.invites",
    "modules.engagement.polls", "modules.support.info",
    "modules.moderation.sticky_roles", "modules.moderation.msglog",
    "modules.administration.branding", "modules.administration.admin",
)


def register_all(bot) -> None:
    """Import every extension in EXTENSIONS order and call register(bot)."""
    for _ext in EXTENSIONS:
        _mod = importlib.import_module(_ext)
        _mod.register(bot)
        logging.info(f"[Modules] registered {_ext} ({len(bot.commands)} commands total)")
