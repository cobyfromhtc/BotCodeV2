# -*- coding: utf-8 -*-
'''
FactionAccess.wiring — lifecycle hooks called from Bot.py (both editions).

This module is the ONLY surface Bot.py needs to know about. Every hook is
fail-safe: exceptions are caught and logged so a FactionAccess issue can
never block the core bot (matching the TicketTool/ReactionRoles wiring
contract exactly).

Bot.py call sites::

    after premium registration:   FactionAccess.commands.register(bot)
                                   FactionAccess.gating.install(bot)
    setup_hook():                  FactionAccess.wiring.on_setup_hook(data_manager, bot)
    after event registration:      FactionAccess.wiring.register_events(bot)
    on_ready (first connect):      FactionAccess.wiring.on_ready_hook(bot)

The expiry sweeper is a module-level tasks.loop owned by this module and
started from on_ready_hook (is_running-guarded, like every other loop in
the bot).
'''

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from .db import FactionAccessDB, install_faction_access_schema
from .service import FactionAccessService, now_iso
from . import gating

# Service singleton (set in on_setup_hook; read by the expiry loop).
_service: FactionAccessService = None   # type: ignore[assignment]

# Once-guard for the guild-dependent on_ready work (mirrors the host bot's
# own _on_ready_initialized pattern: on_ready fires again on every gateway
# reconnect, but the map/sweep/loop only need to happen once).
_on_ready_done: bool = False


# =====================================================================
# SETUP
# =====================================================================

def on_setup_hook(data_manager, bot) -> None:
    '''Install the faction_* schema, build the service, stash it everywhere.

    Called from TicketBot.setup_hook() AFTER DataManager.connect() (so the
    base tables + connection exist). Idempotent.
    '''
    global _service
    try:
        if data_manager._connection is None:
            logging.warning("[faction_access.wiring] no DB connection; skipping setup")
            return
        install_faction_access_schema(data_manager._connection)
        db = FactionAccessDB(data_manager)
        # Deferred import (cycle-safe): core.state is the bottom layer and
        # every edition exposes the same config singleton.
        from core import state
        _service = FactionAccessService(bot, db, state.config)
        _service.hydrate()
        # Access points: on the bot (packages + modules) and on core.state
        # (lower layers like utils.ui resolve per-guild identity from there).
        bot.faction_access = _service
        bot.faction_access_db = db
        state.faction_access = _service
        logging.info("[faction_access.wiring] schema installed + service attached (bot.faction_access / state.faction_access)")
    except Exception as exc:
        logging.exception(f"[faction_access.wiring] on_setup_hook failed: {exc}")


# =====================================================================
# GUILD MEMBERSHIP EVENTS
# =====================================================================

def register_events(bot: commands.Bot) -> None:
    """Install the on_guild_join / on_guild_remove handlers.

    Neither edition defines these events anywhere else (verified against
    both command/event surfaces), so owning them here is conflict-free.
    """

    @bot.event
    async def on_guild_join(guild: discord.Guild) -> None:
        try:
            service = getattr(bot, "faction_access", None)
            if service is None:
                return
            if service.is_home(guild.id):
                # The home faction re-adding the bot (e.g. after a kick):
                # home status is definition-based — just make sure the row
                # exists so dashboards and audits stay complete.
                if service._db.get_guild(guild.id) is None:
                    service._db.upsert_guild(guild.id, joined_at=now_iso(),
                                             updated_at=now_iso())
                    service.audit(None, "guild_joined", guild.id,
                                  f"{guild.name} (home)")
                return
            row_before = service._db.get_guild(guild.id)
            is_new = row_before is None
            service.register_join(guild)
            if is_new:
                await service.announce_join_request(guild)
            else:
                # Rejoined after 'left': back to pending; re-assert nick.
                await service.apply_nickname(guild)
            logging.info(f"[faction_access] guild join recorded: {guild.name} ({guild.id})")
        except Exception as exc:
            logging.exception(f"[faction_access] on_guild_join failed: {exc}")

    @bot.event
    async def on_guild_remove(guild: discord.Guild) -> None:
        try:
            service = getattr(bot, "faction_access", None)
            if service is None:
                return
            service.register_leave(guild.id, guild.name)
            logging.info(f"[faction_access] guild leave recorded: {guild.name} ({guild.id})")
        except Exception as exc:
            logging.exception(f"[faction_access] on_guild_remove failed: {exc}")


# =====================================================================
# ON_READY (first connect only)
# =====================================================================

def on_ready_hook(bot) -> None:
    """Guild-dependent one-time work: classify commands, adopt the home
    guild, register pre-existing guilds, apply nicknames, start the expiry
    sweeper. Called from the host's on_ready; reconnect-safe.

    This is a plain function (not async) so hosts can call it exactly like
    the premium wiring hooks; async work is scheduled onto the event loop.
    """
    global _on_ready_done
    if _on_ready_done:
        return
    _on_ready_done = True
    try:
        service = gating.build_and_apply_map(bot)
        if service is None:
            logging.warning("[faction_access.wiring] on_ready_hook: service missing — gating stays fail-open")
            return
        service.adopt_home()
        # Register guilds the bot was already in before FactionAccess
        # existed (first deploy on an existing install). They land in
        # 'pending' and are announced like fresh join requests.
        for guild in list(bot.guilds):
            if service._db.get_guild(guild.id) is None and not service.is_home(guild.id):
                service.register_join(guild)
                bot.loop.create_task(service.announce_join_request(guild))
        # Apply per-guild nicknames for every guild with an identity
        # override. Nicknames persist server-side; this is a cheap
        # re-assert sweep. Fire-and-forget: apply_nickname logs its own
        # failures and is forbidden-safe.
        for guild in list(bot.guilds):
            if service.identity_for(guild.id).is_override:
                bot.loop.create_task(service.apply_nickname(guild))
        if not _expiry_sweep.is_running():
            _expiry_sweep.start()
            logging.info("[faction_access] license expiry sweeper started (30-minute interval)")
    except Exception as exc:
        logging.exception(f"[faction_access.wiring] on_ready_hook failed: {exc}")


# =====================================================================
# EXPIRY SWEEPER
# =====================================================================

@tasks.loop(minutes=30)
async def _expiry_sweep() -> None:
    """Suspend licensed guilds whose license expiry has lapsed."""
    try:
        if _service is None:
            return
        due = _service.sweep_expiries()
        if due:
            logging.info(f"[faction_access] {len(due)} guild(s) auto-suspended (license expired)")
            await _service.announce_expiry_suspension(due)
    except Exception as exc:
        logging.exception(f"[faction_access] expiry sweep failed: {exc}")


@_expiry_sweep.before_loop
async def _expiry_sweep_wait_ready() -> None:
    """Wait for the gateway cache before the first sweep."""
    await _service._bot.wait_until_ready()
