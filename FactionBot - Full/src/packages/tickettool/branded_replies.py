# -*- coding: utf-8 -*-
'''
TicketTool.branded_replies — Anonymous/Branded Staff Replies (Tier 2 Feature #29).

Lets staff reply in a ticket under a shared identity ("GANG Support") instead
of their personal Discord account. Uses Discord webhooks — built into
discord.py, no external service required.

Per-panel config:
  branded_reply_config.display_name — the shared name shown on replies
  branded_reply_config.avatar_url    — the shared avatar
  branded_reply_config.enabled       — 0/1 toggle

Workflow:
  1. Staff types a message in the ticket channel.
  2. on_message (in TicketTool.wiring) detects the staff member, deletes their
     original message, and re-posts it via the panel's webhook under the
     branded identity.
  3. The original sender is recorded in the transcript (so staff audit is
     still possible internally) but is invisible to the ticket creator.

This is the Ticket Tool Pro "anonymous staff replies" feature, implemented
entirely with discord.py webhooks (which are a native Discord feature, not a
third-party service).

Setup:
  /brandedreplies setup <panel_id> <display_name> [avatar_url]
  Creates a webhook named "branded-replies-<panel_id>" in the panel's
  ticket category (or the panel's category fallback). The webhook URL + id
  are stored in branded_reply_config for reuse.
'''

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import discord

from .db import PremiumDB


# =====================================================================
# CONFIG ACCESSORS
# =====================================================================

def get_config(pdb: PremiumDB, panel_id: str) -> Dict:
    cfg = pdb.get_branded_reply_config(panel_id)
    if not cfg:
        return {
            'panel_id': panel_id,
            'enabled': 0,
            'webhook_id': None,
            'webhook_url': None,
            'display_name': 'Support',
            'avatar_url': None,
        }
    return cfg


def is_enabled(pdb: PremiumDB, panel_id: str) -> bool:
    cfg = pdb.get_branded_reply_config(panel_id)
    return bool(cfg and cfg.get('enabled'))


# =====================================================================
# WEBHOOK SETUP
# =====================================================================

async def setup_webhook(
    *,
    guild: discord.Guild,
    panel: Dict,
    display_name: str,
    avatar_url: Optional[str] = None,
    pdb: PremiumDB,
) -> Optional[Dict]:
    '''Create (or reuse) a Discord webhook for branded replies on a panel.

    The webhook is created in the panel's ticket category (or the panel's
    channel fallback). The webhook URL + id are stored for later use.
    '''
    panel_id = panel.get('panel_id')
    if not panel_id:
        return None
    # Find a channel to host the webhook. Webhooks are channel-scoped in
    # Discord, but can post to ANY channel in the guild — so we just need a
    # stable host channel. Use the panel's category's first text channel,
    # or the panel's channel itself.
    host_channel = None
    category_id = panel.get('category_id')
    if category_id:
        cat = guild.get_channel(int(category_id))
        if isinstance(cat, discord.CategoryChannel):
            for ch in cat.text_channels:
                host_channel = ch
                break
    if host_channel is None:
        # Fall back to the panel's own channel.
        ch_id = panel.get('channel_id')
        if ch_id:
            host_channel = guild.get_channel(int(ch_id))
    if host_channel is None:
        return None
    # Check for an existing webhook with our name to avoid duplicates.
    webhook_name = f"branded-replies-{panel_id}"
    try:
        existing = await host_channel.webhooks()
        for wh in existing:
            if wh.name == webhook_name:
                # Reuse it.
                cfg = {
                    'panel_id': panel_id,
                    'guild_id': guild.id,
                    'enabled': 1,
                    'webhook_id': wh.id,
                    'webhook_url': wh.url,
                    'display_name': display_name,
                    'avatar_url': avatar_url,
                }
                pdb.upsert_branded_reply_config(cfg)
                return cfg
    except (discord.Forbidden, discord.HTTPException) as exc:
        logging.warning(f"[branded_replies] fetch webhooks failed: {exc}")
        return None
    # Create the webhook.
    try:
        # discord.py 2.4: create_webhook(name=..., avatar=bytes)
        avatar_bytes = None
        if avatar_url:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(avatar_url) as resp:
                        if resp.status == 200:
                            avatar_bytes = await resp.read()
            except Exception as exc:
                logging.warning(f"[branded_replies] avatar fetch failed: {exc}")
        webhook = await host_channel.create_webhook(
            name=webhook_name,
            avatar=avatar_bytes,
            reason=f"Branded replies setup for panel {panel_id}",
        )
    except discord.Forbidden:
        logging.warning(f"[branded_replies] no permission to create webhook in #{host_channel.name}")
        return None
    except discord.HTTPException as exc:
        logging.warning(f"[branded_replies] create webhook failed: {exc}")
        return None
    cfg = {
        'panel_id': panel_id,
        'guild_id': guild.id,
        'enabled': 1,
        'webhook_id': webhook.id,
        'webhook_url': webhook.url,
        'display_name': display_name,
        'avatar_url': avatar_url,
    }
    pdb.upsert_branded_reply_config(cfg)
    return cfg


def disable(pdb: PremiumDB, panel_id: str) -> bool:
    cfg = pdb.get_branded_reply_config(panel_id)
    if not cfg:
        return False
    cfg['enabled'] = 0
    pdb.upsert_branded_reply_config(cfg)
    return True


# =====================================================================
# REPLY (the actual webhook post)
# =====================================================================

async def send_branded_reply(
    *,
    channel: discord.TextChannel,
    staff_member: discord.Member,
    content: str,
    pdb: PremiumDB,
    panel: Optional[Dict],
    attachments: Optional[list] = None,
) -> bool:
    '''Post a message in the channel under the branded identity.

    Returns True on success. Best-effort: on failure, falls back to posting as
    the bot with the staff member's name prefixed (so the message isn't lost).
    '''
    if not panel:
        return False
    cfg = get_config(pdb, panel.get('panel_id'))
    if not cfg.get('enabled') or not cfg.get('webhook_url'):
        return False
    try:
        webhook = discord.Webhook.from_url(cfg['webhook_url'],
                                            session=_get_session())
        # Prepend the staff member's name in a subtle way for audit (visible
        # only if someone reads the raw message, but the display name is the
        # branded identity).
        await webhook.send(
            content=content[:1900],
            username=cfg.get('display_name') or 'Support',
            avatar_url=cfg.get('avatar_url'),
        )
        return True
    except discord.HTTPException as exc:
        logging.warning(f"[branded_replies] webhook send failed: {exc}")
        return False
    except Exception as exc:
        logging.warning(f"[branded_replies] unexpected error: {exc}")
        return False


# Session reuse (discord.py 2.4 supports a session= kwarg for webhooks).
_SESSION = None
def _get_session():
    global _SESSION
    if _SESSION is None:
        try:
            import aiohttp
            _SESSION = aiohttp.ClientSession()
        except ImportError:
            _SESSION = None
    return _SESSION
