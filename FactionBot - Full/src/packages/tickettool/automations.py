# -*- coding: utf-8 -*-
'''
TicketTool.automations — Full Ticket Automation Engine (Tier 1 Feature #1).

A general-purpose trigger/condition/action engine attached to ticket panels.
Supports up to N automations per panel (we impose no hard cap; Ticket Tool
Premium documented 10).

TRIGGERS (trigger_type):
  * created         — fires when a ticket is created in this panel
  * closed          — fires when a ticket is closed
  * reopened        — fires when a closed ticket is reopened
  * owner_left      — fires when the ticket creator leaves the guild
  * close_request   — fires when the creator clicks "Close Ticket"
  * claim           — fires when a staff member claims the ticket
  * unclaim         — fires when a claim is released
  * delayed         — fires `delay_seconds` after ticket creation
  * no_response     — fires when no staff message arrives within delay_seconds

CONDITIONS (each a {field, op, value} dict):
  * field: subject | priority | category | claimed | has_claim | ticket_count
           | creator_id | hour_of_day | weekday
  * op:    eq | ne | contains | in | not_in | gt | lt | exists | not_exists

ACTIONS (each a {type, ...} dict):
  * close        {reason}
  * delete       {reason}
  * claim        {user_id?}            (defaults to a configured staff role / bot)
  * unclaim
  * add_role     {role_id, target: 'creator'|'claimer'}
  * remove_role  {role_id, target}
  * send_message {channel: 'ticket'|'transcripts'|'dm', content}  ('dm'
                   messages the ticket creator — TicketTool DM automations)
  * rename       {template}
  * move         {panel_id}
  * escalate     {to_panel_id, reason}
  * execute_command {command}  (e.g. "!priority high")
  * start_automation {automation_id}
  * stop_automation  {automation_id}

Integration:
  * TicketTool.wiring.on_ticket_* fire the relevant triggers via fire_event().
  * The delayed + no_response triggers spawn asyncio tasks whose timers are
    persisted (ticket_automation_timers) so they survive restarts.
'''

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord  # used for Forbidden/HTTPException/Status handling below

from .db import PremiumDB, _json_loads_list, _json_loads_dict


# =====================================================================
# TRIGGER / CONDITION / ACTION CONSTANTS
# =====================================================================

TRIGGERS = {
    'created', 'closed', 'reopened', 'owner_left', 'close_request',
    'claim', 'unclaim', 'delayed', 'no_response',
}

ACTION_TYPES = {
    'close', 'delete', 'claim', 'unclaim',
    'add_role', 'remove_role',
    'send_message', 'rename', 'move', 'escalate',
    'execute_command', 'start_automation', 'stop_automation',
}

CONDITION_OPS = {'eq', 'ne', 'contains', 'in', 'not_in', 'gt', 'lt',
                 'exists', 'not_exists'}


# =====================================================================
# EVENT PAYLOAD
# =====================================================================

class AutomationEvent:
    '''Bundles the runtime context a trigger evaluation needs.

    Built by TicketTool.wiring before calling fire_event().
    '''

    def __init__(self, *, trigger: str, ticket: Dict, panel: Optional[Dict],
                 guild: Any, actor: Optional[Dict] = None, bot=None,
                 ticket_count: Optional[int] = None):
        self.trigger = trigger
        self.ticket = ticket or {}
        self.panel = panel or {}
        self.guild = guild
        self.actor = actor or {}        # {'id','name','mention'}
        self.bot = bot
        self.ticket_count = ticket_count


# =====================================================================
# CONFIG ACCESSORS
# =====================================================================

def list_automations(pdb: PremiumDB, panel_id: str) -> List[Dict]:
    rows = pdb.list_automations(panel_id)
    for r in rows:
        r['conditions'] = _json_loads_list(r.get('conditions'))
        r['actions'] = _json_loads_list(r.get('actions'))
    return rows


