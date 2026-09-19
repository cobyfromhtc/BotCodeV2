# -*- coding: utf-8 -*-
'''
TicketTool.wiring — lifecycle hooks called from Bot.py.

This module is the ONLY place Bot.py needs to know about. Each hook is a
thin orchestrator that pulls the relevant premium submodules together.
Bot.py calls these at the right lifecycle points:

    setup_hook():
        TicketTool.wiring.on_setup_hook(data_manager, bot)

    on_ready() (after ticket_tool is initialized):
        TicketTool.wiring.on_ready_hook(data_manager, bot, ticket_tool)

    TicketToolSystem.create_ticket() — after the channel is created + row
    is status='open':
        await TicketTool.wiring.on_ticket_create(
            bot=bot, ticket_tool=ticket_tool, channel=channel,
            ticket=ticket, panel=panel, creator=creator_member)

    TicketToolSystem.close_ticket() — after transcript generation, BEFORE
    channel.delete():
        await TicketTool.wiring.on_ticket_close(
            bot=bot, ticket_tool=ticket_tool, channel=channel,
            ticket=ticket, closed_by=closed_by_member,
            transcript=transcript_payload)

    TicketToolSystem.claim_ticket() — after atomic claim succeeds:
        await TicketTool.wiring.on_ticket_claim(
            bot=bot, ticket_tool=ticket_tool, channel=channel,
            ticket=ticket, panel=panel, claimer=claimer_member)

    TicketToolSystem.unclaim_ticket() — after release:
        await TicketTool.wiring.on_ticket_unclaim(
            bot=bot, ticket_tool=ticket_tool, channel=channel,
            ticket=ticket, panel=panel, unclaimer=unclaimer_member)

    on_message() — in the existing ticket-tracking block, after
    update_ticket_first_response:
        await TicketTool.wiring.on_ticket_message(
            bot=bot, ticket_tool=ticket_tool, message=message,
            ticket=ticket, is_staff=is_staff_msg)

    check_sla_task() — replace/extend the existing loop body:
        await TicketTool.wiring.on_sla_tick(bot, data_manager)

Every hook is FAIL-SAFE: it logs and swallows exceptions so a premium bug can
never break the core ticket flow. This is critical because premium is layered
ON TOP of working code — it must be possible to disable premium entirely by
removing the wiring calls, with zero behavioral change to the base system.
'''

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import discord

from .db import PremiumDB, install_premium_schema
from . import naming as naming_mod
from . import claiming as claim_mod
from . import role_automation as role_mod
from . import automations as auto_mod
from . import transcripts as tr_mod
from . import sla as sla_mod
# Tier 2 modules
from . import kb as kb_mod
from . import thread_tickets as tt_mod
from . import staff_threads as st_mod
from . import channel_recycle as cr_mod
from . import i18n as i18n_mod
from . import branded_replies as br_mod
from . import flows as flow_mod
from . import custom_commands as cc_mod
# Tier 3 modules
from . import moderator_messages as mm_mod
from . import flow_reviews as fr_mod


# =====================================================================
# SETUP / READY
# =====================================================================

def on_setup_hook(data_manager, bot) -> None:
    '''Install the premium schema + build the PremiumDB accessor.

    Called from TicketBot.setup_hook() AFTER DataManager._create_tables()
    has run (so base tables exist). Idempotent.
    '''
    try:
        if data_manager._connection is None:
            logging.warning("[tickettool.wiring] no DB connection; skipping schema install")
            return
        install_premium_schema(data_manager._connection)
        # Stash the PremiumDB accessor on the bot so commands can find it.
        bot.premium_db = PremiumDB(data_manager)
        logging.info("[tickettool.wiring] schema installed + premium_db attached")
    except Exception as exc:
        logging.exception(f"[tickettool.wiring] on_setup_hook failed: {exc}")


