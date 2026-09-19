# -*- coding: utf-8 -*-
"""FactionBot utils — cross-cutting presentation + toolkit layer.

Currently hosts the shared UI builders (``utils.ui``). Layering: utils
imports from ``core`` only and is imported by ``modules/`` and the bot
entry point.
"""
from utils.ui import EmbedBuilder, PaginatedView, VerificationEmbedBuilder

__all__ = ["EmbedBuilder", "PaginatedView", "VerificationEmbedBuilder"]