def get_automation(pdb: PremiumDB, automation_id: str) -> Optional[Dict]:
    r = pdb.get_automation(automation_id)
    if r:
        r['conditions'] = _json_loads_list(r.get('conditions'))
        r['actions'] = _json_loads_list(r.get('actions'))
    return r


def create_or_update(pdb: PremiumDB, *, panel_id: str, guild_id: int,
                     name: str, trigger_type: str,
                     conditions: List[Dict], actions: List[Dict],
                     delay_seconds: int = 0, enabled: bool = True,
                     automation_id: Optional[str] = None) -> str:
    if trigger_type not in TRIGGERS:
        raise ValueError(f"invalid trigger_type {trigger_type!r}")
    for a in actions:
        if a.get('type') not in ACTION_TYPES:
            raise ValueError(f"invalid action type {a.get('type')!r}")
    for c in conditions:
        if c.get('op') not in CONDITION_OPS:
            raise ValueError(f"invalid condition op {c.get('op')!r}")
    return pdb.upsert_automation({
        'automation_id': automation_id,
        'panel_id': panel_id,
        'guild_id': guild_id,
        'name': name,
        'trigger_type': trigger_type,
        'delay_seconds': delay_seconds,
        'conditions': conditions,
        'actions': actions,
        'enabled': enabled,
    })


def delete(pdb: PremiumDB, automation_id: str) -> bool:
    return pdb.delete_automation(automation_id)


def toggle(pdb: PremiumDB, automation_id: str, enabled: bool) -> bool:
    a = pdb.get_automation(automation_id)
    if not a:
        return False
    a['enabled'] = 1 if enabled else 0
    a['conditions'] = a.get('conditions') or '[]'
    a['actions'] = a.get('actions') or '[]'
    pdb.upsert_automation(a)
    return True


# =====================================================================
# CONDITION EVALUATION
# =====================================================================

def _ticket_field(ticket: Dict, field: str, event: AutomationEvent) -> str:
    if field == 'subject':
        return str(ticket.get('subject') or '')
    if field == 'priority':
        return str(ticket.get('priority') or 'normal')
    if field == 'category':
        return str(ticket.get('category') or '')
    if field == 'claimed':
        return '1' if ticket.get('claimed_by') else '0'
    if field == 'has_claim':
        return '1' if ticket.get('claimed_by') else '0'
    if field == 'ticket_count':
        return str(event.ticket_count or 0)
    if field == 'creator_id':
        return str(ticket.get('creator_id') or '')
    if field == 'hour_of_day':
        return str(datetime.now(timezone.utc).hour)
    if field == 'weekday':
        return ['monday','tuesday','wednesday','thursday','friday','saturday','sunday'][datetime.now(timezone.utc).weekday()]
    return ''


def _eval_condition(cond: Dict, ticket: Dict, event: AutomationEvent) -> bool:
    field = cond.get('field')
    op = cond.get('op')
    expected = cond.get('value')
    if not field or not op:
        return True  # treat malformed condition as pass-through
    actual = _ticket_field(ticket, field, event)
    if op == 'eq':
        return str(actual) == str(expected)
    if op == 'ne':
        return str(actual) != str(expected)
    if op == 'contains':
        return str(expected) in str(actual)
    if op == 'in':
        items = expected if isinstance(expected, list) else str(expected).split(',')
        return str(actual) in [str(x).strip() for x in items]
    if op == 'not_in':
        items = expected if isinstance(expected, list) else str(expected).split(',')
        return str(actual) not in [str(x).strip() for x in items]
    if op == 'gt':
        try:
            return float(actual) > float(expected)
        except (TypeError, ValueError):
            return False
    if op == 'lt':
        try:
            return float(actual) < float(expected)
        except (TypeError, ValueError):
            return False
    if op == 'exists':
        return bool(actual)
    if op == 'not_exists':
        return not actual
    return True