def on_ready_hook(data_manager, bot, ticket_tool, *, start_loop: bool = True) -> None:
    '''Register automations' global refs + start the timer-loop task.

    start_loop=False (used when several domain bots share the database):
    the minute loop (automation timers + SLA + review deadlines) must run on
    exactly ONE process or actions would double-fire; refs and review views
    are still registered so this instance's commands work.'''
    try:
        auto_mod.set_global_refs(bot, ticket_tool)
        if start_loop and not getattr(bot, '_premium_timer_task_started', False):
            bot._premium_timer_task_started = True
            # Start the delayed-automation + SLA timer loop. We piggyback on
            # the existing @tasks.loop pattern by creating a fresh loop here
            # (so we don't need to touch Bot.py's task list).
            @asyncio_at_interval(bot, minutes=1)
            async def _premium_minute_loop():
                await _minute_loop(bot, data_manager)
            bot._premium_minute_loop = _premium_minute_loop
            if not _premium_minute_loop.is_running():
                _premium_minute_loop.start()
        # Re-arm any persisted delayed timers that fired while we were down.
        if start_loop:
            try:
                bot.loop.create_task(auto_mod.process_due_timers(bot, bot.premium_db))
            except Exception as exc:
                logging.warning(f"[tickettool.wiring] timer re-arm failed: {exc}")
        # Re-register persistent review views (application Approve/Reject
        # buttons) so they survive restarts.
        try:
            fr_mod.register_persistent_views(bot)
        except Exception as exc:
            logging.warning(f"[tickettool.wiring] register persistent review views failed: {exc}")
        logging.info("[tickettool.wiring] on_ready hook complete"
                     + ("" if start_loop else " (timer loop owned by another bot)"))
    except Exception as exc:
        logging.exception(f"[tickettool.wiring] on_ready_hook failed: {exc}")


def asyncio_at_interval(bot, *, minutes: int):
    '''Build a discord.ext.tasks.loop without importing tasks at module top.

    We import here (inside the function) so this module stays importable even
    if discord.ext.tasks is temporarily unavailable.
    '''
    from discord.ext import tasks
    return tasks.loop(minutes=minutes)


async def _minute_loop(bot, data_manager) -> None:
    '''Runs every minute: fire due automation timers, SLA checks, review deadlines.'''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None:
        return
    try:
        await auto_mod.process_due_timers(bot, pdb)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] automation timer loop error: {exc}")
    # SLA check every 5 minutes (every 5th tick).
    try:
        if not hasattr(bot, '_premium_sla_tick_counter'):
            bot._premium_sla_tick_counter = 0
        bot._premium_sla_tick_counter += 1
        if bot._premium_sla_tick_counter % 5 == 0:
            for guild in bot.guilds:
                await sla_mod.check_breaches(bot, pdb, guild=guild)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] SLA loop error: {exc}")
    # Application-review auto approve/reject deadlines — every tick.
    try:
        await fr_mod.process_due_reviews(bot, pdb)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] review deadline loop error: {exc}")


async def on_sla_tick(bot, data_manager) -> None:
    '''Replacement for the existing check_sla_task body (optional).

    If Bot.py calls this, premium takes over SLA checking. If not, the
    _minute_loop above also does SLA checks. Either way works.
    '''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None:
        return
    for guild in bot.guilds:
        await sla_mod.check_breaches(bot, pdb, guild=guild)


# =====================================================================
# TICKET LIFECYCLE HOOKS
# =====================================================================

