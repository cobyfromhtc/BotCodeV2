# -*- coding: utf-8 -*-
'''
TicketTool.command_shortcuts — direct dispatch for automation 'execute_command'.

The full discord.py Context machinery is heavy to synthesize for an
automation. Instead, this module implements the small subset of ticket
commands that make sense to trigger from an automation, by calling the
TicketToolSystem + DataManager directly.

Supported automation commands:
  * !priority <level>        -> set ticket priority
  * !close [reason]          -> close the ticket
  * !claim                   -> claim on behalf of an available staff member
  * !note <content>          -> add a staff note
  * !rename <name>           -> rename the channel
  * !addrole <role> [user]   -> give a role (default target: ticket creator;
                                checks Manage Roles + role hierarchy)

Anything unrecognized is logged and ignored (an automation that references a
nonexistent command should not silently do nothing harmful).
'''

from __future__ import annotations

import logging
import shlex
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import discord


async def run_automation_command(bot, channel: discord.TextChannel,
                                  ticket: Dict, command: str) -> None:
    '''Parse + dispatch a single automation command line.'''
    if not command:
        return
    command = command.strip()
    # Strip leading prefix if present.
    if command.startswith('!'):
        command = command[1:]
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return
    cmd = parts[0].lower()
    args = parts[1:]

    tt = getattr(bot, 'ticket_tool', None)
    if tt is None or channel is None:
        return

    try:
        if cmd == 'priority' and args:
            level = args[0].lower()
            if level in {'low', 'normal', 'high', 'urgent'}:
                ticket['priority'] = level
                tt.data_manager.save_ticket(ticket)
        elif cmd == 'close':
            reason = ' '.join(args) if args else 'Closed by automation'
            await tt.close_ticket(channel, channel.guild.me, reason)
        elif cmd == 'claim':
            from . import automations as am
            claimer = am._pick_first_support_member(channel.guild, ticket)
            if claimer:
                await tt.claim_ticket(channel, claimer)
        elif cmd == 'note' and args:
            import uuid
            tt.data_manager.save_ticket_note({
                'note_id': str(uuid.uuid4())[:8],
                'ticket_id': ticket['ticket_id'],
                'guild_id': channel.guild.id,
                'author_id': bot.user.id,
                'content': ' '.join(args),
                'created_at': datetime.now(timezone.utc).isoformat(),
            })
        elif cmd == 'rename' and args:
            import re
            safe = re.sub(r'[^a-z0-9_-]', '-', args[0].lower())
            safe = re.sub(r'-+', '-', safe).strip('-') or 'ticket'
            await channel.edit(name=safe[:100])
        elif cmd == 'addrole' and args:
            await _run_addrole(bot, channel, ticket, args)
        else:
            logging.info(f"[tickettool.cmd_shortcuts] automation command not recognized: {cmd}")
    except Exception as exc:
        logging.warning(f"[tickettool.cmd_shortcuts] command '{cmd}' failed: {exc}")


async def _run_addrole(bot, channel: discord.TextChannel, ticket: Dict,
                        args) -> None:
    '''!addrole <role_id|@role> [user_id|@user] — grant a role.

    The target defaults to the ticket creator. Enforces the bot's Manage
    Roles permission, role hierarchy and managed/@everyone exclusions; every
    failure path is logged (never raises into the automation engine).
    '''
    guild = channel.guild
    role_token = str(args[0]).strip().lstrip('<@&').rstrip('>')
    role = guild.get_role(int(role_token)) if role_token.isdigit() else None
    if role is None:
        logging.info(f"[tickettool.cmd_shortcuts] addrole: unknown role '{args[0]}'")
        return

    # Optional explicit target; defaults to the ticket creator.
    target = None
    if len(args) > 1:
        user_token = str(args[1]).strip().lstrip('<@').rstrip('>')
        if user_token.isdigit():
            target = guild.get_member(int(user_token))
        if target is None:
            logging.info(f"[tickettool.cmd_shortcuts] addrole: target member '{args[1]}' not found")
            return
    else:
        creator_id = ticket.get('creator_id')
        target = guild.get_member(int(creator_id)) if creator_id else None
        if target is None:
            logging.info("[tickettool.cmd_shortcuts] addrole: ticket creator is not in the guild")
            return

    me = guild.me
    if not me.guild_permissions.manage_roles:
        logging.info("[tickettool.cmd_shortcuts] addrole: bot lacks the Manage Roles permission")
        return
    if role.is_default():
        logging.info("[tickettool.cmd_shortcuts] addrole: @everyone cannot be granted")
        return
    if role.managed:
        logging.info(f"[tickettool.cmd_shortcuts] addrole: role '{role.name}' is managed (bots/integrations)")
        return
    if role >= me.top_role:
        logging.info(f"[tickettool.cmd_shortcuts] addrole: role '{role.name}' is above the bot's top role")
        return

    try:
        await target.add_roles(role, reason="Ticket automation !addrole")
        logging.info(f"[tickettool.cmd_shortcuts] addrole: granted '{role.name}' to {target} ({target.id})")
    except (discord.Forbidden, discord.HTTPException) as exc:
        logging.warning(f"[tickettool.cmd_shortcuts] addrole: granting '{role.name}' to {target.id} failed: {exc}")
