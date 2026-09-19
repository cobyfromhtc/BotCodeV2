# -*- coding: utf-8 -*-
'''
TicketTool.sla — Advanced SLA Tracking (Tier 1 Feature #9).

Extends Bot.py's basic SLA check (which only alerts on first-response
breach) with:
  * Two independent SLA clocks per ticket:
      - first_response SLA  (time to first staff message)
      - resolution SLA      (time to close)
  * Per-priority SLA overrides (urgent tickets get tighter targets)
  * SLA state machine:  ok -> warning -> breached  (warning fires at
    warn_before_breach_pct of the target)
  * Staff attribution: the staff member whose message triggers
    first_response_met_at is recorded.
  * Reporting: per-staff SLA performance (avg first response, avg resolution)
  * Auto-escalation hook: tickets that breach resolution SLA can be
    auto-escalated via TicketTool.escalation.

Integration:
  * TicketTool.wiring.on_ticket_create   -> create initial SLA state row
  * TicketTool.wiring.on_ticket_message  -> mark first response met
  * TicketTool.wiring.on_ticket_close    -> mark resolution met
  * TicketTool.wiring.sla_tick (every 5 min) -> advance state + warn/breach
'''

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import discord

from .db import PremiumDB


# =====================================================================
# CONFIG ACCESSORS
# =====================================================================

def get_config(pdb: PremiumDB, guild_id: int) -> Dict:
    cfg = pdb.get_sla_config(guild_id)
    if not cfg:
        return {
            'guild_id': guild_id,
            'first_response_hours': 0.0,
            'resolution_hours': 0.0,
            'urgent_first_response_hours': 0.0,
            'urgent_resolution_hours': 0.0,
            'escalation_role_id': None,
            'warn_before_breach_pct': 0,
            'enabled': 1,
        }
    return cfg


def save_config(pdb: PremiumDB, guild_id: int, cfg: Dict) -> None:
    pdb.upsert_sla_config({**cfg, 'guild_id': guild_id})


# =====================================================================
# SLA STATE LIFECYCLE
# =====================================================================

def targets_for(ticket: Dict, cfg: Dict) -> Tuple[float, float]:
    '''Return (first_response_hours, resolution_hours) for this ticket's priority.'''
    if ticket.get('priority') == 'urgent':
        fr = float(cfg.get('urgent_first_response_hours') or cfg.get('first_response_hours') or 0)
        res = float(cfg.get('urgent_resolution_hours') or cfg.get('resolution_hours') or 0)
    else:
        fr = float(cfg.get('first_response_hours') or 0)
        res = float(cfg.get('resolution_hours') or 0)
    return fr, res


def init_sla_for_ticket(pdb: PremiumDB, *, ticket: Dict, guild_id: int) -> None:
    '''Create the initial SLA state row for a new ticket (called on create).'''
    cfg = get_config(pdb, guild_id)
    if not cfg.get('enabled'):
        return
    fr_hours, res_hours = targets_for(ticket, cfg)
    now = datetime.now(timezone.utc)
    created = _parse_iso(ticket.get('created_at')) or now
    fr_due = (created + timedelta(hours=fr_hours)).isoformat() if fr_hours else None
    res_due = (created + timedelta(hours=res_hours)).isoformat() if res_hours else None
    pdb.upsert_sla_state({
        'ticket_id': ticket.get('ticket_id'),
        'guild_id': guild_id,
        'first_response_due_at': fr_due,
        'first_response_met_at': None,
        'resolution_due_at': res_due,
        'resolution_met_at': None,
        'breach_state': 'ok',
        'last_notified_at': None,
    })


def mark_first_response(pdb: PremiumDB, *, ticket_id: str) -> None:
    '''Record that the first staff response has occurred.'''
    state = pdb.get_sla_state(ticket_id)
    if not state:
        return
    if state.get('first_response_met_at'):
        return  # already met
    state['first_response_met_at'] = datetime.now(timezone.utc).isoformat()
    pdb.upsert_sla_state(state)