async def on_ticket_create(*, bot, ticket_tool, channel: discord.TextChannel,
                            ticket: Dict, panel: Optional[Dict],
                            creator: discord.Member) -> None:
    '''Fired right after a ticket row is 'open' + channel exists.

    - Reserves the guild ticket counter (so {ticket.count} + padding work);
      reuses Bot.py's pre-reserved number when present (no double-reserve).
    - Applies role-automation 'open' rules to the creator.
    - Arms any 'delayed' / 'no_response' automations.
    - Initializes the SLA state row.
    - Fires the 'created' trigger to the automation engine.
    - [Tier 2] Creates a staff discussion thread (if panel.create_staff_thread).
    - [Tier 2] Starts a support flow (if panel.flow_id set).
    - [Tier 2] Suggests KB articles matching the ticket subject.
    - [Tier 3] Fires the 'create' moderator message.
    '''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None or ticket is None:
        return
    guild_id = ticket.get('guild_id') or channel.guild.id
    try:
        # Bot.py pre-reserves the ticket number for the channel name and
        # stashes it on the ticket dict as '_count' — only reserve a new one
        # when it is missing (never double-reserve).
        count = ticket.get('_count')
        if not count:
            count = naming_mod.reserve_number(pdb, guild_id)
            ticket['_count'] = count
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] reserve_number failed: {exc}")
    # Role automation: open
    try:
        await role_mod.apply_open(pdb, creator, panel)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] role_auto open failed: {exc}")
    # SLA init
    try:
        sla_mod.init_sla_for_ticket(pdb, ticket=ticket, guild_id=guild_id)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] sla init failed: {exc}")
    # Arm delayed / no_response automations
    try:
        auto_mod.schedule_delayed_for_new_ticket(bot, pdb, panel=panel or {}, ticket=ticket)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] schedule_delayed failed: {exc}")
    # Fire 'created' automations
    try:
        event = auto_mod.AutomationEvent(
            trigger='created', ticket=ticket, panel=panel or {},
            guild=channel.guild, bot=bot, ticket_count=ticket.get('_count'),
        )
        await auto_mod.fire_event(bot, pdb, event)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] fire 'created' failed: {exc}")

    # === Tier 2 hooks ===
    # Staff discussion thread
    if panel and st_mod.should_create_staff_thread(panel):
        try:
            support_role_id = panel.get('support_role_id')
            staff_thread = await st_mod.create_for_ticket(
                ticket_channel=channel, ticket=ticket, panel=panel,
                support_role_id=support_role_id,
            )
            if staff_thread:
                ticket['staff_thread_id'] = staff_thread.id
                ticket_tool.data_manager.save_ticket(ticket)
        except Exception as exc:
            logging.warning(f"[tickettool.wiring] staff_thread create failed: {exc}")
    # Support flow
    if panel and panel.get('flow_id'):
        try:
            await flow_mod.start_flow(bot=bot, pdb=pdb, channel=channel,
                                       ticket=ticket, panel=panel)
        except Exception as exc:
            logging.warning(f"[tickettool.wiring] flow start failed: {exc}")
    # KB suggestions
    try:
        suggestions = kb_mod.suggest_for_ticket(pdb, guild_id=guild_id,
                                                 subject=ticket.get('subject'))
        if suggestions:
            import discord as _discord
            embed = _discord.Embed(
                title=i18n_mod.t(pdb, guild_id, 'kb.suggestion_intro'),
                color=_discord.Color(0x5865F2),
            )
            lines = [f"• `{s.get('article_id')}` — {s.get('title')}" for s in suggestions[:3]]
            embed.description = "\n".join(lines)
            embed.set_footer(text="Use /kb view <article_id> to read.")
            await channel.send(embed=embed)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] kb suggestion failed: {exc}")

    # === Tier 3: moderator create message ===
    if panel and channel:
        try:
            await mm_mod.send_moderator_message(
                bot=bot, pdb=pdb, channel=channel,
                panel_id=panel.get('panel_id', ''), event_type='create',
                ticket=ticket, panel=panel, actor=creator,
            )
        except Exception as exc:
            logging.debug(f"[tickettool.wiring] moderator create message failed: {exc}")