def _eval_conditions(conditions: List[Dict], ticket: Dict, event: AutomationEvent) -> bool:
    return all(_eval_condition(c, ticket, event) for c in (conditions or []))


# =====================================================================
# ACTION EXECUTION
# =====================================================================

async def _execute_action(bot, pdb: PremiumDB, action: Dict, event: AutomationEvent) -> None:
    '''Execute one action. All errors are logged, never raised.'''
    atype = action.get('type')
    guild = event.guild
    ticket = event.ticket
    channel = None
    try:
        channel = guild.get_channel(int(ticket.get('channel_id'))) if ticket.get('channel_id') else None
    except (TypeError, ValueError):
        pass

    try:
        if atype == 'close':
            reason = action.get('reason', 'Closed by automation')
            await _automation_close(bot, guild, ticket, reason, event.actor)
        elif atype == 'delete':
            reason = action.get('reason', 'Deleted by automation')
            await _automation_delete(bot, guild, ticket, reason)
        elif atype == 'claim':
            await _automation_claim(bot, guild, ticket, action.get('user_id'))
        elif atype == 'unclaim':
            await _automation_unclaim(bot, guild, ticket)
        elif atype == 'add_role':
            await _automation_role(bot, guild, ticket, action, add=True)
        elif atype == 'remove_role':
            await _automation_role(bot, guild, ticket, action, add=False)
        elif atype == 'send_message':
            await _automation_send_message(bot, guild, ticket, channel, action)
        elif atype == 'rename':
            await _automation_rename(channel, action)
        elif atype == 'move':
            await _automation_move(bot, guild, channel, ticket, action)
        elif atype == 'escalate':
            from . import escalation as esc
            await esc.escalate_ticket(
                bot=bot, pdb=pdb, channel=channel, ticket=ticket,
                guild=guild, escalated_by=None,
                to_panel_id=action.get('to_panel_id'),
                reason=action.get('reason', 'Automated escalation'),
            )
        elif atype == 'execute_command':
            await _automation_execute_command(bot, channel, ticket, action)
        elif atype == 'start_automation':
            tid = action.get('automation_id')
            if tid:
                toggle(pdb, tid, enabled=True)
        elif atype == 'stop_automation':
            tid = action.get('automation_id')
            if tid:
                toggle(pdb, tid, enabled=False)
    except Exception as exc:
        logging.warning(f"[tickettool.automations] action {atype} failed: {exc}")


async def _automation_close(bot, guild, ticket: Dict, reason: str, actor: Dict) -> None:
    ticket_tool = getattr(bot, 'ticket_tool', None) or _get_global_ticket_tool()
    if not ticket_tool:
        return
    channel = guild.get_channel(int(ticket.get('channel_id') or 0))
    if not channel:
        return
    closer = guild.get_member(int((actor or {}).get('id') or 0)) or guild.me
    await ticket_tool.close_ticket(channel, closer, reason)


async def _automation_delete(bot, guild, ticket: Dict, reason: str) -> None:
    '''Delete a ticket channel + mark ticket deleted (no transcript).'''
    channel_id = ticket.get('channel_id')
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    # Cancel any pending automations for this ticket.
    # Save transcript config decides whether to save first.
    try:
        if channel:
            await channel.delete(reason=reason)
    except Exception as exc:
        logging.warning(f"[tickettool.automations] delete channel failed: {exc}")
    ticket['status'] = 'closed'
    ticket['close_reason'] = reason
    ticket['closed_at'] = datetime.now(timezone.utc).isoformat()
    try:
        _get_global_ticket_tool().data_manager.save_ticket(ticket)
    except Exception:
        pass