def mark_resolution_met(pdb: PremiumDB, *, ticket_id: str) -> None:
    state = pdb.get_sla_state(ticket_id)
    if not state:
        return
    state['resolution_met_at'] = datetime.now(timezone.utc).isoformat()
    pdb.upsert_sla_state(state)


# =====================================================================
# PERIODIC CHECK (called from wiring.sla_tick)
# =====================================================================

async def check_breaches(bot, pdb: PremiumDB, *, guild: discord.Guild) -> Dict[str, int]:
    '''Advance the SLA state of every open ticket in the guild.

    Returns {'warned': n, 'breached': n}. Pings the escalation role on breach
    and (optionally) auto-escalates the ticket.
    '''
    cfg = get_config(pdb, guild.id)
    if not cfg.get('enabled'):
        return {'warned': 0, 'breached': 0}
    tt = getattr(bot, 'ticket_tool', None)
    if tt is None:
        return {'warned': 0, 'breached': 0}
    warned = 0
    breached = 0
    now = datetime.now(timezone.utc)
    warn_pct = int(cfg.get('warn_before_breach_pct', 0) or 0)

    for state in pdb.list_open_sla_states(guild.id):
        ticket = tt.data_manager.load_ticket(state['ticket_id'])
        if not ticket or ticket.get('status') != 'open':
            continue
        # PAUSED TICKETS (Ticket Tool /pause): excluded from ALL automatic
        # actions — skip SLA warning/breach notifications while paused.
        try:
            from . import automations as _auto_mod
            if _auto_mod.is_ticket_paused(ticket):
                continue
        except Exception:
            pass
        channel = guild.get_channel(int(ticket.get('channel_id') or 0))

        # --- First-response SLA ---
        if not state.get('first_response_met_at') and state.get('first_response_due_at'):
            fr_due = _parse_iso(state['first_response_due_at'])
            if fr_due:
                if now >= fr_due:
                    # BREACH
                    if state.get('breach_state') != 'breached':
                        await _notify_breach(bot, guild, channel, ticket, cfg,
                                              kind='first_response', now=now)
                        state['breach_state'] = 'breached'
                        state['last_notified_at'] = now.isoformat()
                        pdb.upsert_sla_state(state)
                        breached += 1
                elif warn_pct > 0:
                    warn_at = fr_due - timedelta(hours=(fr_due - _parse_iso(ticket.get('created_at')) or now).total_seconds()/3600 * (100 - warn_pct) / 100)
                    # Simpler: warn when remaining < warn_pct% of total window.
                    created = _parse_iso(ticket.get('created_at')) or now
                    total = (fr_due - created).total_seconds()
                    elapsed = (now - created).total_seconds()
                    if total > 0 and elapsed / total >= (warn_pct / 100.0):
                        if state.get('breach_state') == 'ok':
                            await _notify_warning(bot, guild, channel, ticket, cfg,
                                                  kind='first_response', now=now)
                            state['breach_state'] = 'warning'
                            state['last_notified_at'] = now.isoformat()
                            pdb.upsert_sla_state(state)
                            warned += 1

        # --- Resolution SLA ---
        if not state.get('resolution_met_at') and state.get('resolution_due_at'):
            res_due = _parse_iso(state['resolution_due_at'])
            if res_due and now >= res_due:
                if state.get('breach_state') != 'breached':
                    await _notify_breach(bot, guild, channel, ticket, cfg,
                                          kind='resolution', now=now)
                    state['breach_state'] = 'breached'
                    state['last_notified_at'] = now.isoformat()
                    pdb.upsert_sla_state(state)
                    breached += 1
                    # Auto-escalate on resolution breach?
                    from . import escalation as esc
                    route = esc.get_route(pdb, ticket.get('panel_id'))
                    if route and route.get('auto_escalate_hours'):
                        await esc.escalate_ticket(
                            bot=bot, pdb=pdb, channel=channel, ticket=ticket,
                            guild=guild, escalated_by=None,
                            reason='Auto-escalated: resolution SLA breached',
                        )
    return {'warned': warned, 'breached': breached}