async def on_ticket_close(*, bot, ticket_tool, channel: discord.TextChannel,
                          ticket: Dict, closed_by: discord.Member,
                          transcript: Optional[Dict] = None,
                          panel: Optional[Dict] = None) -> None:
    '''Fired after transcript generation, before channel.delete().

    - Cancels pending delayed automations.
    - Marks the SLA resolution met.
    - Applies role-automation 'close' rules to the creator.
    - Applies naming 'closed' template (optional rename before delete).
    - Posts the transcript via the advanced transcript config (custom msg, DM, archive).
    - Fires the 'closed' trigger.
    '''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None or ticket is None:
        return
    guild = channel.guild if channel else None
    try:
        auto_mod.cancel_timers_for_ticket(pdb, ticket.get('ticket_id'))
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] cancel timers failed: {exc}")
    try:
        sla_mod.mark_resolution_met(pdb, ticket_id=ticket.get('ticket_id'))
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] mark resolution failed: {exc}")
    # Role automation: close (applied to creator)
    creator = None
    if guild:
        creator = guild.get_member(int(ticket.get('creator_id') or 0))
    if creator:
        try:
            await role_mod.apply_close(pdb, creator, panel)
        except Exception as exc:
            logging.warning(f"[tickettool.wiring] role_auto close failed: {exc}")
    # Naming: closed template
    if panel and channel:
        try:
            closed_name = naming_mod.compute_closed_name(
                pdb, panel, guild={'id': guild.id, 'name': guild.name},
                ticket=ticket, ticket_count=ticket.get('_count'),
                closer={'id': closed_by.id, 'name': closed_by.display_name} if closed_by else None,
            )
            if closed_name:
                await channel.edit(name=closed_name)
        except Exception as exc:
            logging.warning(f"[tickettool.wiring] closed rename failed: {exc}")
    # Advanced transcript posting (custom msg / DM / archive)
    if transcript and guild:
        try:
            claimer = None
            if ticket.get('claimed_by'):
                claimer = guild.get_member(int(ticket['claimed_by']))
            await tr_mod.post_transcript(
                bot=bot, pdb=pdb, guild=guild, ticket=ticket,
                transcript_payload=transcript, closed_by=closed_by,
                panel=panel, creator=creator, claimer=claimer,
            )
        except Exception as exc:
            logging.warning(f"[tickettool.wiring] advanced transcript post failed: {exc}")
    # Fire 'closed' automations
    try:
        event = auto_mod.AutomationEvent(
            trigger='closed', ticket=ticket, panel=panel or {},
            guild=guild, bot=bot,
            actor={'id': closed_by.id, 'name': closed_by.display_name} if closed_by else {},
            ticket_count=ticket.get('_count'),
        )
        await auto_mod.fire_event(bot, pdb, event)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] fire 'closed' failed: {exc}")

    # === Tier 3: moderator close message ===
    if panel and channel:
        try:
            await mm_mod.send_moderator_message(
                bot=bot, pdb=pdb, channel=channel,
                panel_id=panel.get('panel_id', ''), event_type='close',
                ticket=ticket, panel=panel, actor=closed_by,
            )
        except Exception as exc:
            logging.debug(f"[tickettool.wiring] moderator close message failed: {exc}")

    # === Tier 2 hooks ===
    # Archive the staff discussion thread (if one exists).
    if ticket.get('staff_thread_id') and channel:
        try:
            staff_thread = channel.guild.get_thread(int(ticket['staff_thread_id']))
            if staff_thread is None:
                # Maybe archived; try fetching.
                try:
                    staff_thread = await channel.guild.fetch_channel(int(ticket['staff_thread_id']))
                except Exception:
                    staff_thread = None
            if isinstance(staff_thread, discord.Thread):
                await st_mod.archive_for_ticket(staff_thread, reason=f"Ticket {ticket.get('ticket_id')} closed")
        except Exception as exc:
            logging.warning(f"[tickettool.wiring] staff_thread archive failed: {exc}")
    # Channel recycling — if enabled, recycle the channel instead of letting
    # Bot.py delete it. The caller checks the return: if recycled, skip delete.
    # (We stash the recycle decision on the ticket dict so the close flow can
    # check it.)


async def on_ticket_close_recycle_check(*, bot, pdb: PremiumDB,
                                          channel: discord.TextChannel,
                                          panel: Optional[Dict]) -> bool:
    '''Called by the close flow BEFORE channel.delete(). Returns True if the
    channel was recycled (so the caller should NOT delete it).
    '''
    if not panel or not cr_mod.is_recycle_enabled(panel):
        return False
    try:
        return await cr_mod.release_channel(channel=channel, panel=panel, pdb=pdb)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] recycle release failed: {exc}")
        return False


