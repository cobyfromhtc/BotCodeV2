# -*- coding: utf-8 -*-
'''
TicketTool.thread_tickets — Thread-Based Tickets (Tier 2 Feature #21).

Creates tickets as private Discord THREADS instead of channels. Useful when:
  * A server generates many tickets (threads don't count against Discord's
    500-channel-per-guild limit).
  * Tickets should be grouped under a single parent channel per panel.

Per-panel config (stored on ticket_panels via the Tier 2 migrations):
  use_threads                  — 0/1: create threads instead of channels
  thread_parent_channel_id     — the parent channel to create threads under
  allow_user_invite_in_thread  — 0/1: can non-staff invite others into the
                                 thread

Integration:
  * TicketTool.wiring.on_ticket_create checks panel.use_threads and, if true,
    calls create_thread_ticket() instead of letting Bot.py create a channel.
  * The thread is created private (invited members only); the support role
    is added; the creator is added automatically by Discord on creation.

Notes:
  * discord.py 2.4 supports create_thread on a TextChannel (creates a public
    thread) or a ForumChannel (creates a forum post). For private support
    tickets we use a private thread (archived=False, invitable=True) under a
    regular text channel.
  * Thread tickets are stored in the tickets table with is_thread=1 and
    thread_id = the thread's message-id (channel-ids and thread-ids share a
    namespace in Discord, so channel_id is reused for the thread id).
'''

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import discord

from .db import PremiumDB


# =====================================================================
# PANEL CONFIG HELPERS
# =====================================================================

def is_thread_panel(panel: Optional[Dict]) -> bool:
    if not panel:
        return False
    return bool(int(panel.get('use_threads', 0) or 0))


def get_thread_parent(panel: Dict, guild: discord.Guild) -> Optional[discord.TextChannel]:
    parent_id = panel.get('thread_parent_channel_id')
    if not parent_id:
        return None
    try:
        ch = guild.get_channel(int(parent_id))
    except (TypeError, ValueError):
        return None
    if isinstance(ch, discord.TextChannel):
        return ch
    return None


# =====================================================================
# THREAD TICKET CREATION
# =====================================================================

async def create_thread_ticket(
    *,
    guild: discord.Guild,
    user: discord.Member,
    panel: Dict,
    ticket_id: str,
    channel_name: str,
    support_role_id: Optional[int] = None,
    allow_user_invite: bool = False,
) -> Tuple[Optional[discord.Thread], Optional[str]]:
    '''Create a private thread as a ticket.

    Returns (thread, error_message). On success, the thread's id IS the
    channel_id stored on the ticket row (Discord shares the id namespace).
    '''
    parent = get_thread_parent(panel, guild)
    if parent is None:
        return None, "This panel is configured for thread tickets but has no parent channel set."

    # Build the thread. discord.py 2.4: parent.create_thread(...) creates a
    # thread under the parent text channel. We use type=discord.ChannelType.private_thread
    # so only invited members can see it.
    try:
        thread = await parent.create_thread(
            name=channel_name[:100],
            type=discord.ChannelType.private_thread,
            invitable=bool(allow_user_invite),  # False = only staff can invite
            auto_archive_duration=10080,  # 7 days (max); the bot keeps it alive
            reason=f"Ticket {ticket_id} for {user}",
        )
    except discord.Forbidden:
        return None, "I don't have permission to create threads in that channel."
    except discord.HTTPException as exc:
        return None, f"Failed to create thread: {exc}"

    # Add the support role's members to the thread (the creator is auto-added).
    if support_role_id:
        try:
            support_role = guild.get_role(int(support_role_id))
            if support_role:
                for member in support_role.members:
                    if member.bot:
                        continue
                    try:
                        await thread.add_user(member)
                    except (discord.HTTPException, discord.Forbidden):
                        pass  # best-effort
        except Exception as exc:
            logging.warning(f"[thread_tickets] add support role failed: {exc}")

    # Ensure the bot itself is in the thread (it should be automatically,
    # but this is a safety net).
    try:
        await thread.add_user(guild.me)
    except (discord.HTTPException, discord.Forbidden):
        pass

    return thread, None


# =====================================================================
# THREAD TICKET CLOSE
# =====================================================================

async def archive_thread_ticket(thread: discord.Thread, *, reason: str = "Ticket closed") -> None:
    '''Archive + lock a thread ticket (instead of deleting the channel).'''
    try:
        await thread.edit(archived=True, locked=True, reason=reason)
    except discord.HTTPException as exc:
        logging.warning(f"[thread_tickets] archive failed: {exc}")


# =====================================================================
# CONFIG COMMANDS HELPERS
# =====================================================================

def configure_panel_for_threads(pdb: PremiumDB, data_manager,
                                 panel_id: str, guild_id: int, *,
                                 enabled: bool, parent_channel_id: Optional[int],
                                 allow_user_invite: bool = False) -> bool:
    '''Update the panel's thread-ticket config columns.'''
    try:
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel:
            return False
        panel['use_threads'] = 1 if enabled else 0
        panel['thread_parent_channel_id'] = parent_channel_id
        panel['allow_user_invite_in_thread'] = 1 if allow_user_invite else 0
        data_manager.save_ticket_panel(panel)
        return True
    except Exception as exc:
        logging.warning(f"[thread_tickets] configure_panel_for_threads failed: {exc}")
        return False