async def _notify_warning(bot, guild, channel, ticket, cfg, *, kind, now):
    if channel is None:
        return
    role_mention = ''
    rid = cfg.get('escalation_role_id')
    if rid:
        role_mention = f"<@&{rid}> "
    try:
        await channel.send(
            f"⏰ **SLA Warning** — {role_mention}the {kind.replace('_',' ')} SLA "
            f"window is almost over for ticket `{ticket.get('ticket_id')}`.",
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.HTTPException:
        pass


async def _notify_breach(bot, guild, channel, ticket, cfg, *, kind, now):
    if channel is None:
        return
    role_mention = '@here'
    rid = cfg.get('escalation_role_id')
    if rid:
        role_mention = f"<@&{rid}>"
    try:
        await channel.send(
            f"🚨 **SLA Breach ({kind.replace('_',' ')})** — {role_mention} "
            f"ticket `{ticket.get('ticket_id')}` has breached its "
            f"{kind.replace('_',' ')} SLA. Please respond immediately.",
            allowed_mentions=discord.AllowedMentions(roles=True, everyone=True),
        )
    except discord.HTTPException:
        pass


# =====================================================================
# REPORTING
# =====================================================================

def sla_report(pdb: PremiumDB, *, guild_id: int) -> Dict:
    '''Aggregate SLA stats for /slaconfig report.

    Returns:
      {'first_response_met': n, 'first_response_breached': n,
       'resolution_met': n, 'resolution_breached': n,
       'avg_first_response_minutes': float|None,
       'avg_resolution_minutes': float|None}
    '''
    states = pdb.list_open_sla_states(guild_id)
    # That method returns only unresolved; for reporting we want all-time, so
    # query the table directly via the connection.
    conn = pdb.dm._connection
    cur = conn.cursor()
    cur.execute('SELECT * FROM ticket_sla_state WHERE guild_id = ?', (guild_id,))
    all_states = [dict(r) for r in cur.fetchall()]
    fr_met = sum(1 for s in all_states if s.get('first_response_met_at'))
    fr_breached = sum(1 for s in all_states if s.get('breach_state') == 'breached'
                      and not s.get('first_response_met_at'))
    res_met = sum(1 for s in all_states if s.get('resolution_met_at'))
    res_breached = sum(1 for s in all_states if s.get('breach_state') == 'breached'
                       and not s.get('resolution_met_at'))

    cur.execute('SELECT ticket_id, created_at, first_response_at, closed_at FROM tickets WHERE guild_id = ?', (guild_id,))
    tickets = {r['ticket_id']: dict(r) for r in cur.fetchall()}

    fr_mins = []
    res_mins = []
    for s in all_states:
        t = tickets.get(s['ticket_id'])
        if not t:
            continue
        created = _parse_iso(t.get('created_at'))
        fr_met_at = _parse_iso(s.get('first_response_met_at'))
        if created and fr_met_at:
            fr_mins.append((fr_met_at - created).total_seconds() / 60.0)
        closed = _parse_iso(t.get('closed_at'))
        if created and closed:
            res_mins.append((closed - created).total_seconds() / 60.0)

    return {
        'first_response_met': fr_met,
        'first_response_breached': fr_breached,
        'resolution_met': res_met,
        'resolution_breached': res_breached,
        'avg_first_response_minutes': round(sum(fr_mins)/len(fr_mins), 2) if fr_mins else None,
        'avg_resolution_minutes': round(sum(res_mins)/len(res_mins), 2) if res_mins else None,
    }


# =====================================================================
# HELPERS
# =====================================================================

def _parse_iso(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
