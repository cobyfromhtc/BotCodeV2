# -*- coding: utf-8 -*-
"""Ticket engine — TicketToolSystem, panel processing, reopen/close,
reaction panels, SLA + auto-close tasks."""

# stdlib + discord.py
from packages import tickettool as TicketTool
import asyncio
import discord
import json
import logging
import re
from datetime import datetime, timezone
from discord.ext import commands, tasks
from typing import Dict, List, Optional, Tuple

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from core.data_manager import DataManager
from core.helpers import PREMIUM_AVAILABLE, _esc, _safe_url
from core.ows import ows_get



TICKET_CATEGORIES: Dict[str, str] = {
    "general": "General Support",
    "report": "Player Report",
    "appeal": "Ban Appeal",
    "verification": "Verification Help",
    "other": "Other",
    "alliance": "Request an Alliance",
    "opp": "Request us to Add opp gangs / players"
}


# --- TICKET TOOL SYSTEM (Full Ticket Tool Clone - All Features Free) ---

class TicketToolSystem:
    """Main ticket tool system manager - handles all ticket operations."""
    
    def __init__(self, data_mgr: DataManager, bot_instance: commands.Bot):
        self.data_manager = data_mgr
        self.bot = bot_instance
        # Per-(guild, user) locks to prevent the ticket-limit race condition:
        # two near-simultaneous "Create Ticket" clicks could both pass the
        # open-count check before either row is written. Serializing per user
        # closes that window without blocking unrelated users.
        self._creation_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        self._creation_locks_guard = asyncio.Lock()
    
    async def _get_creation_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        async with self._creation_locks_guard:
            key = (guild_id, user_id)
            lock = self._creation_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._creation_locks[key] = lock
            return lock
    
    async def create_ticket(
        self, 
        guild: discord.Guild, 
        user: discord.Member,
        panel: Dict,
        subject: str = None,
        answers: Dict = None
    ) -> Tuple[Optional[discord.TextChannel], str]:
        """Create a new ticket channel.

        Transactional ordering (fixes orphan-channel / orphan-row bugs):
          1. Insert the ticket row with status='pending' FIRST (no channel yet).
          2. Create the Discord channel.
             - On failure: delete the pending row so DB and Discord stay
               consistent.
          3. Update the row with channel_id and status='open'.
          4. Persist any panel answers. If answer persistence fails, the
             ticket itself still exists (logged, not fatal).

        A per-user asyncio.Lock guards the limit check so two concurrent
        create requests can't both pass it.
        """
        import uuid
        ticket_id = str(uuid.uuid4())[:8]
        now_iso = datetime.now(timezone.utc).isoformat()

        # Owner Settings gate: if the Tickets System is disabled, refuse all
        # new ticket creation. Management commands (close/claim/transcript) are
        # intentionally left available so staff can wind down existing tickets,
        # but no NEW tickets can be opened while the system is disabled.
        if not ows_get("enable_tickets"):
            return None, "The ticket system is currently disabled by the server owner. Please try again later."

        # Check if user is blacklisted
        blacklisted, reason = self.data_manager.is_user_blacklisted(guild.id, user.id)
        if blacklisted:
            return None, f"You are blacklisted from creating tickets. Reason: {reason}"
        
        # Serialize ticket creation per user to close the limit-check race window.
        # The lock is held across the ENTIRE create (limit-check → pending insert
        # → Discord channel creation → open status update) so a second concurrent
        # request from the same user cannot slip in between the limit check and
        # the channel creation. The limit check also now counts BOTH 'pending'
        # and 'open' tickets, so the in-flight 'pending' row is visible to it.
        creation_lock = await self._get_creation_lock(guild.id, user.id)
        async with creation_lock:
            settings = self.data_manager.load_ticket_settings(guild.id)
            # TicketTool-style limit bypass: members holding any configured
            # bypass role skip BOTH the guild-wide and per-panel limits.
            bypass = _member_has_limit_bypass(user, panel, settings)
            if not bypass:
                # Guild-wide limit (TicketTool "Global Ticket Limit"). Gated by
                # the OWS enforce_max_tickets toggle; config.limits is the
                # fallback when no DB settings row exists yet.
                if ows_get("enforce_max_tickets"):
                    max_tickets = (
                        settings.get('max_tickets_per_user')
                        if settings and settings.get('max_tickets_per_user')
                        else getattr(config.limits, 'max_tickets_per_user', 3)
                    )
                    active_count = self.data_manager.count_active_tickets_by_creator(user.id, guild.id)
                    if active_count >= max_tickets:
                        return None, f"You already have {active_count} active ticket(s). Close one first."
                    # TicketTool closed-ticket limit (checked at creation to
                    # prevent open/close cycling). 0/None disables.
                    max_closed = settings.get('max_closed_tickets_per_user') if settings else None
                    if max_closed:
                        closed_count = self.data_manager.count_closed_tickets_by_creator(user.id, guild.id)
                        if closed_count >= int(max_closed):
                            return None, (
                                f"You have already closed {closed_count} ticket(s) "
                                f"(limit: {max_closed}). Please contact staff for further help."
                            )
                    # TicketTool "open tickets all users" cap. 0/None disables.
                    max_open_all = settings.get('max_open_tickets_all') if settings else None
                    if max_open_all:
                        open_all = self.data_manager.count_open_tickets_in_guild(guild.id)
                        if open_all >= int(max_open_all):
                            return None, (
                                f"The ticket queue is full ({open_all}/{max_open_all} open). "
                                f"Please try again later."
                            )
                # Per-panel limit (TicketTool per-panel "open tickets per user").
                panel_limit = panel.get('ticket_limit') if panel else None
                if panel_limit:
                    panel_count = self.data_manager.count_active_tickets_by_creator_and_panel(
                        user.id, guild.id, panel.get('panel_id'),
                    )
                    if panel_count >= int(panel_limit):
                        return None, (
                            f"You already have {panel_count} open ticket(s) in this panel. "
                            f"Close one first."
                        )
        
            # Get panel settings
            # Fall back to config defaults so tickets always land in the
            # configured Tickets category even before the owner runs !channelsetup.
            # This fixes the bug where clicking "Create Ticket" created the
            # channel with NO category (because panel/settings had none set).
            category_id = (
                panel.get('category_id')
                or (settings.get('category_id') if settings else None)
                or config.channels.tickets
            )
            support_role_id = (
                panel.get('support_role_id')
                or (settings.get('support_role_id') if settings else None)
                or config.roles.ticket_support
            )
            
            # Channel name: premium naming templates (with guild-wide ticket
            # counter + zero padding) when configured, else the classic
            # ticket-{username}-{ticket_id} convention.
            ticket_number = None
            safe_name = ''.join(c if c.isalnum() or c == '-' else '-' for c in user.display_name.lower())[:40]
            channel_name = f"ticket-{safe_name}-{ticket_id}"[:90]
            if PREMIUM_AVAILABLE:
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    try:
                        ticket_number = TicketTool.naming.reserve_number(pdb, guild.id)
                    except Exception as exc:
                        logging.debug(f"[TicketTool] reserve_number failed: {exc}")
                    try:
                        channel_name, computed_subject = TicketTool.naming.compute_open_name(
                            pdb, panel,
                            guild={'id': guild.id, 'name': guild.name},
                            ticket_id=ticket_id,
                            creator={'id': user.id, 'name': user.display_name},
                            ticket_count=ticket_number,
                            subject=subject,
                        )
                        if computed_subject and not subject:
                            subject = computed_subject
                    except Exception as exc:
                        logging.debug(f"[TicketTool] compute_open_name failed: {exc}")
            
            # Setup permissions
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
            }
            
            # Add support role
            if support_role_id:
                support_role = guild.get_role(support_role_id)
                if support_role:
                    overwrites[support_role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True, attach_files=True
                    )
            
            # Get category
            category = guild.get_channel(category_id) if category_id else None
            
            # ---- STEP 1: persist a 'pending' ticket row BEFORE creating the channel ----
            ticket_data = {
                'ticket_id': ticket_id,
                'guild_id': guild.id,
                'channel_id': None,           # filled in after channel creation
                'panel_id': panel.get('panel_id'),
                'creator_id': user.id,
                'category': panel.get('name', 'General'),
                'subject': subject,
                'status': 'pending',
                'created_at': now_iso,
                # Internal Ticket Category (folder) inherited from the panel.
                # NULL/absent on old panels = "Uncategorized" (backwards compat).
                'ticket_category_id': panel.get('ticket_category_id') if panel else None,
            }
            await self.data_manager.async_save_ticket(ticket_data)

            # ---- STEP 2: acquire a ticket channel/thread (INSIDE the user lock) ----
            # Holding the lock here is the key race fix: the 'pending' row is
            # already persisted, so a concurrent second request from the same
            # user would see it via count_active_tickets_by_creator and be
            # rejected at the limit check above before reaching this point.
            #
            # Acquisition order (TicketTool behavior):
            #   a) thread-style ticket, when the panel is configured for threads
            #   b) a recycled channel from the panel's recycle pool
            #   c) a freshly created text channel
            channel = None
            if PREMIUM_AVAILABLE:
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    # (a) Thread-style tickets (Ticket Tool premium thread style).
                    try:
                        thread = await TicketTool.wiring.on_ticket_create_thread_check(
                            bot=self.bot, pdb=pdb, guild=guild, panel=panel,
                            ticket_id=ticket_id, channel_name=channel_name,
                            creator=user, support_role_id=support_role_id,
                        )
                        if thread is not None:
                            channel = thread
                            ticket_data['is_thread'] = 1
                            ticket_data['thread_id'] = thread.id
                    except Exception as exc:
                        logging.warning(f"[TicketTool] thread ticket check failed: {exc}")
                    # (b) Channel recycling (dodge the 500-channel guild cap).
                    if channel is None:
                        try:
                            recycled_channel = await TicketTool.channel_recycle.acquire_channel(
                                guild=guild, panel=panel, new_name=channel_name,
                                creator=user, support_role_id=support_role_id,
                                category_id=category_id, pdb=pdb,
                            )
                            if recycled_channel is not None:
                                channel = recycled_channel
                                ticket_data['recycled_from_channel_id'] = recycled_channel.id
                        except Exception as exc:
                            logging.warning(f"[TicketTool] channel recycle acquire failed: {exc}")

            # (c) Fresh text channel.
            if channel is None:
                try:
                    channel = await guild.create_text_channel(
                        channel_name,
                        category=category,
                        overwrites=overwrites,
                        topic=f"Ticket {ticket_id} - {user}"
                    )
                except Exception as e:
                    # Channel creation failed: roll back the pending DB row so we
                    # don't leave an orphan ticket with no channel.
                    logging.error(f"[TicketTool] Failed to create channel: {e}")
                    try:
                        await self.data_manager.async_save_ticket({
                            **ticket_data, 'status': 'failed', 'close_reason': f'Channel creation failed: {e}',
                            'closed_at': datetime.now(timezone.utc).isoformat(),
                        })
                    except Exception:
                        pass
                    return None, f"Failed to create ticket channel: {e}"

            # ---- STEP 3: update the row with the real channel_id and status='open' ----
            ticket_data['channel_id'] = channel.id
            ticket_data['status'] = 'open'
            try:
                await self.data_manager.async_save_ticket(ticket_data)
            except Exception as e:
                # DB update failed after the channel exists. Try to clean up the
                # channel so we don't leave an orphan channel with no DB record.
                logging.error(f"[TicketTool] DB save failed after channel creation: {e}")
                try:
                    await channel.delete(reason="Ticket DB record could not be saved")
                except Exception:
                    pass
                return None, "Ticket channel was created but could not be saved. Please try again."
        
        # ---- STEP 4: persist panel answers (non-fatal if this fails) ----
        # Outside the lock: answer persistence doesn't affect the limit check.
        if answers:
            for qid, answer_text in answers.items():
                try:
                    await self.data_manager.async_save_ticket_answer({
                        'answer_id': str(uuid.uuid4())[:8],
                        'ticket_id': ticket_id,
                        'question_id': qid,
                        'user_id': user.id,
                        'answer_text': answer_text,
                        'answered_at': datetime.now(timezone.utc).isoformat()
                    })
                except Exception as e:
                    logging.warning(f"[TicketTool] Failed to save answer for {ticket_id}: {e}")

        # --- PREMIUM TIER 1: on_ticket_create hook ---
        # Fires role-automation 'open', schedules delayed/no_response
        # automations, initializes SLA state, and fires the 'created' trigger.
        # Reload the ticket row so the hook sees the final 'open' status.
        if PREMIUM_AVAILABLE:
            try:
                fresh_ticket = await self.data_manager.async_load_ticket(ticket_id)
                if fresh_ticket and ticket_number is not None:
                    # Reuse the number reserved for the channel name so the
                    # wiring hook doesn't reserve a second one.
                    fresh_ticket['_count'] = ticket_number
                await TicketTool.wiring.on_ticket_create(
                    bot=self.bot, ticket_tool=self, channel=channel,
                    ticket=fresh_ticket or ticket_data, panel=panel,
                    creator=user,
                )
            except Exception as exc:
                logging.warning(f"[TicketTool] TicketTool.on_ticket_create failed: {exc}")

        # TicketTool-style ticket logging: "Ticket Created" entry in the
        # configured log channel.
        try:
            await log_ticket_event(
                guild, 'created', ticket_data,
                actor=user,
                detail=f"Panel: {panel.get('name', 'General') if panel else 'General'}",
                channel_ref=channel,
            )
        except Exception as exc:
            logging.debug(f"[TicketLog] created event failed: {exc}")

        return channel, ticket_id
    
    async def close_ticket(
        self,
        channel: discord.TextChannel,
        closed_by: discord.Member,
        reason: str = "No reason provided"
    ) -> bool:
        """Close a ticket and generate transcript.

        The open→closing transition is done atomically via
        DataManager.atomic_begin_closing so two near-simultaneous close
        requests (e.g. a staff button click racing the ticket creator's close
        command) can't both proceed to generate a transcript and delete the
        channel. Only the first caller wins the UPDATE; the second sees
        rowcount==0 and returns False immediately.
        """
        ticket = await self.data_manager.async_load_ticket_by_channel(channel.id)
        if not ticket:
            return False

        if ticket.get('status') in ('closed', 'closing'):
            return False

        # Atomic open→closing transition. If this returns False, another caller
        # already started closing (or the ticket was closed/not-found between
        # our load and this UPDATE). Bail out — do NOT generate a transcript.
        won = await asyncio.to_thread(
            self.data_manager.atomic_begin_closing,
            ticket['ticket_id'], closed_by.id, reason,
        )
        if not won:
            logging.info(f"[TicketTool] close_ticket lost the race for {ticket['ticket_id']} (already closing/closed); aborting to avoid duplicate transcript.")
            return False

        # Reload so the in-memory dict reflects the committed 'closing' state
        # (status / closed_by / close_reason) before transcript generation.
        ticket = await self.data_manager.async_load_ticket(ticket['ticket_id'])
        if not ticket:
            logging.error("[TicketTool] Ticket disappeared after atomic_begin_closing; cannot generate transcript.")
            return False
        try:
            transcript = await self._generate_transcript(channel, ticket, closed_by)
        except Exception as e:
            logging.error(f"[TicketTool] Transcript generation failed, aborting close: {e}")
            # Revert to 'open' so the ticket can be retried. Use a conditional
            # UPDATE so we don't clobber a concurrent state change.
            try:
                await asyncio.to_thread(
                    self.data_manager.atomic_revert_closing, ticket['ticket_id']
                )
            except Exception:
                pass
            return False
        
        ticket['status'] = 'closed'
        ticket['closed_at'] = datetime.now(timezone.utc).isoformat()
        try:
            await self.data_manager.async_save_ticket(ticket)
        except Exception as e:
            logging.error(f"[TicketTool] Could not persist final 'closed' status: {e}")
        
        settings = await self.data_manager.async_load_ticket_settings(channel.guild.id) \
            if hasattr(self.data_manager, 'async_load_ticket_settings') \
            else self.data_manager.load_ticket_settings(channel.guild.id)
        settings = settings or {}

        # Resolve the panel row once for the close flow (two-step check, log
        # detail, recycle check all need it).
        panel_row = None
        if ticket.get('panel_id'):
            try:
                panel_row = self.data_manager.load_ticket_panel(ticket['panel_id'])
            except Exception:
                panel_row = None

        # When the premium transcript config is ENABLED, the premium
        # on_ticket_close hook (below) posts the transcript with all the
        # custom message / DM / archive options — skip the default posting so
        # we don't double-post (and double-DM) the same transcript.
        premium_transcripts_active = False
        if PREMIUM_AVAILABLE:
            try:
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    tr_cfg = TicketTool.transcripts.get_config(pdb)
                    premium_transcripts_active = bool(tr_cfg and tr_cfg.get('enabled'))
            except Exception:
                premium_transcripts_active = False

        if not premium_transcripts_active:
            transcripts_channel_id = settings.get('transcripts_channel_id') or getattr(config.channels, 'transcripts', None)
            if transcripts_channel_id:
                transcripts_channel = channel.guild.get_channel(transcripts_channel_id)
                if transcripts_channel:
                    try:
                        await transcripts_channel.send(embed=transcript['embed'], file=transcript['file'])
                        try:
                            await log_ticket_event(channel.guild, 'transcript', ticket, actor=closed_by,
                                                   detail='Posted to transcripts channel')
                        except Exception:
                            pass
                    except Exception as e:
                        logging.warning(f"[Tickets] Could not post transcript to transcripts channel: {e}")
        
        if not premium_transcripts_active and settings.get('dm_transcripts') and ows_get("dm_transcript_on_close"):
            try:
                creator = channel.guild.get_member(ticket['creator_id'])
                if creator:
                    from io import BytesIO
                    dm_file = discord.File(
                        BytesIO(transcript['html'].encode('utf-8')),
                        filename=f"transcript-{ticket['ticket_id']}.html"
                    )
                    await creator.send(
                        embed=discord.Embed(
                            title=f"Ticket Closed - {channel.guild.name}",
                            description=f"Your ticket has been closed.\n**Reason:** {reason}\n\nYour transcript is attached below.",
                            color=discord.Color.orange()
                        ),
                        file=dm_file
                    )
            except Exception as e:
                logging.warning(f"[Tickets] Could not DM transcript to user: {e}")

        # TicketTool-style ticket logging: "Ticket Closed" entry.
        try:
            await log_ticket_event(
                channel.guild, 'closed', ticket, actor=closed_by,
                detail=f"Reason: {reason}",
            )
        except Exception:
            pass

        # --- PREMIUM TIER 1: on_ticket_close hook ---
        # Fires BEFORE channel.delete() so the channel still exists for any
        # final actions (rename to closed template, post custom transcript,
        # apply close role-automation, mark SLA resolution met, cancel delayed
        # automations, fire the 'closed' trigger). Reload the ticket so the
        # hook sees the committed 'closed' status.
        if PREMIUM_AVAILABLE:
            try:
                closed_ticket = await self.data_manager.async_load_ticket(ticket['ticket_id'])
                await TicketTool.wiring.on_ticket_close(
                    bot=self.bot, ticket_tool=self, channel=channel,
                    ticket=closed_ticket or ticket, closed_by=closed_by,
                    transcript=transcript, panel=panel_row,
                )
            except Exception as exc:
                logging.warning(f"[TicketTool] TicketTool.on_ticket_close failed: {exc}")

        # --- TicketTool "Two Step Ticket": retain the closed channel ---
        # When the panel enables two_step_ticket, do NOT delete/recycle the
        # channel: apply the closed permission set, move to the closed
        # category, and post the moderator message (Re-Open / Delete /
        # Transcript buttons). Staff can later re-open in place.
        if panel_row and panel_row.get('two_step_ticket') and isinstance(channel, discord.TextChannel):
            try:
                closed_ticket = await self.data_manager.async_load_ticket(ticket['ticket_id'])
                await apply_closed_ticket_state(channel, closed_ticket or ticket, panel_row, closed_by)
                return True
            except Exception as exc:
                logging.warning(f"[TwoStep] closed-state application failed, falling back to delete: {exc}")

        # --- PREMIUM TIER 2: channel recycling check ---
        # If the panel has recycling enabled, recycle the channel instead of
        # deleting it. on_ticket_close_recycle_check returns True if recycled.
        recycled = False
        if PREMIUM_AVAILABLE:
            try:
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    recycled = await TicketTool.wiring.on_ticket_close_recycle_check(
                        bot=self.bot, pdb=pdb, channel=channel, panel=panel_row,
                    )
            except Exception as exc:
                logging.warning(f"[TicketTool] recycle check failed: {exc}")

        if not recycled:
            try:
                await channel.delete(reason=f"Ticket closed by {closed_by}: {reason}")
            except Exception as e:
                logging.warning(f"[Tickets] Could not delete channel {channel.id}: {e}")
        return True
    
    def _is_ticket_staff(self, member: discord.Member, ticket: Dict) -> bool:
        """Return True if `member` is allowed to act as staff on this ticket.

        Staff = a member with Manage Channels/Administrator, OR a member who
        holds the ticket's support role (resolved from the ticket's panel
        first, falling back to the guild ticket settings). The ticket creator
        is NEVER considered staff for their own ticket — this is what prevents
        a user from claiming their own ticket via the Claim button.
        """
        if member is None:
            return False
        try:
            if member.guild_permissions.administrator or member.guild_permissions.manage_channels:
                return True
        except Exception:
            pass

        support_role_id = None
        panel_id = ticket.get('panel_id') if ticket else None
        if panel_id:
            try:
                panel = self.data_manager.load_ticket_panel(panel_id)
                if panel:
                    support_role_id = panel.get('support_role_id')
            except Exception:
                pass
        if not support_role_id:
            try:
                settings = self.data_manager.load_ticket_settings(member.guild.id)
                if settings:
                    support_role_id = settings.get('support_role_id')
            except Exception:
                pass
        if support_role_id:
            role = member.guild.get_role(support_role_id)
            if role and role in member.roles:
                return True
        return False

    async def claim_ticket(self, channel: discord.TextChannel, user: discord.Member) -> Tuple[bool, str]:
        """Claim a ticket atomically.

        Uses a single conditional UPDATE so two staff members clicking Claim
        at nearly the same time can't both succeed — only the first UPDATE
        affects a row, the second is a no-op.

        PERMISSION FIX: only staff (Manage Channels / Administrator / support
        role) may claim, and the ticket creator is explicitly blocked from
        claiming their own ticket. Without this, a ticket creator who has
        send_messages access to the channel could click Claim and take
        ownership of their own support ticket.
        """
        ticket = await self.data_manager.async_load_ticket_by_channel(channel.id)
        if not ticket:
            return False, "This is not a ticket channel."

        # --- PREMIUM TIER 1: advanced-claim policy ---
        # Honors per-panel allow_owner_claim / auto_replace_claimer. If premium
        # is unavailable or has no config, this block is a no-op and the
        # original (stricter) logic below applies unchanged.
        panel_row = None
        if PREMIUM_AVAILABLE and ticket.get('panel_id'):
            try:
                panel_row = self.data_manager.load_ticket_panel(ticket['panel_id'])
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    allowed, why = TicketTool.claiming.should_allow_claim(
                        pdb, member=user, ticket=ticket, panel=panel_row,
                        is_staff=self._is_ticket_staff(user, ticket),
                    )
                    if not allowed:
                        return False, why
                    # auto_replace_claimer: clear any existing claim first so
                    # the atomic UPDATE below succeeds.
                    if TicketTool.claiming.should_auto_replace(pdb, panel_row) and ticket.get('claimed_by'):
                        try:
                            await asyncio.to_thread(
                                self.data_manager.atomic_clear_claim, ticket['ticket_id']
                            )
                        except AttributeError:
                            # atomic_clear_claim not present: fall back to a
                            # direct save (slightly less race-safe but fine).
                            ticket['claimed_by'] = None
                            ticket['claimed_at'] = None
                            self.data_manager.save_ticket(ticket)
                        ticket = await self.data_manager.async_load_ticket_by_channel(channel.id)
            except Exception as exc:
                logging.warning(f"[TicketTool] premium claim policy failed: {exc}")

        # The ticket creator may never claim their own ticket.
        if ticket.get('creator_id') == user.id:
            return False, "You can't claim your own ticket. Please wait for staff to respond."

        # Only staff may claim.
        if not self._is_ticket_staff(user, ticket):
            return False, "Only staff can claim tickets."

        success, msg = await asyncio.to_thread(
            self.data_manager.atomic_claim_ticket, ticket['ticket_id'], user.id
        )
        if not success:
            # Already claimed — return a friendly message naming the claimer.
            existing_id = ticket.get('claimed_by')
            if not existing_id:
                # Reload to get the freshly written claimer.
                fresh = await self.data_manager.async_load_ticket(ticket['ticket_id'])
                existing_id = fresh.get('claimed_by') if fresh else None
            if existing_id:
                claimer = channel.guild.get_member(existing_id)
                return False, f"This ticket is already claimed by {claimer.mention if claimer else 'someone'}."
            return False, msg

        # --- PREMIUM TIER 1: on_ticket_claim hook ---
        # Applies rename/move/hide/perms/custom-message + claim role-automation
        # + fires the 'claim' trigger. Reload the ticket so the hook sees the
        # committed claimed_by.
        if PREMIUM_AVAILABLE:
            try:
                claimed_ticket = await self.data_manager.async_load_ticket(ticket['ticket_id'])
                await TicketTool.wiring.on_ticket_claim(
                    bot=self.bot, ticket_tool=self, channel=channel,
                    ticket=claimed_ticket or ticket, panel=panel_row, claimer=user,
                )
            except Exception as exc:
                logging.warning(f"[TicketTool] TicketTool.on_ticket_claim failed: {exc}")

        # TicketTool-style ticket logging: "Ticket Claimed" entry.
        try:
            await log_ticket_event(channel.guild, 'claim', ticket, actor=user)
        except Exception:
            pass
        return True, f"Ticket claimed by {user.mention}"
    
    async def unclaim_ticket(self, channel: discord.TextChannel, user: discord.Member) -> Tuple[bool, str]:
        """Release a ticket claim."""
        ticket = self.data_manager.load_ticket_by_channel(channel.id)
        if not ticket:
            return False, "This is not a ticket channel."

        if not ticket.get('claimed_by'):
            return False, "This ticket is not claimed."

        # --- PREMIUM TIER 1: advanced-claim unclaim gating ---
        # only_claimer_unclaim is enforced here (admin always bypasses).
        panel_row = None
        if PREMIUM_AVAILABLE and ticket.get('panel_id'):
            try:
                panel_row = self.data_manager.load_ticket_panel(ticket['panel_id'])
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    allowed, why = TicketTool.claiming.is_authorized(
                        pdb, actor=user, action='unclaim', ticket=ticket,
                        panel=panel_row,
                        is_admin=user.guild_permissions.administrator,
                    )
                    if not allowed:
                        return False, why
            except Exception as exc:
                logging.warning(f"[TicketTool] premium unclaim policy failed: {exc}")

        if ticket['claimed_by'] != user.id and not user.guild_permissions.administrator:
            return False, "You can only unclaim tickets you claimed (or be admin)."

        # Capture the previous claimer so the side-effects hook can reverse
        # their claim roles (role_automation 'unclaim' is applied to the
        # CLAIMER, not the actor who released it).
        previous_claimer_id = ticket.get('claimed_by')
        ticket['claimed_by'] = None
        ticket['claimed_at'] = None
        self.data_manager.save_ticket(ticket)

        # --- PREMIUM TIER 1: on_ticket_unclaim hook ---
        # Reverses rename/perms/custom-message + applies unclaim role-automation
        # + fires the 'unclaim' trigger.
        if PREMIUM_AVAILABLE:
            try:
                unclaimer = user
                if previous_claimer_id and int(previous_claimer_id) != user.id:
                    unclaimer = channel.guild.get_member(int(previous_claimer_id)) or user
                await TicketTool.wiring.on_ticket_unclaim(
                    bot=self.bot, ticket_tool=self, channel=channel,
                    ticket=ticket, panel=panel_row, unclaimer=unclaimer,
                )
            except Exception as exc:
                logging.warning(f"[TicketTool] TicketTool.on_ticket_unclaim failed: {exc}")

        # TicketTool-style ticket logging: "Ticket Unclaimed" entry.
        try:
            await log_ticket_event(channel.guild, 'unclaim', ticket, actor=user)
        except Exception:
            pass
        return True, "Ticket unclaimed."
    
    async def _generate_transcript(self, channel: discord.TextChannel, ticket: Dict,
                                   closed_by: discord.Member, limit: Optional[int] = None) -> Dict:
        """Generate HTML transcript like Ticket Tool.

        `limit` caps the number of messages included (Ticket Tool caps
        transcripts at 1000 messages); None pulls the entire history.
        """
        import uuid
        from io import BytesIO
        
        messages = []
        message_count = 0
        
        # limit=None pulls the entire channel history. Discord paginates this
        # under the hood (100 msgs per request), so very long tickets take
        # longer but are no longer silently truncated at 500 messages.
        # When a limit is requested we fetch newest-first then reverse, so the
        # transcript contains the MOST RECENT `limit` messages in order.
        history_limit = limit if limit is not None else None
        oldest_first = limit is None
        async for msg in channel.history(limit=history_limit, oldest_first=oldest_first):
            if msg.author.bot and msg.embeds:
                continue  # Skip bot embeds
            
            message_count += 1
            messages.append({
                'author_id': msg.author.id,
                'author_name': msg.author.display_name,
                'author_avatar': str(msg.author.avatar.url) if msg.author.avatar else str(msg.author.default_avatar.url),
                'content': msg.content,
                'attachments': [att.url for att in msg.attachments],
                'timestamp': msg.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'embeds': len(msg.embeds)
            })
        if limit is not None:
            messages.reverse()

        # FALLBACK: if channel history is empty (e.g. messages were
        # bulk-deleted, or the channel was partially lost before close),
        # reconstruct the message list from the ticket_messages backup table
        # that on_message populates. This keeps the transcript usable even
        # when Discord's own history is gone, and gives the previously-unused
        # ticket_messages table a real purpose.
        if not messages:
            try:
                backup = await self.data_manager.async_load_ticket_messages(ticket['ticket_id'])
            except Exception as exc:
                logging.warning(f"[TicketTool] ticket_messages fallback load failed: {exc}")
                backup = []
            for row in backup:
                raw_atts = row.get('attachments')
                if isinstance(raw_atts, str):
                    try:
                        atts = json.loads(raw_atts) if raw_atts else []
                    except Exception:
                        atts = []
                elif isinstance(raw_atts, list):
                    atts = raw_atts
                else:
                    atts = []
                # Normalize the stored created_at ISO timestamp into the
                # 'YYYY-MM-DD HH:MM:SS' display format the transcript expects.
                ts_raw = row.get('created_at') or ''
                try:
                    parsed = datetime.fromisoformat(ts_raw.replace('Z', '+00:00'))
                    ts_disp = parsed.strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    ts_disp = ts_raw[:19] if ts_raw else ''
                messages.append({
                    'author_id': row.get('author_id'),
                    'author_name': row.get('author_name', 'Unknown'),
                    'author_avatar': row.get('author_avatar', ''),
                    'content': row.get('content', ''),
                    'attachments': atts,
                    'timestamp': ts_disp,
                    'embeds': 0,
                })
            message_count = len(messages)
            if messages:
                logging.info(f"[TicketTool] Transcript used ticket_messages fallback ({message_count} rows) for {ticket['ticket_id']}")

        # Generate HTML (all user-controlled values are escaped inside).
        html_content = self._generate_html_transcript(channel, ticket, messages, closed_by)
        
        # Save transcript to database
        transcript_id = str(uuid.uuid4())[:8]
        transcript_data = {
            'transcript_id': transcript_id,
            'ticket_id': ticket['ticket_id'],
            'guild_id': channel.guild.id,
            'channel_id': channel.id,
            'creator_id': ticket['creator_id'],
            'closed_by': closed_by.id,
            'claimed_by': ticket.get('claimed_by'),
            'category': ticket.get('category'),
            'created_at': ticket.get('created_at'),
            'closed_at': datetime.now(timezone.utc).isoformat(),
            'message_count': message_count,
            'html_content': html_content
        }
        await self.data_manager.async_save_transcript(transcript_data)
        
        # Create embed with better formatting like Ticket Tool
        embed = discord.Embed(
            title=f"📋 Ticket Transcript - {ticket['ticket_id']}",
            description=(
                f"**Type:** {ticket.get('category', 'General')}\n"
                f"**Category:** {_resolve_ticket_category_for_display(channel.guild.id, ticket)}\n"
                f"**Subject:** {ticket.get('subject', 'N/A')}"
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        creator = channel.guild.get_member(ticket['creator_id'])
        embed.add_field(name="👤 Creator", value=creator.mention if creator else f"<@{ticket['creator_id']}>", inline=True)
        embed.add_field(name="🔒 Closed By", value=closed_by.mention, inline=True)
        embed.add_field(name="💬 Messages", value=str(message_count), inline=True)
        
        if ticket.get('claimed_by'):
            claimer = channel.guild.get_member(ticket['claimed_by'])
            embed.add_field(name="🙋 Claimed By", value=claimer.mention if claimer else f"<@{ticket['claimed_by']}>", inline=True)
        
        # Add message preview (first 5 messages) - this was missing!
        if messages:
            preview_text = ""
            for i, msg in enumerate(messages[:5]):
                content_preview = msg.get('content', '')[:100]
                if len(msg.get('content', '')) > 100:
                    content_preview += "..."
                preview_text += f"**{msg.get('author_name', 'Unknown')}:** {content_preview}\n"
            if len(messages) > 5:
                preview_text += f"\n*...and {len(messages) - 5} more messages*"
            embed.add_field(name="📝 Message Preview", value=preview_text or "No messages", inline=False)
        
        # Add ticket duration
        if ticket.get('created_at'):
            try:
                created = datetime.fromisoformat(ticket['created_at'].replace('Z', '+00:00'))
                duration = datetime.now(timezone.utc) - created
                hours, remainder = divmod(int(duration.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                duration_str = f"{hours}h {minutes}m {seconds}s" if hours > 0 else f"{minutes}m {seconds}s"
                embed.add_field(name="⏱️ Duration", value=duration_str, inline=True)
            except:
                pass
        
        embed.set_footer(text=f"Ticket ID: {ticket['ticket_id']} • Download HTML for full transcript")
        
        # Create file
        file = discord.File(
            BytesIO(html_content.encode('utf-8')),
            filename=f"transcript-{ticket['ticket_id']}.html"
        )
        
        return {'embed': embed, 'file': file, 'html': html_content}
    
    def _generate_html_transcript(self, channel: discord.TextChannel, ticket: Dict, messages: List[Dict], closed_by: discord.Member) -> str:
        """Generate a Ticket Tool style HTML transcript.

        SECURITY: every value that comes from a user-controlled source
        (message content, display names, attachment URLs, panel names) is
        HTML-escaped via _esc() / _safe_url() before being interpolated.
        This closes the XSS vector where a ticket message containing
        `<img src=x onerror=...>` would execute when the transcript was
        opened in a browser.
        """
        guild = channel.guild
        creator = guild.get_member(ticket['creator_id'])

        # Pre-escape header values.
        esc_ticket_id = _esc(ticket['ticket_id'])
        esc_category = _esc(ticket.get('category', 'General'))
        esc_created_at = _esc((ticket.get('created_at') or 'N/A')[:19])
        esc_creator = _esc(creator.display_name if creator else 'Unknown')
        esc_closed_by = _esc(closed_by.display_name)
        esc_bot_name = _esc(self.bot.user.name if self.bot.user else 'Bot')
        claimed_by = ticket.get('claimed_by')
        closed_now = _esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ticket Transcript - {esc_ticket_id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #fff;
        }}
        .container {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
        .header {{
            background: linear-gradient(135deg, #5865F2 0%, #7289DA 100%);
            padding: 30px;
            border-radius: 15px;
            margin-bottom: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
        }}
        .header h1 {{ font-size: 28px; margin-bottom: 10px; }}
        .header .info {{ display: flex; gap: 20px; flex-wrap: wrap; margin-top: 15px; }}
        .header .info-item {{ 
            background: rgba(255,255,255,0.1);
            padding: 8px 15px;
            border-radius: 8px;
            font-size: 14px;
        }}
        .messages {{ background: #2f3136; border-radius: 15px; overflow: hidden; }}
        .message {{
            padding: 15px 20px;
            border-bottom: 1px solid #36393f;
            display: flex;
            gap: 15px;
        }}
        .message:hover {{ background: rgba(79, 84, 92, 0.16); }}
        .message:last-child {{ border-bottom: none; }}
        .message-avatar {{ width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0; }}
        .message-content {{ flex: 1; }}
        .message-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 5px; }}
        .message-author {{ font-weight: 600; color: #fff; }}
        .message-timestamp {{ font-size: 12px; color: #72767d; }}
        .message-text {{ color: #dcddde; line-height: 1.5; word-wrap: break-word; white-space: pre-wrap; }}
        .attachment {{
            background: #2f3136;
            border: 1px solid #4f545c;
            border-radius: 8px;
            padding: 10px;
            margin-top: 8px;
            display: inline-block;
        }}
        .attachment a {{ color: #00b0f4; text-decoration: none; }}
        .footer {{
            text-align: center;
            padding: 20px;
            color: #72767d;
            font-size: 14px;
        }}
        .claimed-badge {{
            background: #faa61a;
            color: #000;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 12px;
            margin-left: 10px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Ticket Transcript</h1>
            <div class="info">
                <div class="info-item">ID: {esc_ticket_id}</div>
                <div class="info-item">Category: {esc_category}</div>
                <div class="info-item">Created: {esc_created_at}</div>
                <div class="info-item">Closed: {closed_now}</div>
            </div>
            <div class="info" style="margin-top: 10px;">
                <div class="info-item">Creator: {esc_creator}</div>
                <div class="info-item">Closed By: {esc_closed_by}</div>
                {f'<div class="info-item">Claimed By: {_esc(claimed_by)}</div>' if claimed_by else ''}
            </div>
        </div>
        <div class="messages">
"""
        for msg in messages:
            # Escape every user-controlled field. content is rendered with
            # white-space: pre-wrap so newlines are preserved without <br>.
            esc_author_name = _esc(msg.get('author_name', 'Unknown'))
            esc_author_avatar = _safe_url(msg.get('author_avatar', ''))
            esc_timestamp = _esc(msg.get('timestamp', ''))
            content = msg.get('content') or ''
            esc_content = _esc(content) if content else '<em>No content</em>'

            html += f"""
            <div class="message">
                <img class="message-avatar" src="{esc_author_avatar}" alt="Avatar">
                <div class="message-content">
                    <div class="message-header">
                        <span class="message-author">{esc_author_name}</span>
                        <span class="message-timestamp">{esc_timestamp}</span>
                    </div>
                    <div class="message-text">{esc_content}</div>
"""
            for att in (msg.get('attachments') or []):
                safe_att = _safe_url(att)
                if safe_att:
                    html += f"""
                    <div class="attachment"><a href="{safe_att}" target="_blank" rel="noopener noreferrer">📎 Attachment</a></div>
"""
            html += """
                </div>
            </div>
"""

        html += f"""
        </div>
        <div class="footer">
            Generated by {esc_bot_name} • {closed_now}
        </div>
    </div>
</body>
</html>"""

        return html


# Global ticket tool instance


def build_ticket_commands_embed(panel: Optional[Dict] = None) -> discord.Embed:
    """Build an embed listing ONLY the commands available inside a ticket channel.

    This is shown the moment a ticket opens, BEFORE the welcome message, so the
    user immediately knows which commands they (and staff) can run in-ticket.
    Only in-ticket commands are listed — panel/settings/blacklist admin commands
    are intentionally excluded.
    """
    try:
        color = discord.Color(panel.get('embed_color', 0x5865F2)) if panel else discord.Color.blurple()
    except (TypeError, ValueError):
        color = discord.Color.blurple()

    embed = discord.Embed(
        title="📋 Ticket Commands",
        description=(
            "Welcome! Here are the commands you can use inside this ticket.\n"
            "Use them with the bot prefix."
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="🙋 Claiming & Closing",
        value=(
            "`!claim` — Claim this ticket\n"
            "`!unclaim` — Release your claim\n"
            "`!close [reason]` — Close this ticket\n"
            "`!closerequest [reason]` — Request staff to close (alias `!ca`)\n"
            "`!rate` — Staff: send the rating prompt"
        ),
        inline=False,
    )

    embed.add_field(
        name="⏸️ Automation & Info",
        value=(
            "`!pause [duration]` — Pause all automations (30m/1h/2d/1w)\n"
            "`!resume` — Resume automations\n"
            "`!ticket-info` — Full status overview of this ticket"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔒 Privacy",
        value=(
            "`!private` — Hide this ticket from other staff\n"
            "`!unprivate` — Restore staff access"
        ),
        inline=False,
    )

    embed.add_field(
        name="📜 Transcripts",
        value="`!transcript [channel] [lines]` — Generate a transcript of this ticket",
        inline=False,
    )

    embed.add_field(
        name="👥 Members",
        value=(
            "`!add @user|@role` — Add a user or role to this ticket\n"
            "`!remove @user|@role` — Remove a user or role from this ticket"
        ),
        inline=False,
    )

    embed.add_field(
        name="📝 Notes & Priority",
        value=(
            "`!note <text>` — Add a private staff note\n"
            "`!notes` — View staff notes for this ticket\n"
            "`!priority <level>` — Set priority (low/normal/high/urgent)\n"
            "`!setcategory` — Set this ticket's category (folder)"
        ),
        inline=False,
    )

    embed.add_field(
        name="💬 Canned Replies",
        value=(
            "`!canned send <name>` — Insert a saved response\n"
            "`!canned list` — Browse saved responses"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔁 Channel Management",
        value=(
            "`!rename <name>` — Rename this ticket channel\n"
            "`!move <panel_id>` — Move this ticket to another category\n"
            "`!reopen <ticket_id>` — Reopen a closed ticket"
        ),
        inline=False,
    )

    embed.set_footer(text="Ticket Commands • Available in this ticket channel")
    return embed


# =============================================================================
# TICKET LOGGING (TicketTool-style Logging Channel)
# =============================================================================
# Ticket Tool's free tier logs toggleable ticket actions (Created, Closed,
# (Re)Opened, Renamed, Deleted, Transcript Saved) to a configured log channel.
# The bot's ticket_settings.log_channel_id column existed but was never read;
# this implementation makes it functional, with a configurable event list.
# =============================================================================

TICKET_LOG_EVENT_INFO: Dict[str, Tuple[str, int]] = {
    'created':   ('🎫 Ticket Created',     0x57F287),
    'closed':    ('🔒 Ticket Closed',      0xE67E22),
    'reopened':  ('🔓 Ticket Reopened',    0x57F287),
    'renamed':   ('✏️ Ticket Renamed',     0x5865F2),
    'deleted':   ('🗑️ Ticket Deleted',     0xED4245),
    'transcript': ('📜 Transcript Saved',  0x5865F2),
    'claim':     ('🙋 Ticket Claimed',     0x57F287),
    'unclaim':   ('🙋 Ticket Unclaimed',   0xE67E22),
    'priority':  ('🚨 Ticket Priority Changed', 0xFEE75C),
    'category':  ('📁 Ticket Category Changed', 0x5865F2),
}
# Events logged when the guild has no explicit log_events config
# (matches Ticket Tool's free-tier default action set).
DEFAULT_TICKET_LOG_EVENTS = ['created', 'closed', 'reopened', 'renamed', 'deleted', 'transcript']


def get_ticket_log_events(guild_id: int) -> List[str]:
    """Parse a guild's configured ticket-log event list (JSON array)."""
    try:
        settings = data_manager.load_ticket_settings(guild_id) or {}
        raw = settings.get('log_events')
        if raw:
            events = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(events, list):
                return [str(e) for e in events if e in TICKET_LOG_EVENT_INFO]
    except Exception:
        pass
    return list(DEFAULT_TICKET_LOG_EVENTS)


async def log_ticket_event(guild: discord.Guild, event: str, ticket: Optional[Dict],
                           *, actor: Optional[discord.abc.User] = None,
                           detail: Optional[str] = None,
                           channel_ref: Optional[discord.abc.GuildChannel] = None) -> bool:
    """Post a TicketTool-style log entry to the guild's ticket log channel.

    Returns True when an embed was actually posted. Silently no-ops when no
    log channel is configured or the event is not in the guild's event list,
    so call sites never need their own guards.
    """
    if guild is None or data_manager is None:
        return False
    try:
        settings = data_manager.load_ticket_settings(guild.id)
    except Exception:
        settings = None
    if not settings or not settings.get('log_channel_id'):
        return False
    if event not in get_ticket_log_events(guild.id):
        return False
    log_channel = guild.get_channel(settings['log_channel_id'])
    if log_channel is None:
        return False

    title, color = TICKET_LOG_EVENT_INFO.get(event, (f'Ticket {event}', 0x5865F2))
    embed = discord.Embed(title=title, color=discord.Color(color),
                          timestamp=datetime.now(timezone.utc))

    desc_lines: List[str] = []
    if ticket:
        ticket_channel_id = ticket.get('channel_id')
        channel_mention = f"<#{ticket_channel_id}>" if ticket_channel_id else "`deleted`"
        desc_lines.append(f"**Ticket:** `{ticket.get('ticket_id', '?')}` ({channel_mention})")
        creator_id = ticket.get('creator_id')
        if creator_id:
            desc_lines.append(f"**Creator:** <@{creator_id}>")
        panel_name = ticket.get('category')
        if panel_name:
            desc_lines.append(f"**Panel:** {panel_name}")
    if actor:
        desc_lines.append(f"**By:** {actor.mention} (`{actor.display_name}`)")
    if channel_ref is not None and not ticket:
        desc_lines.append(f"**Channel:** <#{channel_ref.id}>")
    if detail:
        desc_lines.append(f"**Detail:** {detail}")
    embed.description = '\n'.join(desc_lines) or None
    embed.set_footer(text=f"Ticket Log • {guild.name}")

    try:
        await log_channel.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        logging.warning(f"[TicketLog] Could not post {event} event: {exc}")
        return False


# --- TICKET CATEGORY HELPERS (internal ticket "folders") ---
# A Ticket Category groups related tickets inside the bot (e.g. a "Staff"
# category containing "Apply for Staff" and "Staff Training" tickets). It is
# completely separate from Discord channel categories — tickets in the same
# category can still live in the same Discord channel category.
UNCATEGORIZED_LABEL = 'Uncategorized'
_CUSTOM_EMOJI_RE = re.compile(r'^<a?(:[^:]+:)(\d{15,21})>$')


def _validate_category_emoji(raw: str) -> Tuple[bool, str]:
    """Validate an optional category emoji/icon.

    Accepts either a single unicode emoji (or any short non-space string,
    e.g. '🎫' or '⭐') or a full custom emoji mention like ``<:name:id>`` /
    ``<a:name:id>``. Returns (ok, cleaned_value).
    """
    value = (raw or '').strip()
    if not value:
        return True, ''  # empty = no icon, always allowed
    if ':' in value:
        if _CUSTOM_EMOJI_RE.match(value):
            return True, value
        return False, value
    if len(value) > 32 or any(ch.isspace() for ch in value):
        return False, value
    return True, value


def _ticket_category_label(guild_id: Optional[int], category_id: Optional[str]) -> str:
    """Display label ('emoji Name' or plain name) for a category id.

    Falls back to ``Uncategorized`` when the id is empty/unknown — this is
    what tickets created before the feature (or after a category deletion)
    show. Uses the sync DataManager because every call site here already runs
    in a worker thread or is a cheap indexed lookup.
    """
    if category_id and data_manager is not None:
        try:
            row = data_manager.load_ticket_category(category_id)
            if row and row.get('guild_id') == (guild_id if guild_id is not None else row.get('guild_id')):
                emoji = (row.get('emoji') or '').strip()
                return f"{emoji} {row['name']}".strip()
        except Exception:
            pass
    return UNCATEGORIZED_LABEL


def _resolve_ticket_category_for_display(guild_id: Optional[int], ticket: Optional[Dict]) -> str:
    """Label for a ticket's category row in embeds (Type/Category split:
    ``ticket['category']`` is the panel name = ticket TYPE; the new
    ``ticket['ticket_category_id']`` is the internal folder)."""
    if not ticket:
        return UNCATEGORIZED_LABEL
    return _ticket_category_label(guild_id, ticket.get('ticket_category_id'))


def _member_has_limit_bypass(member: discord.Member, panel: Optional[Dict],
                             settings: Optional[Dict]) -> bool:
    """TicketTool-style limit bypass: members holding any bypass role skip the
    open-ticket limits. Bypass roles are configured per panel
    (ticket_panels.limit_bypass_role_ids, JSON array)."""
    if member is None:
        return False
    raw = panel.get('limit_bypass_role_ids') if panel else None
    if not raw:
        return False
    try:
        role_ids = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return False
    if not isinstance(role_ids, list) or not role_ids:
        return False
    member_role_ids = {r.id for r in getattr(member, 'roles', [])}
    return any(int(rid) in member_role_ids for rid in role_ids)


def _ticket_automation_paused(ticket: Optional[Dict]) -> bool:
    """Pure pause check for Bot.py-side automation consumers (auto-close).

    A pause is active when automation_paused=1 AND (no auto-resume deadline
    OR the deadline is still in the future). Mirrors
    TicketTool.automations.is_ticket_paused without importing the package.
    """
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


_DURATION_RE = None  # compiled lazily


def parse_pause_duration(raw: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """Parse a Ticket Tool-style pause duration ("30m", "1h", "2d", "1w").

    Returns (seconds, error). Passing None / "indefinite" / "forever" (or an
    unparseable value) yields an indefinite pause: (None, None)."""
    import re as _re
    global _DURATION_RE
    if _DURATION_RE is None:
        _DURATION_RE = _re.compile(r'^\s*(\d{1,4})\s*([smhdw])\s*$', _re.IGNORECASE)
    if not raw or not str(raw).strip():
        return None, None  # indefinite
    text = str(raw).strip().lower()
    if text in ('indefinite', 'forever', 'inf', 'none', '-'):
        return None, None
    m = _DURATION_RE.match(text)
    if not m:
        return None, (f"Invalid duration `{raw}`. Use e.g. `30m`, `1h`, `2d`, `1w` — "
                      "or omit it for an indefinite pause.")
    value = int(m.group(1))
    unit = m.group(2).lower()
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    return value * multipliers[unit], None


def _build_panel_message_embeds(panel: Dict) -> List[discord.Embed]:
    """Build the embed(s) for a panel message.

    Uses the premium multi-embed set when the panel has multi-embed enabled
    and embeds configured; otherwise the classic single panel embed. This
    wires TicketTool.multi_embed.build_panel_embeds into the panel message
    rendering (previously configurable but never displayed).
    """
    default = discord.Embed(
        title=panel.get('embed_title') or 'Support Tickets',
        description=panel.get('embed_description') or 'Click the button below to create a ticket.',
        color=discord.Color(panel.get('embed_color', 0x5865F2)),
    )
    if not PREMIUM_AVAILABLE:
        return [default]
    try:
        pdb = getattr(state.bot, 'premium_db', None)
        if pdb is not None and TicketTool.multi_embed.is_multi_embed_enabled(panel):
            embeds = TicketTool.multi_embed.build_panel_embeds(pdb, panel)
            if embeds:
                return embeds[:10]
    except Exception as exc:
        logging.debug(f"[Premium] multi-embed panel build failed: {exc}")
    return [default]


# =============================================================================
# TWO-STEP TICKET (TicketTool "Two Step Ticket") + CLOSE REQUEST
# =============================================================================
# When a panel enables two_step_ticket, closing a ticket no longer deletes the
# channel: the ticket enters a Closed state (creator loses write access,
# channel moves to the closed category, a moderator message with
# Re-Open / Delete / Transcript buttons is posted). Staff can re-open the
# ticket in place at any time.
# =============================================================================

async def apply_closed_ticket_state(channel: discord.TextChannel, ticket: Dict,
                                    panel: Dict, closed_by: discord.Member) -> None:
    """Apply the TicketTool-style Closed state to a two-step ticket channel."""
    from modules.support.tickets.views import TicketModeratorView  # deferred import (cycle-safe)
    guild = channel.guild
    creator = guild.get_member(int(ticket.get('creator_id') or 0))
    support_role_id = panel.get('support_role_id')

    # 1) Closed permission set: creator + any added members lose access,
    #    support team keeps read-only visibility (Ticket Tool default closed
    #    permissions).
    try:
        for target, _overwrite in list(channel.overwrites.items()):
            if isinstance(target, discord.Member) and target != guild.me:
                try:
                    await channel.set_permissions(
                        target, view_channel=False, send_messages=False,
                        reason="Ticket closed (two-step)",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
            elif isinstance(target, discord.Role) and support_role_id and target.id == int(support_role_id):
                try:
                    await channel.set_permissions(
                        target, view_channel=True, send_messages=False,
                        read_message_history=True,
                        reason="Ticket closed (two-step)",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
    except Exception as exc:
        logging.warning(f"[TwoStep] closed permission pass failed: {exc}")

    # 2) Move to the closed category when configured.
    try:
        settings = data_manager.load_ticket_settings(guild.id) or {}
        closed_category_id = settings.get('closed_category_id')
        if closed_category_id and channel.category_id != int(closed_category_id):
            closed_category = guild.get_channel(int(closed_category_id))
            if isinstance(closed_category, discord.CategoryChannel):
                await channel.edit(category=closed_category, reason="Ticket closed (two-step)")
    except Exception as exc:
        logging.warning(f"[TwoStep] closed category move failed: {exc}")

    # 3) Post the moderator message with Re-Open / Delete / Transcript buttons.
    view = TicketModeratorView()
    embed = discord.Embed(
        title="🔒 Ticket Closed",
        description=(
            f"This ticket was closed by {closed_by.mention}.\n"
            f"**Reason:** {ticket.get('close_reason') or 'No reason provided'}\n\n"
            "Staff can re-open, delete, or export a transcript with the buttons below."
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    creator_mention = creator.mention if creator else f"<@{ticket.get('creator_id')}>"
    embed.add_field(name="Creator", value=creator_mention, inline=True)
    embed.add_field(name="Ticket ID", value=f"`{ticket.get('ticket_id')}`", inline=True)
    embed.set_footer(text="Two-Step Ticket • Closed state")
    try:
        await channel.send(content=creator_mention, embed=embed, view=view)
    except Exception as exc:
        logging.warning(f"[TwoStep] moderator message failed: {exc}")


async def reopen_ticket_in_place(channel, reopened_by: discord.Member) -> Tuple[bool, str]:
    """Re-open a two-step closed ticket whose channel still exists.

    Restores the open permission set, moves the channel back to the open
    category, applies the open-name template (premium), resets the ticket row,
    fires the premium 'reopened' hook, and logs the event.
    """
    from modules.support.tickets.views import TicketControlView  # deferred import (cycle-safe)
    if not state.ticket_tool:
        return False, "Ticket system not initialized."
    ticket = await state.ticket_tool.data_manager.async_load_ticket_by_channel(channel.id)
    if not ticket:
        return False, "This is not a ticket channel."
    if ticket.get('status') != 'closed':
        return False, "This ticket is not closed."

    guild = channel.guild
    panel = None
    if ticket.get('panel_id'):
        panel = data_manager.load_ticket_panel(ticket['panel_id'])
    settings = data_manager.load_ticket_settings(guild.id) or {}
    creator = guild.get_member(int(ticket.get('creator_id') or 0))
    support_role_id = (
        (panel.get('support_role_id') if panel else None)
        or settings.get('support_role_id')
        or getattr(config.roles, 'ticket_support', None)
    )

    # 1) Restore open permissions: creator full access, support team writable.
    if creator is not None:
        try:
            await channel.set_permissions(
                creator, view_channel=True, send_messages=True,
                read_message_history=True, attach_files=True,
                reason="Ticket reopened",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[TwoStep] reopen creator perms failed: {exc}")
    if support_role_id:
        role = guild.get_role(int(support_role_id))
        if role is not None:
            try:
                await channel.set_permissions(
                    role, view_channel=True, send_messages=True,
                    read_message_history=True, attach_files=True,
                    reason="Ticket reopened",
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                logging.warning(f"[TwoStep] reopen role perms failed: {exc}")

    # 2) Move back to the open category when one is configured.
    open_category_id = (
        (panel.get('category_id') if panel else None)
        or settings.get('category_id')
        or getattr(config.channels, 'tickets', None)
    )
    if open_category_id and getattr(channel, 'category_id', None) != int(open_category_id):
        open_category = guild.get_channel(int(open_category_id))
        if isinstance(open_category, discord.CategoryChannel):
            try:
                await channel.edit(category=open_category, reason="Ticket reopened")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logging.warning(f"[TwoStep] reopen category move failed: {exc}")

    # 3) Reset the ticket row (claim is cleared, matching Ticket Tool's
    #    auto-unclaim-on-reopen behavior).
    ticket['status'] = 'open'
    ticket['closed_at'] = None
    ticket['closed_by'] = None
    ticket['close_reason'] = None
    ticket['claimed_by'] = None
    ticket['claimed_at'] = None
    data_manager.save_ticket(ticket)

    # 4) Apply the premium open-name template when configured.
    if PREMIUM_AVAILABLE:
        try:
            pdb = getattr(state.bot, 'premium_db', None)
            if pdb is not None and panel:
                new_name, _subject = TicketTool.naming.compute_open_name(
                    pdb, panel,
                    guild={'id': guild.id, 'name': guild.name},
                    ticket_id=ticket['ticket_id'],
                    creator={'id': reopened_by.id, 'name': creator.display_name if creator else 'user'},
                    ticket_count=None,
                )
                if new_name:
                    await channel.edit(name=new_name, reason="Ticket reopened (open-name template)")
        except Exception as exc:
            logging.debug(f"[TwoStep] reopen rename failed: {exc}")

    # 5) Post the reopen notice with the standard control buttons.
    creator_mention = creator.mention if creator else f"<@{ticket.get('creator_id')}>"
    try:
        await channel.send(
            embed=discord.Embed(
                title=f"🔓 Ticket Reopened — #{ticket['ticket_id']}",
                description=(
                    f"This ticket was reopened by {reopened_by.mention}.\n"
                    f"{creator_mention} your ticket has been reopened."
                ),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            ),
            view=TicketControlView(ticket['ticket_id']),
        )
    except Exception as exc:
        logging.warning(f"[TwoStep] reopen notice failed: {exc}")

    # 6) Fire the premium 'reopened' automation trigger.
    if PREMIUM_AVAILABLE:
        try:
            await TicketTool.wiring.on_ticket_reopen(
                bot=state.bot, ticket_tool=state.ticket_tool, ticket=ticket,
                panel=panel, guild=guild,
            )
        except Exception as exc:
            logging.warning(f"[Premium] on_ticket_reopen (in-place) failed: {exc}")

    # 7) Log the reopen.
    try:
        await log_ticket_event(guild, 'reopened', ticket, actor=reopened_by)
    except Exception:
        pass
    logging.info(f"[Tickets] {reopened_by} reopened ticket {ticket['ticket_id']} in place")
    return True, f"Ticket `{ticket['ticket_id']}` reopened: {channel.mention}"


# --- TICKET PANEL VIEW (The panel message with create button) ---
PANEL_BUTTON_PREFIX = "create_ticket:"
PANEL_SELECT_CUSTOM_ID = "ticket_panel_select"


async def process_panel_create_request(interaction: discord.Interaction, panel: Dict) -> None:
    """Shared ticket-creation entry point for ALL panel UI styles.

    Runs the full gate chain (system enabled → blacklist → per-panel limit
    with bypass roles → premium business-hours schedule → panel questions)
    and then creates the ticket + welcome message. Used by:
      * TicketPanelView.create_button  (single-button panels)
      * MultiPanelView._on_click       (TicketTool attached panels)
      * TicketPanelSelectView          (TicketTool dropdown-style panels)
    """
    from modules.support.tickets.views import TicketQuestionsModal  # deferred import (cycle-safe)
    from modules.support.tickets.views import TicketPanelView  # deferred import (cycle-safe)
    if not state.ticket_tool:
        await interaction.response.send_message("Ticket system not initialized.", ephemeral=True)
        return

    # Owner Settings gate: refuse new ticket creation when the Tickets
    # System is disabled. Checked here (the user-facing entry point) AND
    # inside create_ticket() so both paths are covered.
    if not ows_get("enable_tickets"):
        await interaction.response.send_message(
            "The ticket system is currently disabled by the server owner. Please try again later.",
            ephemeral=True,
        )
        return

    if not panel:
        await interaction.response.send_message("This ticket panel no longer exists.", ephemeral=True)
        return

    if ows_get("ticket_blacklist"):
        blacklisted, reason = state.ticket_tool.data_manager.is_user_blacklisted(interaction.guild.id, interaction.user.id)
        if blacklisted:
            await interaction.response.send_message(f"You are blacklisted from creating tickets. Reason: {reason}", ephemeral=True)
            return

    # Per-panel ticket limit with TicketTool-style bypass roles. Counts
    # only THIS panel's active tickets.
    if not _member_has_limit_bypass(interaction.user, panel, None):
        ticket_limit = panel.get('ticket_limit', 3)
        if ticket_limit:
            panel_count = state.ticket_tool.data_manager.count_active_tickets_by_creator_and_panel(
                interaction.user.id, interaction.guild.id, panel['panel_id'],
            )
            if panel_count >= int(ticket_limit):
                await interaction.response.send_message(
                    f"You already have {panel_count} open ticket(s) in this panel. Close one before creating another.",
                    ephemeral=True
                )
                return

    # --- PREMIUM TIER 1: business-hours scheduling gate ---
    # Checks the panel's configured schedule. If the panel is currently
    # closed AND the user doesn't hold a bypass role, refuse creation with
    # the panel's unavailable_message (and tell them when it next opens).
    if PREMIUM_AVAILABLE:
        try:
            pdb = getattr(interaction.client, 'premium_db', None)
            if pdb is not None:
                member = interaction.user
                role_ids = [r.id for r in member.roles] if hasattr(member, 'roles') else []
                is_open, unavailable_msg = TicketTool.scheduling.is_panel_open_now(
                    pdb, panel, role_ids,
                )
                if not is_open:
                    next_open = TicketTool.scheduling.next_open_time(pdb, panel)
                    extra = f"\n\n*Opens {next_open}.*" if next_open else ''
                    await interaction.response.send_message(
                        f"{unavailable_msg}{extra}", ephemeral=True
                    )
                    return
        except Exception as exc:
            logging.debug(f"[Premium] scheduling gate failed: {exc}")

    questions = state.ticket_tool.data_manager.load_panel_questions(panel['panel_id'])

    if questions:
        await interaction.response.send_modal(TicketQuestionsModal(panel, questions))
    else:
        await interaction.response.defer(thinking=True, ephemeral=True)
        channel, ticket_id = await state.ticket_tool.create_ticket(
            interaction.guild, interaction.user, panel
        )
        if channel:
            await TicketPanelView(panel)._send_welcome_message(channel, interaction.user, panel)
            await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)
        else:
            await interaction.followup.send(f"Failed to create ticket: {ticket_id}", ephemeral=True)


def build_multi_panel_view(row: Dict, panels: List[Dict]):
    """Build the correct persistent view for a stored multi-panel row.

    style='buttons' → MultiPanelView (one button per panel)
    style='dropdown' → TicketPanelSelectView (select menu, one option per panel)
    """
    from modules.support.tickets.views import TicketPanelSelectView  # deferred import (cycle-safe)
    from modules.support.tickets.views import MultiPanelView  # deferred import (cycle-safe)
    style = (row.get('style') or 'buttons').lower()
    if style == 'dropdown':
        return TicketPanelSelectView(panels, row.get('placeholder') or 'Select a ticket type…')
    return MultiPanelView(panels, per_row=int(row.get('per_row') or 5))


@tasks.loop(minutes=30)
async def check_sla_task() -> None:
    """Alert in ticket channel if SLA response time has been breached."""
    if not state.ticket_tool:
        return
    for guild in state.bot.guilds:
        # PERFORMANCE (Phase 2) — SLA engine dedup: the premium package runs
        # its own 5-minute SLA state machine (ok → warning → breached). When
        # a guild has ANY premium SLA target configured, that engine owns the
        # guild and this legacy 30-min loop must stand down, otherwise both
        # loops alert on the same breach.
        if PREMIUM_AVAILABLE:
            try:
                _pdb = getattr(state.bot, 'premium_db', None)
                if _pdb is not None:
                    _pcfg = TicketTool.sla.get_config(_pdb, guild.id)
                    if _pcfg.get('enabled') and (
                        _pcfg.get('first_response_hours')
                        or _pcfg.get('resolution_hours')
                        or _pcfg.get('urgent_first_response_hours')
                        or _pcfg.get('urgent_resolution_hours')
                    ):
                        continue  # premium SLA engine owns this guild
            except Exception:
                pass  # premium lookup failed — legacy loop keeps the guild
        settings = data_manager.load_ticket_settings(guild.id)
        sla_hours = settings.get('sla_hours', 0) if settings else 0
        if not sla_hours:
            continue
        open_tickets = data_manager.load_tickets_by_guild(guild.id, 'open')
        for ticket in open_tickets:
            if ticket.get('first_response_at'):
                continue  # Already had a staff response
            if ticket.get('sla_warned_at'):
                continue  # Already warned (without faking a response)
            try:
                created = datetime.fromisoformat(ticket['created_at'].replace('Z', '+00:00'))
                elapsed_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
                if elapsed_hours >= sla_hours:
                    channel = guild.get_channel(ticket['channel_id'])
                    if channel:
                        support_role_id = settings.get('support_role_id')
                        mention = f"<@&{support_role_id}>" if support_role_id else "@here"
                        try:
                            await channel.send(
                                f"⚠️ **SLA Breach** — {mention} This ticket has been open for "
                                f"`{elapsed_hours:.1f}h` with no staff response "
                                f"(SLA: {sla_hours}h). Please respond ASAP.",
                                allowed_mentions=discord.AllowedMentions(roles=True, everyone=True)
                            )
                            # Mark warned only — a bot alert is not a staff
                            # reply, so first_response_at stays clean.
                            data_manager.mark_ticket_sla_warned(ticket['ticket_id'])
                        except Exception:
                            pass
            except Exception as e:
                logging.warning(f"[SLA] Error checking ticket {ticket.get('ticket_id')}: {e}")


@check_sla_task.before_loop
async def before_check_sla() -> None:
    await state.bot.wait_until_ready()


@tasks.loop(minutes=15)
async def check_auto_close_task() -> None:
    """Auto-close tickets idle beyond the configured auto_close_hours.

    Enforces the auto-close setting that is stored per panel
    (ticket_panels.auto_close_hours) with a guild-wide fallback
    (ticket_settings.auto_close_hours). Activity = the newest backed-up
    message in ticket_messages, falling back to the ticket's created_at.

    PAUSED TICKETS (Ticket Tool /pause) are skipped entirely, and a ticket
    that was just resumed starts a fresh idle window from its resume time.

    Gated by the OWS toggle `auto_close_tickets` (Tickets category).
    """
    if not state.ticket_tool or not ows_get("auto_close_tickets"):
        return
    now = datetime.now(timezone.utc)
    for guild in state.bot.guilds:
        try:
            settings = data_manager.load_ticket_settings(guild.id)
        except Exception:
            settings = None
        guild_hours = settings.get('auto_close_hours') if settings else None
        open_tickets = data_manager.load_tickets_by_guild(guild.id, 'open')
        for ticket in open_tickets:
            try:
                channel = guild.get_channel(ticket.get('channel_id') or 0)
                if channel is None:
                    continue
                # TicketTool /pause: paused tickets are excluded from ALL
                # automatic actions.
                if _ticket_automation_paused(ticket):
                    continue
                # Per-panel hours override the guild default; skip if neither.
                panel = None
                if ticket.get('panel_id'):
                    panel = data_manager.load_ticket_panel(ticket['panel_id'])
                hours = panel.get('auto_close_hours') if panel else None
                if not hours:
                    hours = guild_hours
                if not hours:
                    continue
                # Idle time = newest backed-up message, else ticket creation.
                # A resumed ticket's idle clock restarts at its resume time
                # (paused time does not count toward inactivity).
                idle_since_raw = data_manager.get_last_ticket_message_time(ticket['ticket_id'])
                if ticket.get('automation_resumed_at'):
                    idle_since_raw = max(
                        (idle_since_raw, ticket['automation_resumed_at']),
                        key=lambda ts: datetime.fromisoformat(str(ts).replace('Z', '+00:00')),
                    ) if idle_since_raw else ticket['automation_resumed_at']
                if not idle_since_raw:
                    idle_since_raw = ticket.get('created_at')
                if not idle_since_raw:
                    continue
                try:
                    idle_since = datetime.fromisoformat(str(idle_since_raw).replace('Z', '+00:00'))
                except ValueError:
                    continue
                idle_hours = (now - idle_since).total_seconds() / 3600
                if idle_hours < float(hours):
                    continue
                try:
                    await channel.send(embed=discord.Embed(
                        description=(
                            f"⏲️ This ticket has been inactive for `{idle_hours:.1f}h` "
                            f"(auto-close threshold: `{hours}h`) and is being closed "
                            f"automatically. A transcript has been saved."
                        ),
                        color=discord.Color.orange(),
                    ))
                except Exception:
                    pass
                await state.ticket_tool.close_ticket(
                    channel, guild.me,
                    f"Automatically closed after {idle_hours:.1f}h of inactivity (threshold: {hours}h)",
                )
                logging.info(
                    f"[AutoClose] Closed ticket {ticket.get('ticket_id')} in {guild.name} "
                    f"after {idle_hours:.1f}h of inactivity"
                )
            except Exception as e:
                logging.warning(f"[AutoClose] Error processing ticket {ticket.get('ticket_id')}: {e}")


@check_auto_close_task.before_loop
async def before_check_auto_close() -> None:
    await state.bot.wait_until_ready()


# In-memory reaction-panel lookup cache: {message_id: {emoji: panel_id}}.
# Populated on startup (on_ready) and on /reactionpanel; a DB fallback keeps
# it correct even if a row was added by another process.
_reaction_panel_cache: Dict[int, Dict[str, str]] = {}


def _get_reaction_panel_mapping(message_id: int) -> Optional[Dict[str, str]]:
    """Resolve {emoji: panel_id} for a reaction-panel message, via cache
    first and the reaction_panels table as fallback."""
    cached = _reaction_panel_cache.get(message_id)
    if cached is not None:
        return cached
    try:
        row = data_manager.load_reaction_panel(message_id)
    except Exception:
        row = None
    if not row:
        return None
    try:
        mapping = json.loads(row.get('mapping') or '{}')
    except (ValueError, TypeError):
        return None
    if not isinstance(mapping, dict) or not mapping:
        return None
    _reaction_panel_cache[message_id] = mapping
    return mapping


async def handle_ticket_reaction_panel(payload: "discord.RawReactionActionEvent") -> None:
    """Open a ticket when a member reacts on a reaction-panel message.

    Runs the same gates as the panel button (system enabled, blacklist,
    limits — schedule is checked inside when premium is available). Failures
    are DM'd to the reactor (reactions have no ephemeral responses). The
    reaction is removed afterwards so the user can re-react later."""
    from modules.support.tickets.views import TicketPanelView  # deferred import (cycle-safe)
    if payload.guild_id is None or payload.user_id == state.bot.user.id:
        return
    member = payload.member
    if member is None or member.bot:
        return

    mapping = _get_reaction_panel_mapping(payload.message_id)
    if not mapping:
        return
    panel_id = mapping.get(str(payload.emoji))
    if not panel_id:
        return

    guild = state.bot.get_guild(payload.guild_id)
    if guild is None:
        return
    panel = data_manager.load_ticket_panel(panel_id)
    if not panel or not panel.get('is_active', 1):
        return

    # Remove the user's reaction first (best-effort) so they can re-react.
    try:
        channel = guild.get_channel_or_thread(payload.channel_id)
        if channel is not None:
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(payload.emoji, member)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        pass

    async def _dm(text: str) -> None:
        try:
            await member.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass

    if not ows_get("enable_tickets"):
        await _dm("The ticket system is currently disabled by the server owner. Please try again later.")
        return
    if ows_get("ticket_blacklist"):
        blacklisted, reason = data_manager.is_user_blacklisted(guild.id, member.id)
        if blacklisted:
            await _dm(f"You are blacklisted from creating tickets. Reason: {reason}")
            return

    # Premium business-hours gate (same as the panel button).
    if PREMIUM_AVAILABLE:
        try:
            pdb = getattr(state.bot, 'premium_db', None)
            if pdb is not None:
                role_ids = [r.id for r in member.roles] if hasattr(member, 'roles') else []
                is_open, unavailable_msg = TicketTool.scheduling.is_panel_open_now(pdb, panel, role_ids)
                if not is_open:
                    next_open = TicketTool.scheduling.next_open_time(pdb, panel)
                    extra = f"\n\n*Opens {next_open}.*" if next_open else ''
                    await _dm(f"{unavailable_msg}{extra}")
                    return
        except Exception as exc:
            logging.debug(f"[ReactionPanel] scheduling gate failed: {exc}")

    # create_ticket re-checks blacklist + limits internally; failures DM.
    channel_obj, result = await state.ticket_tool.create_ticket(guild, member, panel)
    if channel_obj is None:
        await _dm(f"Could not open your ticket: {result}")
        return
    await TicketPanelView(panel)._send_welcome_message(channel_obj, member, panel)
    try:
        await member.send(f"✅ Your ticket has been opened: {channel_obj.mention} — **{panel.get('name', 'Support')}**")
    except (discord.Forbidden, discord.HTTPException):
        pass
    logging.info(f"[ReactionPanel] {member} opened ticket {result} via reaction on message {payload.message_id}")


async def _resolve_command_style_panel(guild: discord.Guild, panel_id: Optional[str]):
    """Resolve which panel a command-style ticket (/new) uses.

    TicketTool semantics: an explicit panel wins; otherwise the guild's ONLY
    active panel is used; with multiple panels the user must specify one.
    Returns (panel, error_message).
    """
    if panel_id:
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel or panel.get('guild_id') != guild.id or not panel.get('is_active', 1):
            return None, f"Panel `{panel_id}` not found in this server (see `!panels`)."
        return panel, None
    active_panels = data_manager.load_ticket_panels_by_guild(guild.id)
    if len(active_panels) == 1:
        return active_panels[0], None
    if not active_panels:
        return None, "This server has no ticket panels yet. Ask staff to create one with `!panel`."
    listing = ', '.join(f"`{p['panel_id']}` ({p.get('name', 'Unnamed')})" for p in active_panels[:10])
    return None, f"This server has multiple panels — specify one: {listing}"