async def _automation_claim(bot, guild, ticket: Dict, user_id: Optional[int]) -> None:
    ticket_tool = getattr(bot, 'ticket_tool', None) or _get_global_ticket_tool()
    if not ticket_tool:
        return
    channel = guild.get_channel(int(ticket.get('channel_id') or 0))
    if not channel:
        return
    claimer = guild.get_member(int(user_id or 0)) if user_id else None
    if claimer is None:
        # Default: assign to the highest-ranked available support-role member.
        claimer = _pick_first_support_member(guild, ticket)
    if claimer is None:
        return
    await ticket_tool.claim_ticket(channel, claimer)


async def _automation_unclaim(bot, guild, ticket: Dict) -> None:
    ticket_tool = getattr(bot, 'ticket_tool', None) or _get_global_ticket_tool()
    if not ticket_tool:
        return
    channel = guild.get_channel(int(ticket.get('channel_id') or 0))
    if not channel:
        return
    claimer_id = ticket.get('claimed_by')
    claimer = guild.get_member(int(claimer_id)) if claimer_id else None
    if claimer is None:
        claimer = guild.me
    await ticket_tool.unclaim_ticket(channel, claimer)


async def _automation_role(bot, guild, ticket: Dict, action: Dict, *, add: bool) -> None:
    role_id = action.get('role_id')
    if not role_id:
        return
    role = guild.get_role(int(role_id))
    if not role:
        return
    target = action.get('target', 'creator')
    member_id = ticket.get('creator_id') if target == 'creator' else ticket.get('claimed_by')
    if not member_id:
        return
    member = guild.get_member(int(member_id))
    if not member:
        return
    try:
        if add:
            await member.add_roles(role, reason='Ticket automation')
        else:
            await member.remove_roles(role, reason='Ticket automation')
    except discord.Forbidden:
        logging.warning(f"[tickettool.automations] role {role_id} forbidden")
    except discord.HTTPException as exc:
        logging.warning(f"[tickettool.automations] role HTTP error: {exc}")


async def _automation_send_message(bot, guild, ticket: Dict, channel, action: Dict) -> None:
    target = action.get('channel', 'ticket')
    content = str(action.get('content', ''))
    if not content:
        return
    # TicketTool-style DM automation: channel 'dm' sends the message to the
    # ticket CREATOR's direct messages (used for created-DM / closed-DM
    # automations).
    if target == 'dm':
        creator_id = ticket.get('creator_id') if ticket else None
        if not creator_id:
            return
        try:
            creator = guild.get_member(int(creator_id))
            if creator is None:
                return
            await creator.send(content[:1900])
        except discord.Forbidden:
            logging.info(f"[tickettool.automations] DM to creator {creator_id} blocked (DMs off)")
        except discord.HTTPException as exc:
            logging.warning(f"[tickettool.automations] DM send_message failed: {exc}")
        return
    dest = channel
    if target == 'transcripts':
        settings = _get_global_ticket_tool().data_manager.load_ticket_settings(guild.id) if _get_global_ticket_tool() else None
        if settings and settings.get('transcripts_channel_id'):
            dest = guild.get_channel(int(settings['transcripts_channel_id']))
    if dest is None:
        return
    try:
        await dest.send(content[:1900])
    except discord.HTTPException as exc:
        logging.warning(f"[tickettool.automations] send_message failed: {exc}")


async def _automation_rename(channel, action: Dict) -> None:
    if channel is None:
        return
    template = action.get('template')
    if not template:
        return
    safe = re.sub(r'[^a-z0-9_-]', '-', (template or '').lower())
    safe = re.sub(r'-+', '-', safe).strip('-') or 'ticket'
    try:
        await channel.edit(name=safe[:100])
    except discord.HTTPException as exc:
        logging.warning(f"[tickettool.automations] rename failed: {exc}")