async def on_ticket_claim(*, bot, ticket_tool, channel: discord.TextChannel,
                          ticket: Dict, panel: Optional[Dict],
                          claimer: discord.Member) -> None:
    '''Fired after atomic claim succeeds.

    - Applies advanced-claiming side effects (rename, move, hide, perms, msg).
    - Applies role-automation 'claim' rules to the claimer.
    - Fires the 'claim' trigger.
    '''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None:
        return
    try:
        await claim_mod.apply_claim_side_effects(
            bot=bot, pdb=pdb, channel=channel, ticket=ticket,
            panel=panel, claimer=claimer, ticket_count=ticket.get('_count'),
        )
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] claim side effects failed: {exc}")
    try:
        await role_mod.apply_claim(pdb, claimer, panel)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] role_auto claim failed: {exc}")
    try:
        event = auto_mod.AutomationEvent(
            trigger='claim', ticket=ticket, panel=panel or {},
            guild=channel.guild, bot=bot,
            actor={'id': claimer.id, 'name': claimer.display_name},
            ticket_count=ticket.get('_count'),
        )
        await auto_mod.fire_event(bot, pdb, event)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] fire 'claim' failed: {exc}")

    # === Tier 3: moderator claim message + claim_count increment ===
    if panel and channel:
        try:
            # Increment claim_count on the ticket.
            ticket['claim_count'] = int(ticket.get('claim_count') or 0) + 1
            ticket_tool.data_manager.save_ticket(ticket)
            await mm_mod.send_moderator_message(
                bot=bot, pdb=pdb, channel=channel,
                panel_id=panel.get('panel_id', ''), event_type='claim',
                ticket=ticket, panel=panel, actor=claimer,
            )
        except Exception as exc:
            logging.debug(f"[tickettool.wiring] moderator claim message failed: {exc}")


async def on_ticket_unclaim(*, bot, ticket_tool, channel: discord.TextChannel,
                             ticket: Dict, panel: Optional[Dict],
                             unclaimer: discord.Member) -> None:
    '''Fired after a claim is released.'''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None:
        return
    try:
        await claim_mod.apply_unclaim_side_effects(
            bot=bot, pdb=pdb, channel=channel, ticket=ticket,
            panel=panel, unclaimer=unclaimer, ticket_count=ticket.get('_count'),
        )
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] unclaim side effects failed: {exc}")
    try:
        await role_mod.apply_unclaim(pdb, unclaimer, panel)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] role_auto unclaim failed: {exc}")
    try:
        event = auto_mod.AutomationEvent(
            trigger='unclaim', ticket=ticket, panel=panel or {},
            guild=channel.guild, bot=bot,
            actor={'id': unclaimer.id, 'name': unclaimer.display_name},
            ticket_count=ticket.get('_count'),
        )
        await auto_mod.fire_event(bot, pdb, event)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] fire 'unclaim' failed: {exc}")

    # === Tier 3: moderator unclaim message ===
    if panel and channel:
        try:
            await mm_mod.send_moderator_message(
                bot=bot, pdb=pdb, channel=channel,
                panel_id=panel.get('panel_id', ''), event_type='unclaim',
                ticket=ticket, panel=panel, actor=unclaimer,
            )
        except Exception as exc:
            logging.debug(f"[tickettool.wiring] moderator unclaim message failed: {exc}")


async def on_ticket_message(*, bot, ticket_tool, message: discord.Message,
                             ticket: Dict, is_staff: bool) -> None:
    '''Fired from on_message when the message is in an open ticket channel.

    - Records first staff response (SLA + staff_responded_at).
    - Cancels any 'no_response' automation timer for this ticket.
    - [Tier 2] Intercepts staff messages for branded replies (if enabled).
    '''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None or ticket is None:
        return

    # === Tier 2: branded replies interception ===
    # If branded replies are enabled for this panel AND the author is staff,
    # delete the original message and re-post it via the panel's webhook.
    if is_staff and ticket.get('panel_id') and not message.content.startswith('!'):
        try:
            panel = ticket_tool.data_manager.load_ticket_panel(ticket['panel_id'])
            if panel and br_mod.is_enabled(pdb, panel.get('panel_id')):
                content = message.content
                if content:
                    ok = await br_mod.send_branded_reply(
                        channel=message.channel, staff_member=message.author,
                        content=content, pdb=pdb, panel=panel,
                    )
                    if ok:
                        try:
                            await message.delete()
                        except (discord.HTTPException, discord.Forbidden):
                            pass
                        return  # don't do the normal staff-response processing
        except Exception as exc:
            logging.debug(f"[tickettool.wiring] branded reply failed: {exc}")

    if not is_staff:
        return
    try:
        sla_mod.mark_first_response(pdb, ticket_id=ticket.get('ticket_id'))
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] mark first response failed: {exc}")
    # Record staff_responded_at on the ticket row (for no_response automations)
    try:
        if not ticket.get('staff_responded_at'):
            ticket['staff_responded_at'] = datetime.now(timezone.utc).isoformat()
            ticket_tool.data_manager.save_ticket(ticket)
            # Cancel no_response timers (the response happened).
            pdb.cancel_timers_for_ticket(ticket.get('ticket_id'))
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] staff_responded_at update failed: {exc}")


