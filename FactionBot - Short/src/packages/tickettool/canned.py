# -*- coding: utf-8 -*-
'''
TicketTool.canned — Canned Replies (Ticket Tool /canned feature).

Staff-saved reusable response snippets that can be inserted into ticket
channels on demand (with Discord autocomplete on the name). Non-AI, fully
self-contained — this is the free-tier Ticket Tool feature, not their AI
smart replies.

Contents support the full TicketTool.variables template engine, so e.g.:

    Hello {ticket.user}, thanks for contacting support!
    Panel: {panel.name} • claimed by {claim.user}

Integration:
  * TicketTool.commands registers the /canned hybrid group (add / edit /
    delete / list / send) with name autocomplete.
  * Sending renders the snippet against the current ticket's variable
    context and records a usage count.
'''

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import discord

from .db import PremiumDB
from . import variables


MAX_NAME_LEN = 32
MAX_CONTENT_LEN = 1900  # keep under Discord's 2000-char message cap


# =====================================================================
# VALIDATION
# =====================================================================

def validate_name(name: str) -> Optional[str]:
    '''Return a normalized name, or None when invalid.

    Names are lowercase; letters, digits, '-' and '_' only (they appear in
    autocomplete choices, so keep them clean). Names longer than 32
    characters are rejected rather than truncated.'''
    cleaned = (name or '').strip().lower()
    if not cleaned or len(cleaned) > MAX_NAME_LEN:
        return None
    cleaned = ''.join(ch if (ch.isalnum() or ch in '-_') else '-' for ch in cleaned)
    cleaned = cleaned.strip('-_') or None
    return cleaned


def validate_content(content: str) -> Optional[str]:
    '''Return trimmed content, or None when empty/too long.'''
    cleaned = (content or '').strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_CONTENT_LEN:
        return None
    return cleaned


# =====================================================================
# CRUD
# =====================================================================

def get_reply(pdb: PremiumDB, guild_id: int, name: str) -> Optional[Dict]:
    return pdb.get_canned_reply(guild_id, (name or '').strip().lower())


def list_replies(pdb: PremiumDB, guild_id: int) -> List[Dict]:
    return pdb.list_canned_replies(guild_id)


def save_reply(pdb: PremiumDB, guild_id: int, name: str, content: str,
               created_by: Optional[int]) -> Dict:
    '''Create or update a canned reply. Returns a result dict:
    {'ok': bool, 'created': bool, 'error': Optional[str]}'''
    clean_name = validate_name(name)
    if not clean_name:
        return {'ok': False, 'created': False,
                'error': 'Name must be 1-32 characters (letters, digits, - and _).'}
    clean_content = validate_content(content)
    if not clean_content:
        return {'ok': False, 'created': False, 'error': 'Content is required (max 1900 characters).'}
    reply_id, created = pdb.upsert_canned_reply({
        'guild_id': guild_id,
        'name': clean_name,
        'content': clean_content,
        'created_by': created_by,
    })
    return {'ok': True, 'created': created, 'error': None, 'reply_id': reply_id}


def delete_reply(pdb: PremiumDB, guild_id: int, name: str) -> bool:
    return pdb.delete_canned_reply(guild_id, (name or '').strip().lower())


# =====================================================================
# RENDERING
# =====================================================================

def render_reply(reply: Dict, *, ticket: Optional[Dict] = None,
                 panel: Optional[Dict] = None,
                 guild: Optional[Dict] = None,
                 acting_user: Optional[Dict] = None) -> str:
    '''Render a canned reply's content with the ticket variable context.'''
    claimer = None
    if ticket and ticket.get('claimed_by') and guild and guild.get('id'):
        # claim.user.name etc. — best-effort; the caller can pass a richer
        # claimer dict when it has the member object.
        claimer = ticket.get('_claimer') or {'id': ticket.get('claimed_by'),
                                             'name': f"<@{ticket.get('claimed_by')}>"}
    ctx = variables.VariableContext(
        ticket=ticket or {},
        panel=panel or {},
        guild=guild or {},
        claim_user=claimer,
        acting_user=acting_user or {},
        extra={'ticket_creator_name': (ticket or {}).get('creator_name', '')},
    )
    try:
        return variables.render(reply.get('content') or '', ctx)
    except Exception as exc:
        logging.warning(f"[canned] variable render failed for {reply.get('name')}: {exc}")
        return reply.get('content') or ''


async def send_reply(*, bot, pdb: PremiumDB, ctx_or_interaction,
                     channel: discord.TextChannel, guild: discord.Guild,
                     name: str, staff_member) -> Dict:
    '''Resolve + send a canned reply into a ticket channel.

    Returns a result dict: {'ok': bool, 'error': Optional[str]}.'''
    tt = getattr(bot, 'ticket_tool', None)
    if tt is None:
        return {'ok': False, 'error': 'Ticket system not initialized.'}

    ticket = tt.data_manager.load_ticket_by_channel(channel.id)
    if not ticket:
        return {'ok': False, 'error': 'This is not a ticket channel.'}

    reply = get_reply(pdb, guild.id, name)
    if not reply:
        return {'ok': False,
                'error': f"No canned reply named `{(name or '').strip().lower()}`. Use `!canned list`."}

    panel = None
    if ticket.get('panel_id'):
        panel = tt.data_manager.load_ticket_panel(ticket['panel_id'])
    claimer_dict = None
    if ticket.get('claimed_by'):
        member = guild.get_member(int(ticket.get('claimed_by')))
        if member:
            claimer_dict = {'id': member.id, 'name': member.display_name}
            ticket['_claimer'] = claimer_dict

    content = render_reply(
        reply, ticket=ticket, panel=panel,
        guild={'id': guild.id, 'name': guild.name},
        acting_user={'id': staff_member.id, 'name': staff_member.display_name},
    )
    try:
        await channel.send(content or reply.get('content'))
    except discord.HTTPException as exc:
        return {'ok': False, 'error': f'Could not send the reply: {exc}'}

    try:
        pdb.record_canned_reply_use(reply['reply_id'])
    except Exception as exc:
        logging.debug(f"[canned] use tracking failed: {exc}")
    return {'ok': True, 'error': None}


# =====================================================================
# EMBEDS
# =====================================================================

def build_list_embed(replies: List[Dict]) -> discord.Embed:
    embed = discord.Embed(
        title="💬 Canned Replies",
        color=discord.Color.blurple(),
        description="Saved response snippets staff can insert into tickets with `!canned send <name>`.",
    )
    if not replies:
        embed.description += "\n\n*None saved yet.* Add one with `!canned add <name> <content>`."
        return embed
    lines = []
    for r in replies[:25]:
        preview = (r.get('content') or '').replace('\n', ' ')
        if len(preview) > 80:
            preview = preview[:77] + '...'
        lines.append(f"`{r.get('name')}` — {preview} *(used {r.get('uses', 0)}×)*")
    embed.description = (embed.description or '') + '\n\n' + '\n'.join(lines)
    if len(replies) > 25:
        embed.set_footer(text=f"Showing 25 of {len(replies)} replies")
    return embed