async def _automation_move(bot, guild, channel, ticket: Dict, action: Dict) -> None:
    if channel is None:
        return
    panel_id = action.get('panel_id')
    if not panel_id:
        return
    panel = _get_global_ticket_tool().data_manager.load_ticket_panel(panel_id) if _get_global_ticket_tool() else None
    if not panel:
        return
    cat_id = panel.get('category_id')
    if not cat_id:
        return
    cat = guild.get_channel(int(cat_id))
    try:
        await channel.edit(category=cat)
        ticket['panel_id'] = panel_id
        ticket['category'] = panel.get('name', 'General')
        _get_global_ticket_tool().data_manager.save_ticket(ticket)
    except discord.HTTPException as exc:
        logging.warning(f"[tickettool.automations] move failed: {exc}")


async def _automation_execute_command(bot, channel, ticket: Dict, action: Dict) -> None:
    '''Run a prefix command as the bot owner inside the ticket channel.'''
    command = action.get('command', '').strip()
    if not command or channel is None:
        return
    # We can't easily synthesize a full Context; instead, dispatch the small
    # subset of automation-relevant commands directly.
    from . import command_shortcuts as cs
    await cs.run_automation_command(bot, channel, ticket, command)


def _pick_first_support_member(guild, ticket: Dict):
    '''Pick the first online support-role member for an auto-claim.'''
    ticket_tool = _get_global_ticket_tool()
    if not ticket_tool:
        return None
    panel_id = ticket.get('panel_id')
    panel = ticket_tool.data_manager.load_ticket_panel(panel_id) if panel_id else None
    support_role_id = (panel or {}).get('support_role_id')
    if not support_role_id:
        settings = ticket_tool.data_manager.load_ticket_settings(guild.id)
        support_role_id = settings.get('support_role_id') if settings else None
    if not support_role_id:
        return None
    role = guild.get_role(int(support_role_id))
    if not role:
        return None
    for m in role.members:
        if m.status != discord.Status.offline and not m.bot:
            return m
    # Fallback: any member with the role.
    for m in role.members:
        if not m.bot:
            return m
    return None


# =====================================================================
# EVENT FIRING
# =====================================================================

# A handle the wiring layer sets so this module can reach the live
# TicketToolSystem + bot without circular imports.
_TICKET_TOOL_REF = {'tt': None, 'bot': None}


def set_global_refs(bot, ticket_tool) -> None:
    _TICKET_TOOL_REF['bot'] = bot
    _TICKET_TOOL_REF['tt'] = ticket_tool


def _get_global_ticket_tool():
    return _TICKET_TOOL_REF['tt']


def _get_bot():
    return _TICKET_TOOL_REF['bot']


# =====================================================================
# TICKET AUTOMATION PAUSE (Ticket Tool /pause + /resume)
# =====================================================================

def is_ticket_paused(ticket: Optional[Dict]) -> bool:
    '''Pure check: is this ticket's automation pause ACTIVE right now?

    A pause is active when automation_paused=1 AND (there is no auto-resume
    deadline OR the deadline is still in the future). Expired pauses read as
    not-paused; callers clear them lazily via clear_expired_pause().
    '''
    if not ticket or not int(ticket.get('automation_paused') or 0):
        return False
    until = ticket.get('automation_paused_until')
    if until:
        try:
            deadline = datetime.fromisoformat(str(until).replace('Z', '+00:00'))
            if deadline <= datetime.now(timezone.utc):
                return False  # expired
        except (ValueError, TypeError):
            pass
    return True


