# -*- coding: utf-8 -*-
'''
TicketTool.multi_embed — Multi-Embed Panel Messages + Advanced Moderator
Messages (Tier 3 Features #9 + #10).

Two related capabilities:

1. MULTI-EMBED PANEL MESSAGES (#10)
   Replaces the single embed_title/embed_description on ticket_panels with
   support for up to 10 embeds per panel, each with up to 25 fields. The
   embeds are stored in the panel_embeds table and rendered in order_index.

   embed_data JSON shape:
     {
       "title": "...",
       "description": "...",
       "color": "0x5865F2",       # hex string or int
       "url": "...",
       "thumbnail": {"url": "..."},
       "image": {"url": "..."},
       "author": {"name": "...", "icon_url": "..."},
       "footer": {"text": "...", "icon_url": "..."},
       "fields": [{"name": "...", "value": "...", "inline": true}, ...],
       "timestamp": true           # adds current timestamp
     }

2. ADVANCED MODERATOR MESSAGES (#9)
   Customizable messages shown on ticket lifecycle events (close, reopen,
   delete, claim, unclaim). Each message supports:
     - Plain text content (variable-rendered)
     - Multiple embeds (same shape as above)
     - Buttons (up to 25 per message)

   button JSON shape:
     {"label": "...", "style": "primary|secondary|success|danger|link",
      "emoji": "🎫", "custom_id": "mod_action:close", "url": "..."}

   Configured per-panel per-event-type.

Integration:
  * TicketTool.wiring.on_ticket_close -> moderator_messages.send_for_event(... 'close')
  * TicketTool.wiring.on_ticket_reopen -> moderator_messages.send_for_event(... 'reopen')
  * TicketTool.wiring.on_ticket_claim -> moderator_messages.send_for_event(... 'claim')
  * TicketTool.wiring.on_ticket_unclaim -> moderator_messages.send_for_event(... 'unclaim')
  * TicketPanelView (Bot.py) -> multi_embed.build_panel_message(panel) to
    render multi-embed panel messages instead of the single embed.
'''

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord

from .db import PremiumDB
from . import variables


# =====================================================================
# EMBED DATA -> discord.Embed
# =====================================================================

MAX_EMBEDS_PER_MESSAGE = 10
MAX_FIELDS_PER_EMBED = 25


def _parse_color(color_val) -> int:
    '''Accept int, hex string ('0x5865F2' or '#5865F2'), or plain hex.'''
    if color_val is None:
        return 0x5865F2
    if isinstance(color_val, (int, float)):
        return int(color_val)
    s = str(color_val).strip().lstrip('#').lstrip('0x').lstrip('0X')
    try:
        return int(s, 16)
    except ValueError:
        return 0x5865F2


def _parse_style(style_val) -> discord.ButtonStyle:
    '''Accept string or int style.'''
    if isinstance(style_val, (int, float)):
        try:
            return discord.ButtonStyle(int(style_val))
        except (ValueError, TypeError):
            return discord.ButtonStyle.secondary
    s = str(style_val or 'secondary').strip().lower()
    return {
        'primary': discord.ButtonStyle.primary,
        'secondary': discord.ButtonStyle.secondary,
        'success': discord.ButtonStyle.success,
        'danger': discord.ButtonStyle.danger,
        'link': discord.ButtonStyle.link,
    }.get(s, discord.ButtonStyle.secondary)


def embed_from_data(data: Dict, *, ctx: Optional[variables.VariableContext] = None) -> discord.Embed:
    '''Build a discord.Embed from a JSON-serializable embed_data dict.

    If `ctx` is provided, all string values are rendered through the variable
    engine first (so {ticket.user}, {claim.user}, etc. work in embed fields).
    '''
    def _r(text):
        '''Render a string through the variable engine if ctx is provided.'''
        if not text or ctx is None:
            return text or ''
        return variables.render(str(text), ctx)

    title = _r(data.get('title')) or None
    description = _r(data.get('description')) or None
    color = _parse_color(data.get('color'))
    embed = discord.Embed(
        title=title[:256] if title else None,
        description=description[:4096] if description else None,
        color=discord.Color(color),
    )
    if data.get('url'):
        embed.url = str(data['url'])
    if data.get('timestamp'):
        embed.timestamp = datetime.now(timezone.utc)
    # Thumbnail
    thumb = data.get('thumbnail') or {}
    if isinstance(thumb, dict) and thumb.get('url'):
        embed.set_thumbnail(url=str(thumb['url']))
    elif isinstance(thumb, str):
        embed.set_thumbnail(url=thumb)
    # Image
    image = data.get('image') or {}
    if isinstance(image, dict) and image.get('url'):
        embed.set_image(url=str(image['url']))
    elif isinstance(image, str):
        embed.set_image(url=image)
    # Author
    author = data.get('author') or {}
    if isinstance(author, dict) and author.get('name'):
        embed.set_author(
            name=_r(author['name'])[:256],
            url=str(author.get('url') or ''),
            icon_url=str(author.get('icon_url') or '') or None,
        )
    # Footer
    footer = data.get('footer') or {}
    if isinstance(footer, dict) and footer.get('text'):
        embed.set_footer(
            text=_r(footer['text'])[:2048],
            icon_url=str(footer.get('icon_url') or '') or None,
        )
    # Fields
    for field in (data.get('fields') or [])[:MAX_FIELDS_PER_EMBED]:
        if not isinstance(field, dict):
            continue
        name = _r(field.get('name') or '\u200b')[:256]
        value = _r(field.get('value') or '\u200b')[:1024]
        inline = bool(field.get('inline', True))
        embed.add_field(name=name, value=value, inline=inline)
    return embed


