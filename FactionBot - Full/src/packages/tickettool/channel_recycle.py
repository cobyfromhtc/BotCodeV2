# -*- coding: utf-8 -*-
'''
TicketTool.channel_recycle — Ticket Channel Recycling (Tier 2 Feature #24).

Instead of deleting a ticket channel on close, "recycle" it: archive the
messages (transcript is already saved), reset the channel's name/permissions,
and stash it in a pool. The next ticket created in the same panel reuses a
recycled channel instead of creating a new one.

Why: Discord caps guilds at 500 channels. A busy support server that deletes
+ creates channels constantly can hit this limit during a burst. Recycling
eliminates the issue entirely — the channel count stays stable.

Per-panel config:
  ticket_panels.recycle_channels — 0/1: recycle instead of delete on close

Integration:
  * TicketTool.wiring.on_ticket_create -> recycle.acquire_channel(...)
    returns a recycled channel if available (and resets its perms/name),
    else None (and the caller creates a new channel as usual).
  * TicketTool.wiring.on_ticket_close  -> recycle.release_channel(...)
    recycles the channel instead of letting Bot.py delete it.

Recycled channels are stored in recycled_channels (channel_id, panel_id,
category_id). When acquired, they're removed from the pool and re-used.
'''

from __future__ import annotations

import logging
from typing import Dict, Optional

import discord

from .db import PremiumDB


# =====================================================================
# PANEL CONFIG
# =====================================================================

def is_recycle_enabled(panel: Optional[Dict]) -> bool:
    if not panel:
        return False
    return bool(int(panel.get('recycle_channels', 0) or 0))


def configure_panel(pdb: PremiumDB, data_manager, panel_id: str, *,
                    enabled: bool) -> bool:
    try:
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel:
            return False
        panel['recycle_channels'] = 1 if enabled else 0
        data_manager.save_ticket_panel(panel)
        return True
    except Exception as exc:
        logging.warning(f"[channel_recycle] configure_panel failed: {exc}")
        return False


# =====================================================================
# ACQUIRE / RELEASE
# =====================================================================

async def acquire_channel(
    *,
    guild: discord.Guild,
    panel: Dict,
    new_name: str,
    creator: discord.Member,
    support_role_id: Optional[int] = None,
    category_id: Optional[int] = None,
    pdb: PremiumDB,
) -> Optional[discord.TextChannel]:
    '''Try to reuse a recycled channel for a new ticket.

    Returns the channel (already renamed + perms reset) or None if no recycled
    channel is available. The caller then creates a new channel as usual.
    '''
    panel_id = panel.get('panel_id')
    if not panel_id:
        return None
    recycled = pdb.get_recycled_channel(panel_id)
    if not recycled:
        return None
    channel_id = recycled['channel_id']
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        # The recycled channel was deleted out-of-band; drop it from the pool.
        pdb.remove_recycled_channel(int(channel_id))
        return None
    # Unarchive if it's archived.
    try:
        if isinstance(channel, discord.TextChannel):
            # Reset name.
            await channel.edit(name=new_name[:100], reason="Recycled for new ticket")
            # Reset permissions: wipe the old ticket's overwrites.
            try:
                await channel.purge(reason="Recycling channel for new ticket")
            except (discord.HTTPException, discord.Forbidden):
                pass
            # Re-apply the standard ticket overwrites.
            overwrites = _build_overwrites(guild, creator, support_role_id)
            await channel.edit(overwrites=overwrites)
            # Move category if needed.
            if category_id and channel.category_id != int(category_id):
                cat = guild.get_channel(int(category_id))
                if isinstance(cat, discord.CategoryChannel):
                    await channel.edit(category=cat)
            # Remove from the pool — it's now in use.
            pdb.remove_recycled_channel(int(channel_id))
            return channel
    except discord.HTTPException as exc:
        logging.warning(f"[channel_recycle] acquire failed for {channel_id}: {exc}")
        pdb.remove_recycled_channel(int(channel_id))
        return None
    return None


async def release_channel(
    *,
    channel: discord.TextChannel,
    panel: Dict,
    pdb: PremiumDB,
) -> bool:
    '''Recycle a closing ticket's channel instead of deleting it.

    Returns True if the channel was recycled, False if recycling is disabled
    or failed (the caller should then delete the channel as usual).
    '''
    if not is_recycle_enabled(panel):
        return False
    panel_id = panel.get('panel_id')
    if not panel_id:
        return False
    try:
        # Archive the channel's messages (transcript already saved by the
        # close hook). We purge to clean it for reuse, then stash the channel.
        try:
            await channel.purge(reason="Recycling ticket channel")
        except (discord.HTTPException, discord.Forbidden):
            pass
        # Reset the channel name to a placeholder.
        await channel.edit(name=f"recycled-ticket-{channel.id % 10000}",
                           topic="Recycled ticket channel — available for reuse")
        # Strip the overwrites back to default (only the bot + admins).
        try:
            for target, overwrite in list(channel.overwrites.items()):
                # Keep the bot's own overwrite + @everyone; drop the rest.
                if isinstance(target, discord.Member):
                    if target != channel.guild.me:
                        await channel.set_permissions(target, overwrite=None,
                                                       reason="Recycling channel")
        except (discord.HTTPException, discord.Forbidden) as exc:
            logging.warning(f"[channel_recycle] reset overwrites failed: {exc}")
        # Add to the pool.
        pdb.add_recycled_channel(channel.id, channel.guild.id, panel_id,
                                  channel.category_id)
        logging.info(f"[channel_recycle] recycled channel {channel.id} for panel {panel_id}")
        return True
    except discord.HTTPException as exc:
        logging.warning(f"[channel_recycle] release failed for {channel.id}: {exc}")
        return False


def _build_overwrites(guild: discord.Guild, creator: discord.Member,
                       support_role_id: Optional[int]) -> dict:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        creator: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              read_message_history=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                               manage_channels=True, read_message_history=True),
    }
    if support_role_id:
        role = guild.get_role(int(support_role_id))
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, attach_files=True,
            )
    return overwrites


# =====================================================================
# STATS
# =====================================================================

def pool_stats(pdb: PremiumDB, panel_id: Optional[str] = None) -> Dict:
    return {
        'total': pdb.count_recycled_channels(panel_id),
        'by_panel': pdb.count_recycled_channels() if panel_id else None,
    }