async def on_ticket_reopen(*, bot, ticket_tool, ticket: Dict,
                            panel: Optional[Dict], guild: discord.Guild) -> None:
    '''Fired from the /reopen command after the channel is recreated.'''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None or ticket is None:
        return
    try:
        sla_mod.init_sla_for_ticket(pdb, ticket=ticket, guild_id=guild.id)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] sla reinit on reopen failed: {exc}")
    try:
        auto_mod.schedule_delayed_for_new_ticket(bot, pdb, panel=panel or {}, ticket=ticket)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] rearm delayed on reopen failed: {exc}")
    try:
        event = auto_mod.AutomationEvent(
            trigger='reopened', ticket=ticket, panel=panel or {},
            guild=guild, bot=bot, ticket_count=ticket.get('_count'),
        )
        await auto_mod.fire_event(bot, pdb, event)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] fire 'reopened' failed: {exc}")

    # === Tier 3: moderator reopen message ===
    if panel and guild:
        try:
            ticket_tool = getattr(bot, 'ticket_tool', None)
            ch = guild.get_channel(int(ticket.get('channel_id') or 0))
            if ch and ticket_tool:
                await mm_mod.send_moderator_message(
                    bot=bot, pdb=pdb, channel=ch,
                    panel_id=panel.get('panel_id', ''), event_type='reopen',
                    ticket=ticket, panel=panel,
                )
        except Exception as exc:
            logging.debug(f"[tickettool.wiring] moderator reopen message failed: {exc}")


async def on_owner_left(*, bot, ticket_tool, ticket: Dict,
                         panel: Optional[Dict], guild: discord.Guild) -> None:
    '''Fired from on_member_remove when the leaving member owns an open ticket.'''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None or ticket is None:
        return
    try:
        event = auto_mod.AutomationEvent(
            trigger='owner_left', ticket=ticket, panel=panel or {},
            guild=guild, bot=bot, ticket_count=ticket.get('_count'),
        )
        await auto_mod.fire_event(bot, pdb, event)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] fire 'owner_left' failed: {exc}")


# =====================================================================
# TIER 2: CUSTOM COMMAND PREFIX DISPATCH
# =====================================================================

async def on_prefix_command(*, bot, message: discord.Message,
                              command_name: str) -> bool:
    '''Try to dispatch a !-prefixed message to a custom command.

    Called from Bot.py's on_message for any !-prefixed message that isn't a
    built-in command. Returns True if a custom command ran (so Bot.py can
    skip its own "unknown command" handling).
    '''
    pdb = getattr(bot, 'premium_db', None)
    if pdb is None:
        return False
    if not message.guild:
        return False
    try:
        return await cc_mod.try_invoke(bot=bot, pdb=pdb, message=message,
                                         command_name=command_name)
    except Exception as exc:
        logging.warning(f"[tickettool.wiring] custom command dispatch failed: {exc}")
        return False


# =====================================================================
# TIER 2: THREAD TICKET CREATION HELPER
# =====================================================================

async def on_ticket_create_thread_check(*, bot, pdb: PremiumDB,
                                          guild: discord.Guild,
                                          panel: Dict,
                                          ticket_id: str,
                                          channel_name: str,
                                          creator: discord.Member,
                                          support_role_id: Optional[int]) -> Optional[discord.Thread]:
    '''If the panel is configured for thread tickets, create a thread instead
    of a channel. Returns the thread, or None (caller creates a channel).
    '''
    if not tt_mod.is_thread_panel(panel):
        return None
    allow_invite = bool(int(panel.get('allow_user_invite_in_thread', 0) or 0))
    thread, err = await tt_mod.create_thread_ticket(
        guild=guild, user=creator, panel=panel, ticket_id=ticket_id,
        channel_name=channel_name, support_role_id=support_role_id,
        allow_user_invite=allow_invite,
    )
    if err:
        logging.warning(f"[tickettool.wiring] thread ticket create failed: {err}")
    return thread