def embed_to_data(embed: discord.Embed) -> Dict:
    '''Inverse of embed_from_data: extract a JSON-serializable dict from a
    live discord.Embed. Used when migrating existing single-embed panels.
    '''
    data = {}
    if embed.title:
        data['title'] = embed.title
    if embed.description:
        data['description'] = embed.description
    if embed.color:
        data['color'] = f"0x{embed.color.value:06X}"
    if embed.url:
        data['url'] = embed.url
    if embed.thumbnail and embed.thumbnail.url:
        data['thumbnail'] = {'url': embed.thumbnail.url}
    if embed.image and embed.image.url:
        data['image'] = {'url': embed.image.url}
    if embed.author:
        data['author'] = {
            'name': embed.author.name,
            'icon_url': embed.author.icon_url or '',
        }
    if embed.footer:
        data['footer'] = {
            'text': embed.footer.text or '',
            'icon_url': embed.footer.icon_url or '',
        }
    if embed.fields:
        data['fields'] = [
            {'name': f.name, 'value': f.value, 'inline': f.inline}
            for f in embed.fields
        ]
    return data


# =====================================================================
# MULTI-EMBED PANEL MESSAGES (Feature #10)
# =====================================================================

def add_panel_embed(pdb: PremiumDB, *, panel_id: str, guild_id: int,
                    embed_data: Dict, order_index: Optional[int] = None) -> str:
    '''Add an embed to a panel. Auto-assigns order_index if not provided.'''
    existing = pdb.list_panel_embeds(panel_id)
    if len(existing) >= MAX_EMBEDS_PER_MESSAGE:
        raise ValueError(f"Panel already has the maximum of {MAX_EMBEDS_PER_MESSAGE} embeds.")
    if order_index is None:
        order_index = len(existing)
    return pdb.save_panel_embed({
        'panel_id': panel_id,
        'guild_id': guild_id,
        'order_index': order_index,
        'embed_data': embed_data,
        'is_active': 1,
    })


def update_panel_embed(pdb: PremiumDB, embed_id: str, embed_data: Dict) -> bool:
    '''Update an existing panel embed's data.'''
    embeds = pdb.list_panel_embeds('')  # need the specific one
    # We need to fetch the embed by ID — list_panel_embeds is per-panel, so
    # we'll use the raw accessor.
    # Actually we can just call save_panel_embed with the existing ID.
    pdb.save_panel_embed({
        'embed_id': embed_id,
        'embed_data': embed_data,
        'is_active': 1,
    })
    return True


def remove_panel_embed(pdb: PremiumDB, embed_id: str) -> bool:
    return pdb.delete_panel_embed(embed_id)


def reorder_panel_embeds(pdb: PremiumDB, panel_id: str, embed_order: List[str]) -> None:
    pdb.reorder_panel_embeds(panel_id, embed_order)


def build_panel_embeds(pdb: PremiumDB, panel: Dict,
                        *, ctx: Optional[variables.VariableContext] = None) -> List[discord.Embed]:
    '''Build a list of discord.Embed objects for a panel.

    Falls back to the single embed_title/embed_description on the panel row
    if no multi-embeds are configured (backward compatible).
    '''
    panel_id = panel.get('panel_id')
    embeds: List[discord.Embed] = []
    if panel_id:
        rows = pdb.list_panel_embeds(panel_id)
        for row in rows:
            try:
                embeds.append(embed_from_data(row.get('embed_data', {}), ctx=ctx))
            except Exception as exc:
                logging.warning(f"[multi_embed] failed to build embed {row.get('embed_id')}: {exc}")
    if not embeds:
        # Backward-compatible fallback: build a single embed from the panel row.
        embed = discord.Embed(
            title=panel.get('embed_title') or 'Support Tickets',
            description=panel.get('embed_description') or 'Click the button below to create a ticket.',
            color=discord.Color(panel.get('embed_color', 0x5865F2)),
        )
        if panel.get('embed_thumbnail'):
            embed.set_thumbnail(url=panel['embed_thumbnail'])
        if panel.get('embed_image'):
            embed.set_image(url=panel['embed_image'])
        embeds.append(embed)
    return embeds[:MAX_EMBEDS_PER_MESSAGE]


def is_multi_embed_enabled(panel: Optional[Dict]) -> bool:
    if not panel:
        return False
    return bool(int(panel.get('use_multi_embed', 0) or 0))


