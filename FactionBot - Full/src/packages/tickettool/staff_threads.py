# -*- coding: utf-8 -*-
'''
TicketTool.staff_threads — Private Staff Discussion Threads (Tier 2 Feature #22).

Creates a private sub-thread inside each ticket channel (or thread) for staff
to discuss the ticket without the ticket creator seeing the conversation.

  🎫 Ticket Channel (creator can see)
  └── 🔒 Staff Discussion (only staff can see)

Builds on the existing ticket_notes system (which is text-only notes): staff
discussion threads are real Discord threads, so they support real-time back-
and-forth, pinned messages, and message history.

Per-panel config:
  ticket_panels.create_staff_thread — 0/1: auto-create a staff thread on
                                       ticket open

Integration:
  * TicketTool.wiring.on_ticket_create -> staff_threads.create_for_ticket(...)
    after the ticket channel exists.
  * TicketTool.wiring.on_ticket_close  -> staff_threads.archive_for_ticket(...)
    before the channel is deleted.
  * Staff-only visibility: the thread is created as a private thread whose
    initial overwrites exclude the ticket creator and @everyone, and include
    only the support role + admins.
'''

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import discord

from .db import PremiumDB


STAFF_THREAD_PREFIX = "🔒-staff-"


# =====================================================================
# PANEL CONFIG
# =====================================================================

def should_create_staff_thread(panel: Optional[Dict]) -> bool:
    if not panel:
        return False
    return bool(int(panel.get('create_staff_thread', 0) or 0))


def configure_panel(pdb: PremiumDB, data_manager, panel_id: str, *,
                    enabled: bool) -> bool:
    try:
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel:
            return False
        panel['create_staff_thread'] = 1 if enabled else 0
        data_manager.save_ticket_panel(panel)
        return True
    except Exception as exc:
        logging.warning(f"[staff_threads] configure_panel failed: {exc}")
        return False


# =====================================================================
# STAFF THREAD LIFECYCLE
# =====================================================================

async def create_for_ticket(
    *,
    ticket_channel: discord.TextChannel,
    ticket: Dict,
    panel: Optional[Dict],
    support_role_id: Optional[int] = None,
) -> Optional[discord.Thread]:
    '''Create a private staff discussion thread inside a ticket channel.

    The thread is created as a private thread (so the ticket creator can't see
    it even if they have view_channel on the parent). Staff (support role +
    admins) are added explicitly. Returns the thread object, or None on failure.
    '''
    guild = ticket_channel.guild
    ticket_id = ticket.get('ticket_id', 'unknown')
    thread_name = f"{STAFF_THREAD_PREFIX}{ticket_id}"[:100]
    try:
        # private_thread type makes the thread invisible to anyone not added.
        staff_thread = await ticket_channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=10080,
            reason=f"Staff discussion for ticket {ticket_id}",
        )
    except discord.Forbidden:
        logging.warning(f"[staff_threads] no permission to create thread in #{ticket_channel.name}")
        return None
    except discord.HTTPException as exc:
        logging.warning(f"[staff_threads] create thread failed: {exc}")
        return None

    # Add all support-role members + admins to the staff thread.
    added = 0
    for member in guild.members:
        if member.bot:
            continue
        try:
            if member.guild_permissions.administrator:
                await staff_thread.add_user(member)
                added += 1
                continue
            if support_role_id:
                role = guild.get_role(int(support_role_id))
                if role and role in member.roles:
                    await staff_thread.add_user(member)
                    added += 1
        except (discord.HTTPException, discord.Forbidden):
            continue
    # Post an intro message so the thread isn't empty.
    try:
        await staff_thread.send(
            embed=discord.Embed(
                title="🔒 Staff Discussion",
                description=(
                    f"This private thread is for staff discussion of ticket `{ticket_id}`.\n"
                    f"The ticket creator **cannot see** messages here.\n\n"
                    f"Use this for: internal notes, coordinating hand-offs, discussing sensitive info.\n"
                    f"For permanent visible notes the user can read later, use `!note`."
                ),
                color=discord.Color(0x2f3136),
            )
        )
    except discord.HTTPException:
        pass
    logging.info(f"[staff_threads] created staff thread for {ticket_id} ({added} staff added)")
    return staff_thread


async def archive_for_ticket(staff_thread: discord.Thread, *, reason: str = "Ticket closed") -> None:
    '''Archive + lock the staff discussion thread when the ticket closes.'''
    try:
        await staff_thread.edit(archived=True, locked=True, reason=reason)
    except discord.HTTPException as exc:
        logging.warning(f"[staff_threads] archive failed: {exc}")


# =====================================================================
# LOOKUP
# =====================================================================

async def find_staff_thread(channel: discord.TextChannel, ticket_id: str) -> Optional[discord.Thread]:
    '''Find the staff discussion thread for a ticket by its naming convention.'''
    expected = f"{STAFF_THREAD_PREFIX}{ticket_id}"[:100]
    try:
        async for thread in channel.archived_threads(private=True, limit=50):
            if thread.name == expected:
                return thread
    except (discord.HTTPException, discord.Forbidden):
        pass
    # Also check active threads.
    try:
        for thread in channel.threads:
            if thread.name == expected:
                return thread
    except (AttributeError, discord.HTTPException):
        pass
    return None
