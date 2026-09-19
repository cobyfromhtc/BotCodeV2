# -*- coding: utf-8 -*-
"""FactionBot feature modules — one package per functional domain.

discord.py cogs loaded from ``src/bot.py`` setup_hook via
``await bot.load_extension(ext)``. Each module defines ``async def
setup(bot)`` per the discord.py extension contract and receives the core
through bot attributes (``bot.fb_config`` / ``bot.fb_data_manager`` /
``bot.embed_builder``) plus the shared ``utils.botkit`` toolkit — they
never import the entry module.

Domains
-------
administration  setup wizard
engagement      invites, leveling, polls
moderation      automod
support         help
verification    member verification flow
factions        reserved — gang-rule commands ship with the Full variant
                 (see docs/FEATURES.md "Open parity work"); the folder is
                 kept for structural parity between variants.
"""
EXTENSIONS = (
    "modules.verification.verification",
    "modules.engagement.polls",
    "modules.engagement.invites",
    "modules.engagement.leveling",
    "modules.moderation.automod",
    "modules.administration.setup",
    "modules.support.help",
)
