# -*- coding: utf-8 -*-
'''
TicketTool.transcripts — Advanced Transcript Automation (Tier 1 Feature #6).

Extends Bot.py's existing transcript system (which already generates HTML,
posts to the transcripts channel, and DMs the creator) with:
  * A configurable save mode: on_close | on_delete | both | never
    (ENFORCED: 'never' skips the transcripts-channel post entirely;
    'on_delete' posts on close only when save_on_delete is enabled, because
    Bot.py's close flow deletes the channel right after this hook runs)
  * transcript_format: 'html' (default) or 'txt' (plain-text attachment)
  * disable_html_attachment: send embeds only, no file anywhere
  * Always-DM-the-creator toggle (independent of ticket_settings.dm_transcripts)
  * Custom transcript message (embed title + description) with variable support
    (including {ticket.message_count})
  * Custom transcript title
  * Auto-save to a dedicated archive channel (separate from the live
    transcripts channel) for long-term retention

Integration:
  * TicketTool.wiring.on_ticket_close() reads this config and decides whether to
    post the transcript, DM it, and/or archive it.
  * The actual HTML generation stays in TicketToolSystem._generate_transcript
    (we reuse its output rather than duplicating it).

Note (scope): Google Drive storage (Ticket Tool Pro) is intentionally NOT
    implemented here — it needs OAuth credentials the user must supply. The
    config schema leaves a `drive_folder_id`-style hook out for now; we can
    add it in Tier 2 when credentials are available.
'''

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, Optional

import discord

from .db import PremiumDB
from . import variables


SAVE_MODES = {'on_close', 'on_delete', 'both', 'never'}


# =====================================================================
# CONFIG ACCESSORS
# =====================================================================

def get_config(pdb: PremiumDB, guild_id: Optional[int] = None) -> Optional[Dict]:
    '''Load the transcript config for a guild (defaults when no row exists).

    When `guild_id` is omitted the guild's own row cannot be identified, so
    the first ENABLED row across all guilds is returned instead (or None when
    none exists). Bot.py uses that form for its coarse "is premium transcript
    handling active?" gate before posting default transcripts.
    '''
    if guild_id is None:
        try:
            cur = pdb._conn.cursor()
            cur.execute('SELECT * FROM ticket_transcript_config WHERE enabled = 1 LIMIT 1')
            row = cur.fetchone()
            return dict(row) if row else None
        except Exception as exc:
            logging.warning(f"[tickettool.transcripts] guild-less config lookup failed: {exc}")
            return None
    cfg = pdb.get_transcript_config(guild_id)
    if not cfg:
        return {
            'guild_id': guild_id,
            'save_mode': 'on_close',
            'auto_dm': 0,
            'custom_message': None,
            'custom_title': None,
            'auto_save_channel_id': None,
            'enabled': 1,
            'disable_html_attachment': 0,
            'save_on_delete': 1,
            'transcript_format': 'html',
        }
    return cfg


def save_config(pdb: PremiumDB, guild_id: int, cfg: Dict) -> None:
    pdb.upsert_transcript_config({**cfg, 'guild_id': guild_id})


# =====================================================================
# DECISION HELPERS
# =====================================================================

def should_save_on_close(cfg: Dict) -> bool:
    mode = cfg.get('save_mode', 'on_close')
    return mode in ('on_close', 'both') and cfg.get('enabled', 1)


def should_save_on_delete(cfg: Dict) -> bool:
    mode = cfg.get('save_mode', 'on_close')
    return mode in ('on_delete', 'both') and cfg.get('enabled', 1)


def should_post_channel_on_close(cfg: Dict) -> bool:
    '''Decide whether the transcripts-CHANNEL post should happen on close.

    Bot.py calls post_transcript during close, right before the channel is
    deleted — so a close IS also the delete point:
      * 'never'             -> never post to the channel
      * 'on_close' / 'both' -> post now
      * 'on_delete'         -> post only when save_on_delete is enabled
    DM and archive-channel rules apply independently of this decision.
    '''
    if not cfg or not cfg.get('enabled', 1):
        return False
    mode = cfg.get('save_mode', 'on_close')
    if mode in ('on_close', 'both'):
        return True
    if mode == 'on_delete':
        return bool(cfg.get('save_on_delete', 1))
    return False  # 'never' or unknown mode


# =====================================================================
# EMBED CUSTOMIZATION
# =====================================================================

