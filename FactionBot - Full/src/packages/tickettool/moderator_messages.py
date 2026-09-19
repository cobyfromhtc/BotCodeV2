# -*- coding: utf-8 -*-
'''
TicketTool.moderator_messages — Advanced Moderator Messages (Tier 3 Feature #9).

Thin re-export of the moderator-message functions from TicketTool.multi_embed,
so callers can `from premium import moderator_messages` with a clean API.

The actual implementation lives in TicketTool.multi_embed (it shares the
embed_from_data / build_buttons_view helpers with multi-embed panel messages).
'''

from .multi_embed import (
    MODERATOR_EVENT_TYPES,
    set_moderator_message,
    get_moderator_message,
    delete_moderator_message,
    build_buttons_view,
    send_moderator_message,
)

__all__ = [
    'MODERATOR_EVENT_TYPES',
    'set_moderator_message',
    'get_moderator_message',
    'delete_moderator_message',
    'build_buttons_view',
    'send_moderator_message',
]