def resume_ticket_automations(bot, pdb: PremiumDB, ticket: Dict) -> Dict:
    '''Explicit resume (/resume command or lazy auto-resume).

    Shifts every pending automation timer forward by the elapsed pause
    duration (paused time doesn't count toward no_response/delayed timers),
    clears the pause columns, and persists the ticket.'''
    dm = None
    tt = _get_global_ticket_tool() or getattr(bot, 'ticket_tool', None)
    if tt is not None:
        dm = tt.data_manager
    if dm is None:
        return {'paused_seconds': 0, 'shifted': 0}
    now = datetime.now(timezone.utc)
    paused_seconds = 0
    paused_at_raw = ticket.get('automation_paused_at')
    if paused_at_raw:
        try:
            paused_at = datetime.fromisoformat(str(paused_at_raw).replace('Z', '+00:00'))
            paused_seconds = max(0, int((now - paused_at).total_seconds()))
        except (ValueError, TypeError):
            paused_seconds = 0
    shifted = 0
    if paused_seconds > 0:
        try:
            shifted = pdb.shift_timers_for_ticket(ticket.get('ticket_id'), paused_seconds)
        except Exception as exc:
            logging.warning(f"[automations] timer shift on resume failed: {exc}")
    ticket['automation_paused'] = 0
    ticket['automation_paused_at'] = None
    ticket['automation_paused_until'] = None
    ticket['automation_resumed_at'] = now.isoformat()
    try:
        dm.save_ticket(ticket)
    except Exception as exc:
        logging.warning(f"[automations] resume save failed: {exc}")
    return {'paused_seconds': paused_seconds, 'shifted': shifted}


def _ticket_pause_active_for(bot, pdb: PremiumDB, ticket_id: Optional[str]) -> bool:
    '''Fresh pause check by ticket id (event.ticket dicts can be stale).

    Handles lazy auto-resume of expired pauses (with timer shifting).'''
    if not ticket_id:
        return False
    tt = _get_global_ticket_tool() or getattr(bot, 'ticket_tool', None)
    if tt is None:
        return False
    try:
        ticket = tt.data_manager.load_ticket(ticket_id)
    except Exception:
        return False
    if not ticket:
        return False
    if is_ticket_paused(ticket):
        return True
    # Not actively paused — clear + timer-shift if it just expired.
    if int(ticket.get('automation_paused') or 0):
        try:
            clear_expired_pause_with_timers(bot, pdb, ticket)
        except Exception as exc:
            logging.debug(f"[automations] lazy pause-expiry failed: {exc}")
    return False


