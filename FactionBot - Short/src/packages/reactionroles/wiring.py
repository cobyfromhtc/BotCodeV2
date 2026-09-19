# -*- coding: utf-8 -*-
'''
ReactionRoles.wiring — lifecycle hooks called from Bot.py.

This module is the ONLY surface Bot.py needs to know about. Each hook is a
thin orchestrator over the submodules. Every hook is fail-safe: exceptions are
caught and logged so a ReactionRoles issue can never block the core bot
(matching the TicketTool.wiring contract exactly).

Bot.py call sites:
    setup_hook():
        ReactionRoles.wiring.on_setup_hook(data_manager, bot)
    on_raw_reaction_add(payload):
        await ReactionRoles.wiring.on_raw_reaction_add(payload, bot)
    on_raw_reaction_remove(payload):
        await ReactionRoles.wiring.on_raw_reaction_remove(payload, bot)
    on_raw_reaction_clear(payload):
        await ReactionRoles.wiring.on_raw_reaction_clear(payload, bot)
    on_message_delete(message):
        await ReactionRoles.wiring.on_message_delete(message.id, bot)
'''

from __future__ import annotations

import logging

import discord

from .db import install_reaction_roles_schema, ReactionRolesDB
from . import system as system_mod


# =====================================================================
# SETUP
# =====================================================================

def on_setup_hook(data_manager, bot) -> None:
    '''Install the reaction_roles schema + build the accessor + set global refs.

    Called from TicketBot.setup_hook() AFTER DataManager._create_tables() has
    run (so base tables exist). Idempotent.
    '''
    try:
        if data_manager._connection is None:
            logging.warning("[reactionroles.wiring] no DB connection; skipping schema install")
            return
        install_reaction_roles_schema(data_manager._connection)
        # Stash the accessor on the bot so commands can find it.
        bot.reaction_roles_db = ReactionRolesDB(data_manager)
        # Set module-level refs used by ReactionRoleSystem methods.
        system_mod.set_global_refs(bot, data_manager)
        logging.info("[reactionroles.wiring] schema installed + reaction_roles_db attached")
    except Exception as exc:
        logging.exception(f"[reactionroles.wiring] on_setup_hook failed: {exc}")


# =====================================================================
# RAW REACTION EVENTS
# =====================================================================

async def on_raw_reaction_add(payload: "discord.RawReactionActionEvent", bot) -> None:
    '''on_raw_reaction_add — delegate to ReactionRoleSystem.handle_reaction.'''
    try:
        await system_mod.handle_reaction(payload, added=True)
    except Exception as exc:
        logging.exception(f"[RR] on_raw_reaction_add error: {exc}")


async def on_raw_reaction_remove(payload: "discord.RawReactionActionEvent", bot) -> None:
    '''on_raw_reaction_remove — fix payload.member then delegate.

    payload.member is None on reaction_remove, so we fetch the member from the
    guild first (mirrors the original inline handler in Bot.py).
    '''
    try:
        if payload.member is None and payload.guild_id is not None:
            guild = bot.get_guild(payload.guild_id)
            if guild is not None:
                payload.member = guild.get_member(payload.user_id)
        await system_mod.handle_reaction(payload, added=False)
    except Exception as exc:
        logging.exception(f"[RR] on_raw_reaction_remove error: {exc}")


async def on_raw_reaction_clear(payload: "discord.RawReactionClearEvent", bot) -> None:
    '''on_raw_reaction_clear — drop all mappings tied to the cleared message.'''
    try:
        db = getattr(bot, 'reaction_roles_db', None)
        if db is None:
            return
        removed = db.delete_reaction_roles_for_message(payload.message_id)
        if removed:
            logging.info(
                f"[RR] Cleared {removed} mappings (reactions cleared on msg {payload.message_id})"
            )
    except Exception as exc:
        logging.exception(f"[RR] on_raw_reaction_clear error: {exc}")


async def on_message_delete(message_id: int, bot) -> int:
    '''on_message_delete — drop any reaction-role mappings tied to the deleted
    message. Returns the number of mappings removed (0 if none / not ready).'''
    try:
        db = getattr(bot, 'reaction_roles_db', None)
        if db is None:
            return 0
        return db.delete_reaction_roles_for_message(message_id)
    except Exception as exc:
        logging.exception(f"[RR] on_message_delete error: {exc}")
        return 0
