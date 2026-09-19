# -*- coding: utf-8 -*-
"""FactionBot UI — shared embed builders and generic views (``utils/ui``).

The presentation layer: everything that constructs Discord embeds or
reusable components lives here so feature cogs stay thin and every embed
across the bot shares one branded look.

Layering: ``utils/ui/`` imports from ``core/`` only (state + helpers) and
is imported by ``modules/`` and the bot entry point — never the other way
around.

Members
-------
EmbedBuilder              Static branded embed factory (success / error /
                          warning / info / ticket / giveaway / level …).
VerificationEmbedBuilder  The multi-step verification flow's embed screens.
PaginatedView             Generic pagination control used by every
                          list-style command (help, warnings, blacklist…).

Usage
-----
    from utils.ui import EmbedBuilder, PaginatedView
"""
from utils.ui.embeds import EmbedBuilder, VerificationEmbedBuilder
from utils.ui.common import PaginatedView

__all__ = [
    "EmbedBuilder",
    "VerificationEmbedBuilder",
    "PaginatedView",
]