def clear_expired_pause_with_timers(bot, pdb: PremiumDB, ticket: Dict) -> bool:
    '''clear_expired_pause, but shifts pending timers via the premium DB.'''
    until = ticket.get('automation_paused_until')
    if not until or not int(ticket.get('automation_paused') or 0):
        return False
    try:
        deadline = datetime.fromisoformat(str(until).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return False
    now = datetime.now(timezone.utc)
    if deadline > now:
        return False
    resume_ticket_automations(bot, pdb, ticket)
    return True


async def fire_event(bot, pdb: PremiumDB, event: AutomationEvent) -> None:
    '''Fire all enabled automations whose trigger matches the event.

    Called by TicketTool.wiring.on_ticket_* hooks. Best-effort: any failure in
    one automation does not abort the others.

    PAUSED TICKETS: all event-driven automations are suppressed while the
    ticket's automation pause is active (Ticket Tool /pause semantics).
    '''
    panel = event.panel
    panel_id = panel.get('panel_id') if panel else None
    if not panel_id:
        return
    # Pause gate — fresh DB check (event.ticket may predate the pause).
    ticket_id = (event.ticket or {}).get('ticket_id')
    if ticket_id and _ticket_pause_active_for(bot, pdb, ticket_id):
        logging.info(f"[automations] suppressed '{event.trigger}' for ticket {ticket_id} (paused)")
        return
    automations = pdb.list_automations_by_trigger(panel_id, event.trigger)
    for auto in automations:
        conditions = _json_loads_list(auto.get('conditions'))
        actions = _json_loads_list(auto.get('actions'))
        if not _eval_conditions(conditions, event.ticket, event):
            continue
        for action in actions:
            await _execute_action(bot, pdb, action, event)


# =====================================================================
# DELAYED + NO_RESPONSE TIMERS
# =====================================================================

def schedule_delayed_timer(pdb: PremiumDB, *, bot, automation_id: str,
                           ticket_id: str, guild_id: int, delay_seconds: int) -> None:
    '''Persist a delayed-automation timer so it survives restarts.'''
    import uuid as _uuid
    fire_at = (datetime.now(timezone.utc) + timedelta(seconds=max(1, delay_seconds))).isoformat()
    pdb.save_automation_timer({
        'timer_id': str(_uuid.uuid4())[:8],
        'automation_id': automation_id,
        'ticket_id': ticket_id,
        'guild_id': guild_id,
        'fire_at': fire_at,
        'fired': 0,
    })


def schedule_delayed_for_new_ticket(bot, pdb: PremiumDB, *, panel: Dict, ticket: Dict) -> None:
    '''For a freshly-created ticket, arm any 'delayed' or 'no_response' automations.'''
    panel_id = panel.get('panel_id') if panel else None
    if not panel_id:
        return
    for trig in ('delayed', 'no_response'):
        for auto in pdb.list_automations_by_trigger(panel_id, trig):
            delay = int(auto.get('delay_seconds', 0) or 0)
            if delay <= 0:
                continue
            schedule_delayed_timer(
                pdb, bot=bot, automation_id=auto['automation_id'],
                ticket_id=ticket['ticket_id'], guild_id=ticket.get('guild_id'),
                delay_seconds=delay,
            )


async def process_due_timers(bot, pdb: PremiumDB) -> int:
    '''Fire every not-yet-fired timer whose fire_at has passed.

    Returns the number of timers fired. Called from a background task every
    minute (registered in TicketTool.wiring.on_ready).
    '''
    fired = 0
    now = datetime.now(timezone.utc)
    for timer in pdb.load_pending_timers():
        try:
            fire_at = datetime.fromisoformat(timer['fire_at'].replace('Z', '+00:00'))
        except Exception:
            pdb.mark_timer_fired(timer['timer_id'])
            continue
        if fire_at > now:
            continue
        # Fire it.
        auto = pdb.get_automation(timer['automation_id'])
        if not auto or not auto.get('enabled'):
            pdb.mark_timer_fired(timer['timer_id'])
            continue
        tt = _get_global_ticket_tool()
        if not tt:
            pdb.mark_timer_fired(timer['timer_id'])
            continue
        ticket = tt.data_manager.load_ticket(timer['ticket_id'])
        if not ticket or ticket.get('status') != 'open':
            pdb.mark_timer_fired(timer['timer_id'])
            continue
        # PAUSED TICKETS: skip due timers while the pause is active. Expired
        # pauses are lazily resumed here (timers shifted by the pause length).
        if ticket.get('automation_paused'):
            if is_ticket_paused(ticket):
                continue
            try:
                clear_expired_pause_with_timers(bot, pdb, ticket)
                continue  # timers were shifted; re-evaluated on a later tick
            except Exception as exc:
                logging.debug(f"[automations] lazy pause-expiry failed: {exc}")
        # For 'no_response': only fire if still no staff response.
        if auto.get('trigger_type') == 'no_response':
            if ticket.get('staff_responded_at') or ticket.get('first_response_at'):
                pdb.mark_timer_fired(timer['timer_id'])
                continue
        panel = tt.data_manager.load_ticket_panel(ticket.get('panel_id') or '') or {}
        guild = bot.get_guild(int(ticket.get('guild_id') or 0))
        if guild is None:
            pdb.mark_timer_fired(timer['timer_id'])
            continue
        event = AutomationEvent(
            trigger=auto.get('trigger_type'),
            ticket=ticket, panel=panel, guild=guild, bot=bot,
        )
        actions = _json_loads_list(auto.get('actions'))
        for action in actions:
            await _execute_action(bot, pdb, action, event)
        pdb.mark_timer_fired(timer['timer_id'])
        fired += 1
    return fired


def cancel_timers_for_ticket(pdb: PremiumDB, ticket_id: str) -> int:
    '''Cancel pending delayed automations for a ticket (used on close).'''
    return pdb.cancel_timers_for_ticket(ticket_id)
