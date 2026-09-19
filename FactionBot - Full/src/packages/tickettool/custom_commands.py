# -*- coding: utf-8 -*-
'''
TicketTool.custom_commands — Custom Commands (Tier 2 Feature #36).

Per-guild custom commands that run automation action sequences (the same
action shape as ticket_automations). Lets server owners define their own
!commands without touching code.

  /customcommand add <name> <actions_json> [description]
  /customcommand remove <name>
  /customcommand list
  /customcommand run <name>           (or just !<name> in a ticket)

Custom commands can be:
  * restricted to a role (required_role_id)
  * restricted to ticket channels (ticket_only)
  * cooldown-gated (cooldown_seconds)

When invoked, the command's actions are executed via the same executor as
ticket automations (TicketTool.automations._execute_action), so custom commands
have access to: close, delete, claim, unclaim, add_role, remove_role,
send_message, rename, move, escalate, execute_command, start_automation,
stop_automation.
'''

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from .db import PremiumDB, _json_loads_list
from . import automations as auto_mod


# =====================================================================
# CRUD
# =====================================================================

def list_commands(pdb: PremiumDB, guild_id: int) -> List[Dict]:
    return pdb.list_custom_commands(guild_id)


def get_command(pdb: PremiumDB, guild_id: int, name: str) -> Optional[Dict]:
    return pdb.get_custom_command(guild_id, name)


def create_or_update(pdb: PremiumDB, *, guild_id: int, name: str,
                      actions: List[Dict], description: Optional[str] = None,
                      required_role_id: Optional[int] = None,
                      ticket_only: bool = False,
                      cooldown_seconds: int = 0,
                      created_by: Optional[int] = None,
                      command_id: Optional[str] = None) -> str:
    # Validate action types.
    for a in actions:
        if a.get('type') not in auto_mod.ACTION_TYPES:
            raise ValueError(f"invalid action type {a.get('type')!r}")
    return pdb.upsert_custom_command({
        'command_id': command_id,
        'guild_id': guild_id,
        'name': name.lower(),
        'description': description,
        'actions': actions,
        'required_role_id': required_role_id,
        'ticket_only': int(ticket_only),
        'cooldown_seconds': cooldown_seconds,
        'created_by': created_by,
        'is_active': 1,
    })


def delete(pdb: PremiumDB, command_id: str) -> bool:
    return pdb.delete_custom_command(command_id)


# =====================================================================
# INVOCATION
# =====================================================================

async def try_invoke(*, bot, pdb: PremiumDB, message: discord.Message,
                      command_name: str) -> bool:
    '''Try to run a custom command. Returns True if it ran.

    Called from TicketTool.wiring.on_message_prefix_command (which Bot.py's
    on_message invokes for any !-prefixed message that isn't a built-in).
    '''
    guild = message.guild
    if guild is None:
        return False
    cmd = pdb.get_custom_command(guild.id, command_name)
    if not cmd:
        return False
    # Permission check.
    if cmd.get('required_role_id'):
        role = guild.get_role(int(cmd['required_role_id']))
        if role is None or role not in message.author.roles:
            return False  # silently ignore — it's not their command
    # ticket_only check.
    if cmd.get('ticket_only'):
        ticket_tool = getattr(bot, 'ticket_tool', None)
        if ticket_tool is None:
            return False
        ticket = ticket_tool.data_manager.load_ticket_by_channel(message.channel.id)
        if not ticket:
            return False  # not in a ticket channel
    # Cooldown check.
    cooldown = int(cmd.get('cooldown_seconds', 0) or 0)
    if cooldown > 0:
        can_run, remaining = pdb.check_custom_command_cooldown(
            cmd['command_id'], message.author.id, cooldown
        )
        if not can_run:
            try:
                await message.channel.send(
                    f"⏳ This command is on cooldown. Try again in {remaining}s.",
                    delete_after=10,
                )
            except discord.HTTPException:
                pass
            return True  # it WAS our command, just on cooldown
        pdb.record_custom_command_use(cmd['command_id'], message.author.id)
    # Execute the actions.
    ticket_tool = getattr(bot, 'ticket_tool', None)
    ticket = None
    if ticket_tool:
        ticket = ticket_tool.data_manager.load_ticket_by_channel(message.channel.id)
    panel = None
    if ticket and ticket.get('panel_id'):
        panel = ticket_tool.data_manager.load_ticket_panel(ticket.get('panel_id'))
    event = auto_mod.AutomationEvent(
        trigger='custom_command',
        ticket=ticket or {},
        panel=panel or {},
        guild=guild, bot=bot,
        actor={'id': message.author.id, 'name': message.author.display_name},
    )
    actions = _json_loads_list(cmd.get('actions'))
    for action in actions:
        await auto_mod._execute_action(bot, pdb, action, event)
    logging.info(f"[custom_commands] {message.author} ran !{command_name} ({len(actions)} actions)")
    return True
