# -*- coding: utf-8 -*-
'''
FactionAccess.gating — the global command gate.

One async check is installed on the bot (``bot.add_check``) and runs for
EVERY prefix-command invocation in both editions. Nothing else in the
codebase uses global checks (verified against the Full and Short command
surfaces), so the gate composes cleanly with the existing per-command
permission decorators and the domain-split pruning.

Decision order for an invocation:

    DM context                        -> allow (unchanged legacy behavior)
    service missing (pre-setup)       -> allow + warn once (fail-open: the
                                         home guild must never be locked out
                                         by a packaging failure)
    home guild                        -> allow (full access by definition)
    ALWAYS_AVAILABLE command          -> allow (help / ping / license / request)
    guild not licensed                -> deny + self-announcing notice
    command home-only or bundle not
    granted for this guild            -> deny + self-announcing notice

The notice is sent by the check itself because both editions' on_command_error
silently swallow generic CheckFailure — a denied user must still learn WHY.
Notices are throttled per (guild, user) so a denied command can't be used to
flood a channel.
'''

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from . import catalog

# Set once the service has been attached; used to fail-open with a single
# warning instead of one log line per invocation.
_warned_no_service: bool = False


def _top_level_name(command) -> str:
    node = command
    while getattr(node, "parent", None) is not None:
        node = node.parent
    return node.name


async def _send_notice(ctx: commands.Context, embed: discord.Embed) -> None:
    """Best-effort denial notice; never raises into the check machinery."""
    try:
        await ctx.send(embed=embed, delete_after=20)
    except (discord.HTTPException, discord.Forbidden, discord.NotFound) as exc:
        logging.debug(f"[faction_access] denial notice could not be sent: {exc}")


async def faction_gate(ctx: commands.Context) -> bool:
    """Global check — see module docstring for the decision order."""
    global _warned_no_service

    if ctx.guild is None:
        return True  # DMs keep the exact pre-FactionAccess behavior.

    service = getattr(ctx.bot, "faction_access", None)
    if service is None:
        if not _warned_no_service:
            _warned_no_service = True
            logging.warning(
                "[faction_access] service unavailable — gate failing OPEN "
                "(home guild unaffected; allied gating disabled until setup_hook)"
            )
        return True

    guild_id = ctx.guild.id
    if service.is_home(guild_id):
        return True

    top_name = _top_level_name(ctx.command)
    if top_name in catalog.ALWAYS_AVAILABLE_COMMANDS:
        return True

    status = service.status(guild_id)
    identity = service.identity_for(guild_id)

    if status != "licensed":
        if service.notice_throttled(guild_id, ctx.author.id):
            return False
        embed = discord.Embed(
            title="🔒 Server Not Licensed",
            description=(
                f"**{ctx.bot.user.name if ctx.bot.user else 'This bot'}** is running in "
                f"**{ctx.guild.name}** without an active license, so most systems stay off.\n\n"
                f"A server leader can ping the bot owner with `{ctx.prefix}request` — "
                f"once the license is approved, the granted systems unlock here."
            ),
            color=discord.Color.dark_grey(),
        )
        embed.set_footer(text=f"FactionAccess · {identity.qualified_name()}")
        await _send_notice(ctx, embed)
        return False

    bundle_key = service.classify(ctx.command)
    if bundle_key is None or bundle_key == catalog.HOME_BUNDLE:
        if service.notice_throttled(guild_id, ctx.author.id):
            return False
        embed = discord.Embed(
            title="🔒 Owner-Only System",
            description=(
                f"`{ctx.prefix}{top_name}` manages global bot configuration and is "
                f"reserved for the home faction.\n\n"
                f"Systems currently enabled for **{ctx.guild.name}**: "
                f"{service.licensed_features_text(guild_id) or 'none yet'}\n"
                f"Ask for more with `{ctx.prefix}request`."
            ),
            color=discord.Color.dark_grey(),
        )
        embed.set_footer(text=f"FactionAccess · {identity.qualified_name()}")
        await _send_notice(ctx, embed)
        return False

    if not service.feature_enabled(guild_id, bundle_key):
        if service.notice_throttled(guild_id, ctx.author.id):
            return False
        embed = discord.Embed(
            title="🔒 System Not Enabled Here",
            description=(
                f"**{catalog.bundle_label(bundle_key)}** isn't part of "
                f"**{ctx.guild.name}**'s license.\n\n"
                f"Enabled systems: {service.licensed_features_text(guild_id) or 'none yet'}\n"
                f"A server leader can request it with `{ctx.prefix}request`."
            ),
            color=discord.Color.dark_grey(),
        )
        embed.set_footer(text=f"FactionAccess · {identity.qualified_name()}")
        await _send_notice(ctx, embed)
        return False

    return True


def install(bot) -> None:
    """Attach the global gate (idempotent — safe if called twice)."""
    check_coroutines = [c.__name__ for c in getattr(bot, "_checks", [])]
    if "faction_gate" in check_coroutines:
        return
    bot.add_check(faction_gate)
    logging.info("[faction_access] global command gate installed")


def build_and_apply_map(bot) -> Optional[object]:
    """Classify every registered command onto the service (post-registration).

    Returns the service, or None when the service isn't attached yet (the
    caller logs it; the gate keeps failing open for the home guild).
    """
    service = getattr(bot, "faction_access", None)
    if service is None:
        return None
    top_map, qualified_map = catalog.build_command_map(bot)
    service.set_command_map(top_map, qualified_map)
    home_only = [name for name, key in top_map.items() if key == catalog.HOME_BUNDLE]
    logging.info(
        "[faction_access] command map built: %d top-level commands "
        "(%d home-only, %d always-available)",
        len(top_map), len(home_only),
        sum(1 for k in top_map.values() if k == "__always__"),
    )
    return service
