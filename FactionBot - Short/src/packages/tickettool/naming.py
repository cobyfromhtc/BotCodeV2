# -*- coding: utf-8 -*-
'''
TicketTool.naming — Advanced Ticket Naming + Variables (Tier 1 Features #5 + #8).

Adds:
  * Automatic ticket-naming templates per panel, with separate templates for
    open / closed / claimed tickets (Ticket Tool premium feature).
  * A guild-wide ticket counter ({ticket.count}) with configurable zero
    padding (e.g. #57 -> #0057 with padding=4). Supports up to 20 chars of
    padding (matches Ticket Tool's documented limit).
  * Full variable support in templates via TicketTool.variables.

Integration:
  * TicketToolSystem.create_ticket calls naming.compute_open_name(panel, ctx)
    to get the channel name + subject.
  * On close, naming.compute_closed_name(...) can rename the channel.
  * On claim, naming.compute_claimed_name(...) can rename the channel.

Everything is pure logic + DB; no discord imports here except the type hints.
'''

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from . import variables
from .db import PremiumDB


# Discord channel-name limits (we enforce these so templates can never
# produce an invalid name and crash create_text_channel).
MAX_CHANNEL_NAME_LEN = 100
MAX_SUBJECT_LEN = 1000


def _sanitize_channel_name(name: str) -> str:
    '''Make a string safe for use as a Discord channel name.

    Discord allows lowercase letters, numbers, '-' and '_'. Spaces become '-'.
    Everything else is stripped. Lowercased. Truncated to 100 chars.
    '''
    if not name:
        return 'ticket'
    cleaned = []
    for ch in name.strip().lower():
        if ch.isalnum() or ch in '-_':
            cleaned.append(ch)
        elif ch.isspace():
            cleaned.append('-')
        # else: drop it
    result = ''.join(cleaned)
    # Collapse runs of '-' and strip leading/trailing '-'
    result = re.sub(r'-+', '-', result).strip('-')
    if not result:
        result = 'ticket'
    return result[:MAX_CHANNEL_NAME_LEN]


def _pad_number(value: int, width: int) -> str:
    '''Zero-pad a number to `width` digits (max 20, matching Ticket Tool).'''
    if width <= 0:
        return str(value)
    width = min(width, 20)
    return str(value).zfill(width)


# =====================================================================
# TEMPLATE RESOLUTION
# =====================================================================

def get_config(pdb: PremiumDB, panel_id: str) -> Optional[Dict]:
    return pdb.get_naming(panel_id)


def _padded_count(cfg: Optional[Dict], count) :
    '''Apply the panel's configured zero-padding to a ticket number.

    TicketTool "Ticket Padding" premium feature: pad(57, 4) -> '0057'.
    Previously the padding config was stored but never applied to the
    {ticket.count} variable in naming templates.
    '''
    if cfg is None or count is None:
        return count
    try:
        width = int(cfg.get('number_padding', 0) or 0)
    except (TypeError, ValueError):
        width = 0
    if width <= 0:
        return count
    return _pad_number(int(count), width)


def compute_open_name(
    pdb: PremiumDB,
    panel: Dict,
    *,
    guild: Dict,
    ticket_id: str,
    creator: Dict,
    ticket_count: int,
    subject: Optional[str] = None,
    tz_offset_hours: float = 0.0,
) -> Tuple[str, str]:
    '''Compute (channel_name, subject) for a newly-opened ticket.

    Falls back to the existing Bot.py naming convention
    (`ticket-{username}-{ticket_id}`) when no template is configured, so the
    behavior is identical to before unless the owner opts in.
    '''
    panel_id = panel.get('panel_id')
    cfg = pdb.get_naming(panel_id) if panel_id else None
    count = ticket_count or pdb.reserve_ticket_number_safe(guild.get('id', 0))

    ctx = variables.VariableContext(
        ticket={'ticket_id': ticket_id, 'subject': subject,
                'creator_id': creator.get('id') if creator else None},
        ticket_count=_padded_count(cfg, count),
        panel=panel,
        guild=guild,
        acting_user=creator,
        extra={'ticket_creator_name': (creator or {}).get('name', '')},
        tz_offset_hours=tz_offset_hours,
    )

    if cfg and cfg.get('open_template'):
        rendered = variables.render(cfg['open_template'], ctx)
        channel_name = _sanitize_channel_name(rendered) or f'ticket-{ticket_id}'
    else:
        # Backward-compatible default: ticket-{username}-{ticket_id}
        uname = (creator or {}).get('name', 'user')
        safe_uname = ''.join(c if c.isalnum() or c == '-' else '-' for c in uname.lower())[:40]
        channel_name = f'ticket-{safe_uname}-{ticket_id}'[:MAX_CHANNEL_NAME_LEN]

    # Subject: use provided, else a template, else the panel name.
    rendered_subject = subject
    if not rendered_subject and cfg and cfg.get('open_template'):
        # If the owner used a {ticket.subject} var in the open template but
        # did not pass a subject, leave the subject blank (it can be set later).
        rendered_subject = ''
    return channel_name, (rendered_subject or '')[:MAX_SUBJECT_LEN]