def build_transcript_embed(
    *,
    ticket: Dict,
    channel_name: str,
    closed_by: Optional[discord.Member],
    message_count: int,
    duration_str: str,
    pdb: PremiumDB,
    guild_id: int,
    panel: Optional[Dict] = None,
    creator: Optional[discord.Member] = None,
    claimer: Optional[discord.Member] = None,
) -> discord.Embed:
    '''Build the transcript embed, honoring custom title/message + variables.'''
    cfg = get_config(pdb, guild_id)
    ctx = variables.VariableContext(
        ticket=ticket,
        panel=panel or {},
        guild={'name': '', 'id': guild_id},
        acting_user={'id': closed_by.id if closed_by else None,
                      'name': closed_by.display_name if closed_by else ''},
        claim_user={'id': claimer.id if claimer else None,
                    'name': claimer.display_name if claimer else ''},
        extra={'ticket_creator_name': creator.display_name if creator else '',
               'ticket_message_count': message_count},
    )

    title = cfg.get('custom_title') or f"📋 Ticket Transcript — {ticket.get('ticket_id')}"
    title = variables.render(title, ctx)[:256]

    desc = cfg.get('custom_message')
    if desc:
        desc = variables.render(desc, ctx)
    else:
        desc = (
            f"**Category:** {ticket.get('category','General')}\n"
            f"**Subject:** {ticket.get('subject','N/A')}"
        )

    embed = discord.Embed(
        title=title,
        description=desc[:4000],
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    creator_mention = creator.mention if creator else f"<@{ticket.get('creator_id')}>"
    embed.add_field(name="👤 Creator", value=creator_mention, inline=True)
    embed.add_field(name="🔒 Closed By",
                    value=closed_by.mention if closed_by else 'Unknown',
                    inline=True)
    embed.add_field(name="💬 Messages", value=str(message_count), inline=True)
    if claimer:
        embed.add_field(name="🙋 Claimed By", value=claimer.mention, inline=True)
    embed.add_field(name="⏱️ Duration", value=duration_str or 'N/A', inline=True)
    embed.add_field(name="#️⃣ Channel", value=f"#{channel_name}"[:256], inline=True)
    embed.set_footer(text=f"Ticket ID: {ticket.get('ticket_id')}")
    return embed


# =====================================================================
# MESSAGE COUNT + PLAIN-TEXT CONVERSION
# =====================================================================

def _count_html_messages(html: str) -> int:
    '''Count the message elements in a Bot.py HTML transcript.

    Bot.py's `_generate_html_transcript` renders each message as
    `<div class="message">...</div>`. Message content is HTML-escaped by the
    generator, so user text can never forge that marker.
    '''
    if not html:
        return 0
    return html.count('<div class="message">')


def _count_from_embed(embed) -> int:
    '''Fallback: read the Messages field from the transcript payload's embed.'''
    if embed is None:
        return 0
    try:
        for field in (getattr(embed, 'fields', None) or []):
            if 'message' in str(getattr(field, 'name', '') or '').lower():
                digits = ''.join(
                    ch for ch in str(getattr(field, 'value', '') or '') if ch.isdigit())
                if digits:
                    return int(digits)
    except Exception:
        pass
    return 0


def derive_message_count(transcript_payload: Optional[Dict]) -> int:
    '''Derive the real message count from a transcript payload.

    Order: explicit payload count -> HTML message markers -> the payload
    embed's "Messages" field -> 0.
    '''
    payload = transcript_payload or {}
    count = payload.get('message_count')
    if isinstance(count, int) and count > 0:
        return count
    n = _count_html_messages(payload.get('html') or '')
    if n > 0:
        return n
    return _count_from_embed(payload.get('embed'))


# Block-level tags that start a new line in the plain-text version.
_BLOCK_TAG_RE = re.compile(r'(?i)</?\s*(br|div|p|li|tr|table|h[1-6])\b[^>]*>')
_ANY_TAG_RE = re.compile(r'<[^>]+>')


def html_to_text(html_str: str) -> str:
    '''Convert an HTML transcript to readable plain text.

    Simple regex-based conversion: block boundaries (<br>, <div>, <p>, ...)
    become newlines, every other tag is stripped, and the basic HTML entities
    are unescaped (&amp; is replaced last so double-escaping survives intact).
    '''
    if not html_str:
        return ''
    text = _BLOCK_TAG_RE.sub('\n', html_str)
    text = _ANY_TAG_RE.sub('', text)
    text = (text.replace('&lt;', '<')
                .replace('&gt;', '>')
                .replace('&quot;', '"')
                .replace('&#39;', "'"))
    text = text.replace('&amp;', '&')
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text + ('\n' if text else '')


# =====================================================================
# POSTING / DM / ARCHIVE
# =====================================================================

async def post_transcript(
    *,
    bot,
    pdb: PremiumDB,
    guild: discord.Guild,
    ticket: Dict,
    transcript_payload: Dict,
    closed_by: Optional[discord.Member],
    panel: Optional[Dict] = None,
    creator: Optional[discord.Member] = None,
    claimer: Optional[discord.Member] = None,
) -> Dict[str, bool]:
    '''Post the transcript to the right places based on config.

    transcript_payload is the dict returned by TicketToolSystem._generate_transcript:
      {'embed': Embed, 'file': File, 'html': str}

    Enforced config (ticket_transcript_config):
      * save_mode 'never'           -> skip the transcripts-channel post
                                       entirely (DM + archive still apply).
      * save_mode 'on_delete'       -> Bot.py calls this hook during close,
                                       right before the channel is deleted, so
                                       a close IS the delete point: post only
                                       when save_on_delete is enabled.
      * save_mode 'on_close'/'both' -> post to the transcripts channel now.
      * disable_html_attachment     -> send embeds only; no file anywhere
                                       (channel post, DM and archive alike).
      * transcript_format 'txt'     -> attach a plain-text conversion instead
                                       of the HTML file (any other value: HTML).

    Returns {'posted_to_transcripts': bool, 'dm_sent': bool, 'archived': bool}.
    '''
    result = {'posted_to_transcripts': False, 'dm_sent': False, 'archived': False}
    cfg = get_config(pdb, guild.id)
    html = transcript_payload.get('html') or ''
    message_count = derive_message_count(transcript_payload)

    attach_files = not bool(cfg.get('disable_html_attachment'))
    want_txt = str(cfg.get('transcript_format') or 'html').strip().lower() == 'txt'

    def _make_file():
        '''Build a fresh File for each destination (a discord.File is single-use).'''
        if not attach_files:
            return None
        if want_txt:
            data = html_to_text(html)
            return discord.File(BytesIO(data.encode('utf-8')),
                                filename=f"transcript-{ticket.get('ticket_id')}.txt")
        return discord.File(BytesIO(html.encode('utf-8')),
                            filename=f"transcript-{ticket.get('ticket_id')}.html")

    # Rebuild the embed with the customized title/message when configured;
    # otherwise reuse the payload's own embed (it already carries the real
    # message count and message-preview fields).
    channel_name = ''
    ch = guild.get_channel(int(ticket.get('channel_id') or 0))
    if ch:
        channel_name = ch.name
    # Compute duration.
    duration_str = 'N/A'
    try:
        if ticket.get('created_at'):
            created = datetime.fromisoformat(ticket['created_at'].replace('Z', '+00:00'))
            delta = datetime.now(timezone.utc) - created
            h, rem = divmod(int(delta.total_seconds()), 3600)
            m, s = divmod(rem, 60)
            duration_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
    except Exception:
        pass

    payload_embed = transcript_payload.get('embed')
    if cfg.get('custom_title') or cfg.get('custom_message'):
        send_embed = build_transcript_embed(
            ticket=ticket, channel_name=channel_name, closed_by=closed_by,
            message_count=message_count, duration_str=duration_str, pdb=pdb,
            guild_id=guild.id, panel=panel, creator=creator, claimer=claimer,
        )
    else:
        send_embed = payload_embed

    # 1. Post to the transcripts channel (gated by save_mode / save_on_delete).
    tt = getattr(bot, 'ticket_tool', None)
    settings = tt.data_manager.load_ticket_settings(guild.id) if tt else None
    if should_post_channel_on_close(cfg):
        transcripts_channel_id = (settings or {}).get('transcripts_channel_id')
        if transcripts_channel_id:
            tc = guild.get_channel(int(transcripts_channel_id))
            if tc:
                try:
                    await tc.send(embed=send_embed, file=_make_file())
                    result['posted_to_transcripts'] = True
                except discord.HTTPException as exc:
                    logging.warning(f"[tickettool.transcripts] post to transcripts channel failed: {exc}")

    # 2. DM the creator (if auto_dm is on, or the existing settings.dm_transcripts).
    want_dm = bool(cfg.get('auto_dm')) or (settings and settings.get('dm_transcripts'))
    if want_dm and creator:
        try:
            dm_embed = discord.Embed(
                title=f"Ticket Closed — {guild.name}",
                description=(
                    f"Your ticket has been closed.\n"
                    f"**Reason:** {ticket.get('close_reason','No reason provided')}\n\n"
                    + ("Your transcript is attached below." if attach_files
                       else "The full transcript has been saved.")
                ),
                color=discord.Color.orange(),
            )
            await creator.send(embed=dm_embed, file=_make_file())
            result['dm_sent'] = True
        except discord.Forbidden:
            logging.info(f"[tickettool.transcripts] cannot DM creator {creator.id} (DMs off)")
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.transcripts] DM transcript failed: {exc}")

    # 3. Archive to a dedicated long-term channel if configured.
    archive_id = cfg.get('auto_save_channel_id')
    if archive_id:
        ac = guild.get_channel(int(archive_id))
        if ac:
            try:
                await ac.send(embed=send_embed, file=_make_file())
                result['archived'] = True
            except discord.HTTPException as exc:
                logging.warning(f"[tickettool.transcripts] archive channel post failed: {exc}")

    return result
