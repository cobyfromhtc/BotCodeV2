# -*- coding: utf-8 -*-
"""FactionBot TicketTool — premium ticket-system feature package.

26 modules covering analytics, automations, claiming, escalation, flows,
SLA tracking, transcripts, staff threads and more. Import order mirrors the
dependency order of the original inline loader, so relative imports
(``from .db import ...``) resolve identically.
"""

from . import (
    db, variables, naming, scheduling, claiming, role_automation,
    automations, escalation, transcripts, sla, analytics,
    command_shortcuts,
    kb, thread_tickets, staff_threads, channel_recycle,
    i18n, branded_replies, flows, custom_commands,
    multi_embed, moderator_messages, flow_reviews,
    canned,
    wiring, commands,
)

__all__ = [
    "db", "variables", "naming", "scheduling", "claiming", "role_automation",
    "automations", "escalation", "transcripts", "sla", "analytics",
    "command_shortcuts", "kb", "thread_tickets", "staff_threads",
    "channel_recycle", "i18n", "branded_replies", "flows", "custom_commands",
    "multi_embed", "moderator_messages", "flow_reviews", "canned",
    "wiring", "commands",
]