def compute_closed_name(
    pdb: PremiumDB,
    panel: Dict,
    *,
    guild: Dict,
    ticket: Dict,
    ticket_count: Optional[int] = None,
    closer: Optional[Dict] = None,
    tz_offset_hours: float = 0.0,
) -> Optional[str]:
    '''Return the channel name to apply on close, or None to leave as-is.'''
    panel_id = panel.get('panel_id') if panel else None
    cfg = pdb.get_naming(panel_id) if panel_id else None
    if not cfg or not cfg.get('closed_template'):
        return None
    ctx = variables.VariableContext(
        ticket=ticket,
        ticket_count=_padded_count(cfg, ticket_count),
        panel=panel,
        guild=guild,
        acting_user=closer or {},
        extra={'ticket_creator_name': ''},
        tz_offset_hours=tz_offset_hours,
    )
    rendered = variables.render(cfg['closed_template'], ctx)
    return _sanitize_channel_name(rendered) or None


def compute_claimed_name(
    pdb: PremiumDB,
    panel: Dict,
    *,
    guild: Dict,
    ticket: Dict,
    claimer: Dict,
    ticket_count: Optional[int] = None,
    tz_offset_hours: float = 0.0,
) -> Optional[str]:
    '''Return the channel name to apply on claim, or None to leave as-is.'''
    panel_id = panel.get('panel_id') if panel else None
    cfg = pdb.get_naming(panel_id) if panel_id else None
    if not cfg or not cfg.get('claimed_template'):
        return None
    ctx = variables.VariableContext(
        ticket=ticket,
        ticket_count=_padded_count(cfg, ticket_count),
        panel=panel,
        guild=guild,
        claim_user=claimer,
        acting_user=claimer,
        extra={'ticket_creator_name': ''},
        tz_offset_hours=tz_offset_hours,
    )
    rendered = variables.render(cfg['claimed_template'], ctx)
    return _sanitize_channel_name(rendered) or None


def compute_unclaimed_name(
    pdb: PremiumDB,
    panel: Dict,
    *,
    guild: Dict,
    ticket: Dict,
    ticket_count: Optional[int] = None,
    tz_offset_hours: float = 0.0,
) -> Optional[str]:
    '''On unclaim, restore the open-template name (if configured).'''
    panel_id = panel.get('panel_id') if panel else None
    cfg = pdb.get_naming(panel_id) if panel_id else None
    if not cfg or not cfg.get('open_template'):
        return None
    ctx = variables.VariableContext(
        ticket=ticket,
        ticket_count=_padded_count(cfg, ticket_count),
        panel=panel,
        guild=guild,
        extra={'ticket_creator_name': ''},
        tz_offset_hours=tz_offset_hours,
    )
    rendered = variables.render(cfg['open_template'], ctx)
    return _sanitize_channel_name(rendered) or None


# =====================================================================
# COUNTER HELPERS
# =====================================================================

def reserve_number(pdb: PremiumDB, guild_id: int) -> int:
    '''Atomically reserve the next guild ticket number.'''
    return pdb.reserve_ticket_number(guild_id)


def pad(pdb: PremiumDB, panel_id: str, number: int) -> str:
    '''Apply a panel's configured padding to a ticket number.'''
    cfg = pdb.get_naming(panel_id)
    width = int(cfg.get('number_padding', 0)) if cfg else 0
    return _pad_number(number, width)


# Small wrapper used by compute_open_name when no count was supplied — keeps
# the public API simple while still reserving atomically.
def _reserve_safe(self, guild_id):
    try:
        return self.reserve_ticket_number(guild_id)
    except Exception:
        return 1

PremiumDB.reserve_ticket_number_safe = _reserve_safe  # type: ignore[attr-defined]
