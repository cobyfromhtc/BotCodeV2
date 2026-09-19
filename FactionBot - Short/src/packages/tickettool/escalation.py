# -*- coding: utf-8 -*-
'''
TicketTool.escalation — Ticket Escalation (Tier 1 Feature #10).

Provides:
  * /escalate [to_panel_id] [reason] — move a ticket into another panel's
    category, notify a configured role, log the escalation, and bump the
    ticket's escalation counter.
  * Per-panel escalation routes (from_panel -> to_panel) with an optional
    auto-escalate timer and auto-escalate priority.
  * Escalation history per ticket.

Integration:
  * TicketTool.commands exposes the /escalate hybrid command.
  * TicketTool.automations calls escalate_ticket() from an 'escalate' action.
  * TicketTool.wiring's SLA loop checks auto_escalate_hours and escalates
    tickets that have been stuck longer than the threshold.
'''

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import discord

from .db import PremiumDB


# =====================================================================
# ROUTE CONFIG
# =====================================================================

def list_routes(pdb: PremiumDB, guild_id: int) -> list:
    return pdb.list_escalation_routes(guild_id)


def get_route(pdb: PremiumDB, from_panel_id: str) -> Optional[Dict]:
    return pdb.get_escalation_route(from_panel_id)


def upsert_route(pdb: PremiumDB, *, guild_id: int, from_panel_id: str,
                 to_panel_id: str, notify_role_id: Optional[int] = None,
                 auto_escalate_hours: float = 0,
                 auto_escalate_priority: Optional[str] = None) -> str:
    return pdb.upsert_escalation_route({
        'guild_id': guild_id,
        'from_panel_id': from_panel_id,
        'to_panel_id': to_panel_id,
        'notify_role_id': notify_role_id,
        'auto_escalate_hours': auto_escalate_hours,
        'auto_escalate_priority': auto_escalate_priority,
    })


def delete_route(pdb: PremiumDB, route_id: str) -> bool:
    return pdb.delete_escalation_route(route_id)


# =====================================================================
# ESCALATION ACTION
# =====================================================================

async def escalate_ticket(
    *,
    bot,
    pdb: PremiumDB,
    channel: Optional[discord.TextChannel],
    ticket: Dict,
    guild: discord.Guild,
    escalated_by: Optional[discord.Member],
    to_panel_id: Optional[str] = None,
    reason: str = "Escalated",
) -> Dict:
    '''Escalate a ticket to another panel.

    If to_panel_id is None, looks up the configured escalation route for the
    ticket's current panel. Moves the channel into the target panel's
    category, updates the ticket row, notifies the configured role, and logs
    to ticket_escalation_history.

    Returns a dict describing what happened:
      {'ok': bool, 'to_panel': name|None, 'reason': str}
    '''
    tt = getattr(bot, 'ticket_tool', None)
    if tt is None:
        return {'ok': False, 'to_panel': None, 'reason': 'ticket system not initialized'}

    ticket_id = ticket.get('ticket_id')
    current_panel_id = ticket.get('panel_id')
    # Resolve target panel.
    target_panel = None
    if to_panel_id:
        target_panel = tt.data_manager.load_ticket_panel(to_panel_id)
    else:
        route = pdb.get_escalation_route(current_panel_id) if current_panel_id else None
        if route:
            target_panel = tt.data_manager.load_ticket_panel(route.get('to_panel_id'))
        if target_panel is None:
            # Fall back: any panel in the guild that isn't this one.
            panels = tt.data_manager.load_ticket_panels_by_guild(guild.id)
            for p in panels:
                if p.get('panel_id') != current_panel_id:
                    target_panel = p
                    break
    if target_panel is None:
        return {'ok': False, 'to_panel': None, 'reason': 'no escalation target panel found'}

    # Move the channel into the target panel's category.
    target_category_id = target_panel.get('category_id')
    if channel and target_category_id:
        target_cat = guild.get_channel(int(target_category_id))
        if isinstance(target_cat, discord.CategoryChannel):
            try:
                await channel.edit(category=target_cat,
                                   reason=f"Escalated to {target_panel.get('name')}")
            except discord.HTTPException as exc:
                logging.warning(f"[tickettool.escalation] move channel failed: {exc}")
                return {'ok': False, 'to_panel': target_panel.get('name'),
                        'reason': f'channel move failed: {exc}'}

    # Update the ticket row.
    ticket['panel_id'] = target_panel.get('panel_id')
    ticket['category'] = target_panel.get('name', ticket.get('category'))
    ticket['escalation_count'] = int(ticket.get('escalation_count') or 0) + 1
    ticket['last_escalated_at'] = datetime.now(timezone.utc).isoformat()
    # Optional priority bump.
    route = pdb.get_escalation_route(current_panel_id) if current_panel_id else None
    auto_priority = (route or {}).get('auto_escalate_priority')
    if auto_priority:
        ticket['priority'] = auto_priority
    tt.data_manager.save_ticket(ticket)

    # Notify configured role.
    notify_role_id = notify_role_id_from_route(route)
    if notify_role_id and channel:
        try:
            await channel.send(
                f"⬆️ **Escalated** to **{target_panel.get('name')}** — "
                f"<@&{notify_role_id}> please take a look.\n*Reason:* {reason}",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.escalation] notify message failed: {exc}")

    # Log to history.
    pdb.add_escalation_history({
        'ticket_id': ticket_id,
        'guild_id': guild.id,
        'from_panel_id': current_panel_id,
        'to_panel_id': target_panel.get('panel_id'),
        'escalated_by': (escalated_by.id if escalated_by else None),
        'reason': reason,
    })
    logging.info(f"[tickettool.escalation] ticket {ticket_id} -> panel "
                 f"{target_panel.get('name')} ({reason})")
    return {'ok': True, 'to_panel': target_panel.get('name'), 'reason': reason}


def notify_role_id_from_route(route: Optional[Dict]) -> Optional[int]:
    if not route:
        return None
    rid = route.get('notify_role_id')
    try:
        return int(rid) if rid else None
    except (TypeError, ValueError):
        return None


def history(pdb: PremiumDB, ticket_id: str) -> list:
    return pdb.load_escalation_history(ticket_id)