def enable_multi_embed(pdb: PremiumDB, data_manager, panel_id: str, *, enabled: bool) -> bool:
    try:
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel:
            return False
        panel['use_multi_embed'] = 1 if enabled else 0
        data_manager.save_ticket_panel(panel)
        return True
    except Exception as exc:
        logging.warning(f"[multi_embed] enable_multi_embed failed: {exc}")
        return False


# =====================================================================
# ADVANCED MODERATOR MESSAGES (Feature #9)
# =====================================================================

MODERATOR_EVENT_TYPES = {'close', 'reopen', 'delete', 'claim', 'unclaim', 'create'}


def set_moderator_message(pdb: PremiumDB, *, panel_id: str, guild_id: int,
                           event_type: str, content: Optional[str] = None,
                           embeds: Optional[List[Dict]] = None,
                           buttons: Optional[List[Dict]] = None,
                           enabled: bool = True) -> str:
    '''Create or update a moderator message for a panel + event.'''
    if event_type not in MODERATOR_EVENT_TYPES:
        raise ValueError(f"Invalid event_type. Use one of: {', '.join(sorted(MODERATOR_EVENT_TYPES))}")
    return pdb.upsert_moderator_message({
        'panel_id': panel_id,
        'guild_id': guild_id,
        'event_type': event_type,
        'content': content,
        'embeds': embeds or [],
        'buttons': buttons or [],
        'enabled': enabled,
    })


def get_moderator_message(pdb: PremiumDB, panel_id: str, event_type: str) -> Optional[Dict]:
    return pdb.get_moderator_message(panel_id, event_type)


def delete_moderator_message(pdb: PremiumDB, message_id: str) -> bool:
    return pdb.delete_moderator_message(message_id)


def build_buttons_view(buttons_data: List[Dict]) -> Optional[discord.ui.View]:
    '''Build a discord.ui.View from a list of button config dicts.'''
    if not buttons_data:
        return None
    view = discord.ui.View(timeout=None)
    for i, btn_data in enumerate(buttons_data[:25]):
        try:
            style = _parse_style(btn_data.get('style', 'secondary'))
            label = str(btn_data.get('label', ''))[:80] or None
            emoji = btn_data.get('emoji') or None
            custom_id = btn_data.get('custom_id')
            url = btn_data.get('url')
            # Link buttons use url; action buttons use custom_id.
            if style == discord.ButtonStyle.link and url:
                btn = discord.ui.Button(
                    style=style, label=label, emoji=emoji, url=url,
                )
            else:
                if not custom_id:
                    custom_id = f"modbtn:{i}"
                btn = discord.ui.Button(
                    style=style, label=label, emoji=emoji, custom_id=custom_id,
                )
            view.add_item(btn)
        except Exception as exc:
            logging.warning(f"[multi_embed] button build failed: {exc}")
    return view


async def send_moderator_message(*, bot, pdb: PremiumDB, channel: discord.TextChannel,
                                   panel_id: str, event_type: str,
                                   ticket: Dict, panel: Optional[Dict] = None,
                                   actor: Optional[discord.Member] = None,
                                   extra: Optional[Dict] = None) -> bool:
    '''Send the configured moderator message for a panel + event.

    Returns True if a message was sent, False if no config exists or it's
    disabled.
    '''
    cfg = pdb.get_moderator_message(panel_id, event_type)
    if not cfg or not cfg.get('enabled'):
        return False

    # Build the variable context for rendering.
    claimer = None
    if ticket.get('claimed_by') and channel.guild:
        claimer = channel.guild.get_member(int(ticket['claimed_by']))
    ctx = variables.VariableContext(
        ticket=ticket,
        panel=panel or {},
        guild={'id': channel.guild.id, 'name': channel.guild.name},
        claim_user={'id': claimer.id, 'name': claimer.display_name} if claimer else {},
        acting_user={'id': actor.id, 'name': actor.display_name} if actor else {},
        extra=extra or {},
    )

    # Render content.
    content = None
    if cfg.get('content'):
        content = variables.render(cfg['content'], ctx)[:1900]

    # Build embeds.
    embeds: List[discord.Embed] = []
    for embed_data in (cfg.get('embeds') or [])[:MAX_EMBEDS_PER_MESSAGE]:
        try:
            embeds.append(embed_from_data(embed_data, ctx=ctx))
        except Exception as exc:
            logging.warning(f"[multi_embed] moderator embed build failed: {exc}")

    # Build buttons view.
    view = build_buttons_view(cfg.get('buttons') or [])

    # Send.
    try:
        # discord.py allows up to 10 embeds per message.
        if embeds and content:
            await channel.send(content=content, embeds=embeds[:10],
                                view=view)
        elif embeds:
            await channel.send(embeds=embeds[:10], view=view)
        elif content:
            await channel.send(content=content, view=view)
        else:
            return False  # nothing to send
        return True
    except discord.HTTPException as exc:
        logging.warning(f"[multi_embed] moderator message send failed: {exc}")
        return False
