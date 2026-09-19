# -*- coding: utf-8 -*-
'''
TicketTool.role_automation — Open/Close/Claim Role Automation (Tier 1 Feature #3).

Per-panel rules that add/remove Discord roles on the ticket creator when:
  * the ticket is opened  (open_add_roles / open_remove_roles)
  * the ticket is closed  (close_add_roles / close_remove_roles)
  * the ticket is claimed  (claim_add_roles / claim_remove_roles — applied to CLAIMER)
  * the ticket is unclaimed (unclaim_add_roles / unclaim_remove_roles — applied to CLAIMER)

Integration (called from TicketTool.wiring):
  * on_ticket_create(...)   -> apply_open(...)
  * on_ticket_close(...)    -> apply_close(...)
  * on_ticket_claim(...)    -> apply_claim(...)
  * on_ticket_unclaim(...)  -> apply_unclaim(...)

All role mutations are best-effort: a missing role or permission failure is
logged and does not abort the surrounding ticket operation. This matches the
existing Bot.py convention (side effects never roll back the primary action).
'''

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import discord

from .db import PremiumDB, _json_loads_list


EVENT_OPEN = 'open'
EVENT_CLOSE = 'close'
EVENT_CLAIM = 'claim'
EVENT_UNCLAIM = 'unclaim'

_VALID_EVENTS = {EVENT_OPEN, EVENT_CLOSE, EVENT_CLAIM, EVENT_UNCLAIM}


# =====================================================================
# CONFIG ACCESSORS
# =====================================================================

def get_config(pdb: PremiumDB, panel_id: str) -> Dict:
    cfg = pdb.get_role_automation(panel_id)
    if not cfg:
        return {
            'panel_id': panel_id,
            'open_add_roles': [], 'open_remove_roles': [],
            'close_add_roles': [], 'close_remove_roles': [],
            'claim_add_roles': [], 'claim_remove_roles': [],
            'unclaim_add_roles': [], 'unclaim_remove_roles': [],
        }
    return {
        'panel_id': panel_id,
        'open_add_roles': _json_loads_list(cfg.get('open_add_roles')),
        'open_remove_roles': _json_loads_list(cfg.get('open_remove_roles')),
        'close_add_roles': _json_loads_list(cfg.get('close_add_roles')),
        'close_remove_roles': _json_loads_list(cfg.get('close_remove_roles')),
        'claim_add_roles': _json_loads_list(cfg.get('claim_add_roles')),
        'claim_remove_roles': _json_loads_list(cfg.get('claim_remove_roles')),
        'unclaim_add_roles': _json_loads_list(cfg.get('unclaim_add_roles')),
        'unclaim_remove_roles': _json_loads_list(cfg.get('unclaim_remove_roles')),
    }


def save_config(pdb: PremiumDB, panel_id: str, guild_id: int, cfg: Dict) -> None:
    pdb.upsert_role_automation({**cfg, 'panel_id': panel_id, 'guild_id': guild_id})


def set_event_roles(pdb: PremiumDB, panel_id: str, guild_id: int, event: str,
                    *, add_roles: Optional[List[int]] = None,
                    remove_roles: Optional[List[int]] = None) -> None:
    '''Convenience setter for a single event's add/remove role lists.'''
    if event not in _VALID_EVENTS:
        raise ValueError(f"invalid event {event!r}")
    cfg = get_config(pdb, panel_id)
    if add_roles is not None:
        cfg[f'{event}_add_roles'] = [int(r) for r in add_roles]
    if remove_roles is not None:
        cfg[f'{event}_remove_roles'] = [int(r) for r in remove_roles]
    save_config(pdb, panel_id, guild_id, cfg)


# =====================================================================
# ROLE APPLICATION
# =====================================================================

async def _apply_to_member(member: discord.Member, *,
                           add: List[int], remove: List[int],
                           reason: str) -> Dict[str, List[int]]:
    '''Apply role add/remove to a member. Returns {'added':[...],'removed':[...]}.

    Silently skips roles that don't exist or that the bot can't manage
    (hierarchy / missing perms). Never raises.
    '''
    added: List[int] = []
    removed: List[int] = []
    guild = member.guild
    # Resolve role objects once.
    add_objs = [guild.get_role(int(rid)) for rid in add if rid]
    rem_objs = [guild.get_role(int(rid)) for rid in remove if rid]
    add_objs = [r for r in add_objs if r is not None]
    rem_objs = [r for r in rem_objs if r is not None]
    # Filter out roles the bot can't touch.
    me = guild.me
    bot_top = me.top_role.position if me.top_role else 0
    add_objs = [r for r in add_objs if r.position < bot_top and r not in member.roles]
    rem_objs = [r for r in rem_objs if r.position < bot_top and r in member.roles]
    if add_objs:
        try:
            await member.add_roles(*add_objs, reason=reason)
            added = [r.id for r in add_objs]
        except discord.Forbidden:
            logging.warning(f"[tickettool.role_auto] forbidden adding roles to {member}")
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.role_auto] add_roles HTTP error: {exc}")
    if rem_objs:
        try:
            await member.remove_roles(*rem_objs, reason=reason)
            removed = [r.id for r in rem_objs]
        except discord.Forbidden:
            logging.warning(f"[tickettool.role_auto] forbidden removing roles from {member}")
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.role_auto] remove_roles HTTP error: {exc}")
    return {'added': added, 'removed': removed}


async def apply_open(pdb: PremiumDB, member: discord.Member, panel: Optional[Dict]) -> Dict:
    '''Add/remove roles on the ticket creator when their ticket opens.'''
    if not panel:
        return {'added': [], 'removed': []}
    cfg = get_config(pdb, panel.get('panel_id'))
    return await _apply_to_member(
        member,
        add=cfg['open_add_roles'],
        remove=cfg['open_remove_roles'],
        reason=f"Ticket opened in panel {panel.get('name','?')}",
    )


async def apply_close(pdb: PremiumDB, member: discord.Member, panel: Optional[Dict]) -> Dict:
    '''Add/remove roles on the ticket creator when their ticket closes.'''
    if not panel:
        return {'added': [], 'removed': []}
    cfg = get_config(pdb, panel.get('panel_id'))
    return await _apply_to_member(
        member,
        add=cfg['close_add_roles'],
        remove=cfg['close_remove_roles'],
        reason=f"Ticket closed in panel {panel.get('name','?')}",
    )


async def apply_claim(pdb: PremiumDB, claimer: discord.Member, panel: Optional[Dict]) -> Dict:
    '''Add/remove roles on the CLAIMER when they claim a ticket.'''
    if not panel:
        return {'added': [], 'removed': []}
    cfg = get_config(pdb, panel.get('panel_id'))
    return await _apply_to_member(
        claimer,
        add=cfg['claim_add_roles'],
        remove=cfg['claim_remove_roles'],
        reason=f"Claimed ticket in panel {panel.get('name','?')}",
    )


async def apply_unclaim(pdb: PremiumDB, claimer: discord.Member, panel: Optional[Dict]) -> Dict:
    '''Reverse claim roles when a claim is released.'''
    if not panel:
        return {'added': [], 'removed': []}
    cfg = get_config(pdb, panel.get('panel_id'))
    # On unclaim: undo claim_add by removing, undo claim_remove by adding back.
    return await _apply_to_member(
        claimer,
        add=cfg['unclaim_add_roles'],
        remove=cfg['unclaim_remove_roles'],
        reason=f"Unclaimed ticket in panel {panel.get('name','?')}",
    )
