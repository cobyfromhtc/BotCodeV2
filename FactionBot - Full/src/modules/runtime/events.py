# -*- coding: utf-8 -*-
"""Lifecycle events — on_ready, on_message, reactions, deletes, errors.

All @bot.event handlers are installed via register_events(bot)."""

# stdlib + discord.py
from packages import reactionroles as ReactionRoles
from packages import tickettool as TicketTool
from packages import faction_access as FactionAccess
import asyncio
import copy
import discord
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from discord.ext import commands
from discord.ui import Button, View
from typing import Dict, Optional, Tuple

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from core.helpers import PREMIUM_AVAILABLE, RR_AVAILABLE, brand_text
from core.ows import OWS_CATEGORIES, apply_bot_presence, get_owner_setting, ows_get
from core.process_manager import process_manager
from core.domains import INSTANCE_DOMAIN, _is_lead_instance, instance_handles
from utils.ui.embeds import EmbedBuilder
from modules.administration.channels import get_auto_purge_manager, is_verification_channel
from modules.engagement.giveaways import GiveawayView, check_giveaways_task
from modules.engagement.invites import InviteManager, check_invites_task
from modules.engagement.leveling import process_leveling
from modules.moderation.moderation import auto_ban_if_blacklisted, auto_blacklist_scan, check_temp_mutes_task, check_text_for_keywords, prune_message_cache_task, restore_temp_mutes, send_report_message
from modules.moderation.msglog import MessageLogSystem
from modules.administration.owner import OwnerSettingsView, send_owner_tutorial
from modules.moderation.sticky_roles import StickyRoleSystem
from modules.support.tickets.engine import TicketToolSystem, _reaction_panel_cache, build_multi_panel_view, check_auto_close_task, check_sla_task, handle_ticket_reaction_panel
from modules.support.tickets.views import TicketPanelView
from modules.verification.verification import WELCOME_TEMPLATES


# Once-flag: ensure the one-time portion of on_ready runs only on the first
# connect, not on every gateway reconnect. (setup_hook handles DB + caches;
# this flag guards the guild-dependent work in on_ready that can't move to
# setup_hook because it needs bot.guilds to be fully cached.)
_on_ready_initialized: bool = False

# =============================================================================
# MULTI-COMMAND CHAINING
# Allows users to run multiple commands in one message, e.g.:
#   !setupinvites, !regenerateinvites
#   !kick @user, !ban @user
#
# Splits on ", !" (comma + optional whitespace + prefix) so commas inside
# command arguments (e.g. !warn @user reason, with comma) are NOT split.
# =============================================================================
MULTI_COMMAND_SPLIT_REGEX = re.compile(
    r'\s*,\s*(?=' + re.escape(config.command_prefix) + r')'
)
MAX_CHAINED_COMMANDS = 10  # Safety cap to prevent abuse

class CommandCleanupView(View):
    """Prompt to ask staff if they want to delete the original chained command message."""
    def __init__(self, original_message: discord.Message):
        super().__init__(timeout=30.0)
        self.original_message = original_message
        self.cleanup_msg = None

    @discord.ui.button(label="Yes, delete it", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def yes_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.original_message.author.id:
            await interaction.response.send_message("This isn't your command to clean up!", ephemeral=True)
            return
        try:
            await self.original_message.delete()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        # Instead of leaving a "deleted" status message behind, just delete
        # the cleanup prompt so the chat stays clean.
        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            # Fallback: if we can't delete it, at least clear the buttons.
            try:
                await interaction.response.edit_message(embed=None, view=None)
            except Exception:
                pass
        self.stop()

    @discord.ui.button(label="No, keep it", style=discord.ButtonStyle.secondary, emoji="✋")
    async def no_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.original_message.author.id:
            await interaction.response.send_message("This isn't your command to clean up!", ephemeral=True)
            return
        # Instead of leaving a "kept" status message behind, just delete the
        # cleanup prompt so the chat stays clean.
        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            # Fallback: if we can't delete it, at least clear the buttons.
            try:
                await interaction.response.edit_message(embed=None, view=None)
            except Exception:
                pass
        self.stop()

    async def on_timeout(self) -> None:
        # Instead of leaving an "expired" status message behind, just delete
        # the cleanup prompt so the chat stays clean.
        if self.cleanup_msg:
            try:
                await self.cleanup_msg.delete()
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
_chain_offsets: Dict[int, Tuple[int, bool]] = {}

async def process_potential_multi_command(message: discord.Message) -> None:
    content = message.content
    prefix = config.command_prefix

    if content.startswith(f"{prefix}kick ") or content.startswith(f"{prefix}ban "):
        parts_initial = content.split(" ", 1)
        if len(parts_initial) > 1:
            parts_initial[1] = parts_initial[1].replace(",", " ")
            content = " ".join(parts_initial)
            message.content = content

    if not content.startswith(prefix):
        await state.bot.process_commands(message)
        return

    parts = MULTI_COMMAND_SPLIT_REGEX.split(content)

    if len(parts) <= 1 or not ows_get("multi_command_chain"):
        await state.bot.process_commands(message)
        return

    if len(parts) > MAX_CHAINED_COMMANDS:
        try:
            await message.reply(
                f"⚠️ Too many commands chained (max {MAX_CHAINED_COMMANDS}). "
                f"Please split them into separate messages.",
                delete_after=10,
            )
        except discord.HTTPException:
            pass
        return

    normalized_content = content.strip()
    if ' -del' in normalized_content and not normalized_content.lower().endswith('-del'):
        try:
            await message.reply(
                "⚠️ **Invalid Command Format**\n"
                "You cannot use `-del` in the middle of a multi-command chain.\n"
                "If you want to delete the command message, please put `-del` at the **very end** of the entire chain.\n\n"
                "**Correct Format:** `!purgeall, !cmds, !cmds, !cmds, !cmds, !cmds, !setupinvites, !regenerateinvites -del`",
                delete_after=30
            )
        except discord.HTTPException:
            pass
        return

    global_del_flag = False
    if parts[-1].strip().lower().endswith('-del'):
        global_del_flag = True
        parts[-1] = parts[-1].strip()[:-4].strip()

    cmd_names = []
    for part in parts:
        part = part.strip()
        if part.endswith(','):
            part = part[:-1].strip()
        if part.startswith(prefix):
            cmd_names.append(part[len(prefix):].split(" ")[0].lower())
            
    restrict_purge = get_owner_setting("restrict_purge_chain", True)
    if restrict_purge:
        has_purge = any(name in ['purge', 'purgeall'] for name in cmd_names)
        has_restricted = any(name in ['cmds', 'setupinvites', 'regenerateinvites'] for name in cmd_names)
        
        if has_purge and has_restricted:
            try:
                await message.reply(
                    "⚠️ **Invalid Command Format**\n"
                    "You cannot chain `!purge` or `!purgeall` with `!cmds`, `!setupinvites`, or `!regenerateinvites`. \n"
                    "Please run it separately, or ask the owner to disable this restriction with `!ows`.",
                    delete_after=30
                )
            except discord.HTTPException:
                pass
            return

    chain_command_counts = {}
    is_chained = len(cmd_names) > 1
    handled_here = 0  # commands in this chain that THIS instance owns

    for idx, part in enumerate(parts, 1):
        part = part.strip()
        
        if part.endswith(','):
            part = part[:-1].strip()
            
        if not part or not part.startswith(prefix):
            continue

        msg_copy = copy.copy(message)
        msg_copy.content = part

        cmd_name = part[len(prefix):].split(" ")[0].lower()
        chain_command_counts[cmd_name] = chain_command_counts.get(cmd_name, 0) + 1
        _chain_offsets[msg_copy.id] = (chain_command_counts[cmd_name] - 1, is_chained)

        if state.bot.get_command(cmd_name) is not None:
            handled_here += 1

        try:
            await state.bot.process_commands(msg_copy)
        except Exception as exc:
            logging.error(f"[MultiCommand] Error running command {idx}/{len(parts)} ('{part}'): {exc}")
            try:
                await message.reply(f"⚠️ Command `{part}` failed: `{exc}`", delete_after=15)
            except discord.HTTPException:
                pass

    # Domain split: only an instance that actually executed at least one
    # command from this chain may post the cleanup prompt (otherwise every
    # domain bot would prompt for the same message).
    if handled_here == 0:
        return

    is_staff = False
    if message.guild:
        if message.author.guild_permissions.manage_messages or message.author.guild_permissions.administrator:
            is_staff = True

    if global_del_flag and ows_get("auto_delete_command_del"):
        try:
            await message.delete()
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass
        return

    command_message_still_exists = True
    try:
        if message.channel is not None:
            try:
                await message.channel.fetch_message(message.id)
            except (discord.NotFound, discord.HTTPException):
                command_message_still_exists = False
    except Exception:
        pass

    if is_staff and command_message_still_exists and ows_get("command_cleanup_prompt"):
        cleanup_embed = discord.Embed(
            title="🧹 Cleanup Command Message?",
            description="Do you want to delete the original command message to keep chat clean?",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        original_content_preview = message.content[:1024]
        cleanup_embed.add_field(name="Original Message", value=f"> {original_content_preview}", inline=False)
        
        view = CommandCleanupView(message)
        view.cleanup_msg = await message.channel.send(embed=cleanup_embed, view=view)

def register_events(bot: commands.Bot) -> None:
    """Install every @bot.event handler on the bot instance."""


    @bot.event
    async def on_ready() -> None:
        from modules.support.info import build_getallroles_embed  # deferred import (cycle-safe)
        from modules.support.info import GetAllRolesView  # deferred import (cycle-safe)
        global _on_ready_initialized

        print(f'Logged in as {bot.user.name}')
        print(f'Bot started at: {time.strftime("%Y-%m-%d %H:%M:%S")}')
        logging.info(f'Bot started as {bot.user.name}')

        # Re-set presence on every connect — Discord clears the bot's presence
        # on a gateway reconnect, so this must run each time on_ready fires.
        # Respects the `use_bot_status` OWS toggle: when ON, show the custom
        # "Watching <bot_status>" activity; when OFF, clear the activity.
        await apply_bot_presence()

        # -----------------------------------------------------------------------
        # RECONNECT PATH: on_ready fires again on every gateway reconnect. The
        # one-time init (DB, caches, view registration, task starts)
        # is handled by setup_hook + the _on_ready_initialized guard below. On a
        # reconnect, we only need to re-scan invites (they may have changed while
        # we were disconnected).
        # -----------------------------------------------------------------------
        if _on_ready_initialized:
            if state.invite_manager:
                try:
                    state.invite_manager.load_data()
                    logging.info("[InviteManager] Reconnect: re-scanned invite data")
                except Exception as exc:
                    logging.warning(f"[InviteManager] Reconnect rescan failed: {exc}")
            logging.info("[on_ready] Reconnect: one-time init already done (setup_hook); skipping.")
            return

        # -----------------------------------------------------------------------
        # FIRST-CONNECT PATH: everything below runs exactly once.
        # DB connect + cache loads + generic persistent views are already done in
        # setup_hook (which runs before on_ready). This block handles the
        # guild-dependent work that needs bot.guilds to be fully cached.
        # -----------------------------------------------------------------------
        _on_ready_initialized = True

        # --- FACTIONACCESS ON_READY HOOK ---
        # Classifies every command into its feature bundle, adopts the home
        # guild when no explicit setting exists, registers pre-existing
        # guilds as pending, applies per-guild identity nicknames and starts
        # the license-expiry sweeper. Must run BEFORE any command can be
        # answered in an allied guild.
        try:
            FactionAccess.wiring.on_ready_hook(bot)
        except Exception as exc:
            logging.exception(f"[on_ready] FactionAccess.on_ready_hook failed: {exc}")

        if _is_lead_instance() and ows_get("first_startup_tutorial"):
            await send_owner_tutorial(force=False)
        else:
            logging.info("[Tutorial] First-startup tutorial disabled via OWS or handled by the lead bot; skipping")

        if _is_lead_instance():
            process_manager.clear_lock_file()
            if not send_report_message.is_running():
                send_report_message.start()
        state.invite_manager = InviteManager(bot)
        state.invite_manager.load_data()

        state.ticket_tool = TicketToolSystem(data_manager, bot)
        # CRITICAL: expose the ticket system on the bot object. ~20 call sites in
        # the TicketTool premium package resolve it via getattr(bot, 'ticket_tool')
        # (escalation, flows, SLA breach checks, staff threads, transcripts,
        # command helpers, on_owner_left...). Without this assignment every one of
        # them silently degraded to "ticket system not initialized".
        bot.ticket_tool = state.ticket_tool
        logging.info("[TicketTool] Initialized ticket tool system")

        # --- PREMIUM TIER 1 ON_READY HOOK ---
        # Sets up automation-engine global refs + starts the delayed-automation /
        # SLA background timer loop + re-arms persisted delayed timers.
        # Domain split: the minute loop (timers + SLA + review deadlines) must run
        # on exactly ONE bot (the ticket bot) or actions would double-fire; the
        # global refs + review views are safe on every instance.
        if PREMIUM_AVAILABLE:
            try:
                TicketTool.wiring.on_ready_hook(
                    data_manager, bot, state.ticket_tool,
                    start_loop=instance_handles('ticket'),
                )
            except Exception as exc:
                logging.exception(f"[on_ready] TicketTool.on_ready_hook failed: {exc}")

        for guild in bot.guilds:
            guild_panels = data_manager.load_ticket_panels_by_guild(guild.id)
            for panel in guild_panels:
                view = TicketPanelView(panel)
                bot.add_view(view)
        logging.info(f"[TicketTool] Registered views for panels")

        # Warm the reaction-panel lookup cache (TicketTool reaction panels).
        for guild in bot.guilds:
            try:
                for row in data_manager.load_reaction_panels_by_guild(guild.id):
                    try:
                        mapping = json.loads(row.get('mapping') or '{}')
                    except (ValueError, TypeError):
                        continue
                    if isinstance(mapping, dict) and mapping:
                        _reaction_panel_cache[row['message_id']] = mapping
            except Exception as exc:
                logging.warning(f"[ReactionPanel] cache warm failed in {guild.name}: {exc}")
        if _reaction_panel_cache:
            logging.info(f"[TicketTool] Warmed reaction-panel cache ({len(_reaction_panel_cache)} message(s))")

        # Re-register the persistent views for every stored multi-panel message
        # (TicketTool Attached Panels / Dropdown Style). Views are rebuilt from
        # the CURRENT panel rows so button labels / select options are fresh.
        for guild in bot.guilds:
            try:
                multi_rows = data_manager.load_multi_panels_by_guild(guild.id)
            except Exception as exc:
                logging.warning(f"[TicketTool] multi-panel load failed in {guild.name}: {exc}")
                continue
            for row in multi_rows:
                try:
                    panel_ids = json.loads(row.get('panel_ids') or '[]')
                    panels = [p for p in (data_manager.load_ticket_panel(pid) for pid in panel_ids) if p and p.get('is_active', 1)]
                    if panels:
                        bot.add_view(build_multi_panel_view(row, panels))
                    else:
                        logging.warning(f"[TicketTool] multi-panel message {row.get('message_id')} has no active panels; skipping view registration")
                except Exception as exc:
                    logging.warning(f"[TicketTool] multi-panel view registration failed for {row.get('message_id')}: {exc}")
        logging.info("[TicketTool] Registered views for multi-panels")

        # Re-register GiveawayView for every ACTIVE giveaway so the "Enter
        # Giveaway" button keeps working after a restart. Without this, a
        # giveaway created before the restart would display correctly but its
        # button click would silently no-op (no view handler attached).
        # GiveawayView uses timeout=None (persistent), so add_view is correct.
        if instance_handles('utility') and ows_get("enable_giveaways"):
            restored_giveaways = 0
            for giveaway_id, giveaway in state.giveaways_data.items():
                try:
                    if giveaway.get('status') == 'active':
                        bot.add_view(GiveawayView(giveaway_id))
                        restored_giveaways += 1
                except Exception as e:
                    logging.warning(f"[Giveaways] Could not re-register view for {giveaway_id}: {e}")
            if restored_giveaways:
                logging.info(f"[Giveaways] Re-registered persistent views for {restored_giveaways} active giveaway(s)")
        else:
            logging.info("[Giveaways] Giveaway system disabled via OWS or another bot's domain; skipping view restoration")

        if instance_handles('ticket') and ows_get("startup_orphan_cleanup"):
            for guild in bot.guilds:
                cleaned = 0
                for status in ('open', 'pending', 'closing'):
                    tickets = data_manager.load_tickets_by_guild(guild.id, status)
                    for ticket in tickets:
                        channel_id = ticket.get('channel_id')
                        if channel_id is None or not guild.get_channel(channel_id):
                            ticket['status'] = 'closed'
                            ticket['close_reason'] = ticket.get('close_reason') or f'Reconciled on startup (was {status})'
                            if not ticket.get('closed_at'):
                                ticket['closed_at'] = datetime.now(timezone.utc).isoformat()
                            data_manager.save_ticket(ticket)
                            cleaned += 1
                if cleaned:
                    logging.info(f"[TicketTool] Reconciled {cleaned} interrupted/orphaned ticket(s) in {guild.name}")
        else:
            logging.info("[TicketTool] Startup orphan cleanup disabled via OWS or another bot's domain; skipping")

        if instance_handles('utility') and not check_invites_task.is_running():
            check_invites_task.start()
            logging.info("[InviteManager] Started invite checking task")

        gar_refreshed = 0
        gar_pruned = 0
        if instance_handles('utility'):
            for guild in bot.guilds:
                tracked = data_manager.load_getallroles_messages(guild_id=guild.id)
                for row in tracked:
                    channel = guild.get_channel(row['channel_id'])
                    if channel is None:
                        data_manager.delete_getallroles_message(row['message_id'])
                        gar_pruned += 1
                        continue
                    try:
                        message = await channel.fetch_message(row['message_id'])
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        data_manager.delete_getallroles_message(row['message_id'])
                        gar_pruned += 1
                        continue
                    try:
                        embed = build_getallroles_embed(guild)
                        await message.edit(embed=embed, view=GetAllRolesView())
                        gar_refreshed += 1
                    except (discord.HTTPException, discord.Forbidden) as e:
                        logging.warning(f"[GetAllRoles] Could not refresh message {row['message_id']} on startup: {e}")
            if gar_refreshed or gar_pruned:
                logging.info(f"[GetAllRoles] Startup refresh: {gar_refreshed} refreshed, {gar_pruned} pruned")

        if instance_handles('mod') and not auto_blacklist_scan.is_running():
            auto_blacklist_scan.start()
        logging.info("[Blacklist] Auto-scan task assigned to the moderation bot" if not instance_handles('mod') else "[Blacklist] Started scheduled auto-scan task")

        if instance_handles('utility') and config.enable_giveaways and not check_giveaways_task.is_running():
            check_giveaways_task.start()
            logging.info("[Giveaways] Started giveaway check task")

        if instance_handles('ticket') and not check_sla_task.is_running():
            check_sla_task.start()
            logging.info("[SLA] Started SLA check task")

        if instance_handles('ticket') and ows_get("auto_close_tickets") and not check_auto_close_task.is_running():
            check_auto_close_task.start()
            logging.info("[AutoClose] Started idle-ticket auto-close task")

        if instance_handles('mod'):
            await restore_temp_mutes()
            if not check_temp_mutes_task.is_running():
                check_temp_mutes_task.start()
                logging.info("[TempMute] Started temp-mute background check task")

        try:
            if instance_handles('mod'):
                pruned = data_manager.prune_message_cache(keep_recent=5000)
                if pruned:
                    logging.info(f"[MsgLog] Startup prune removed {pruned} cached message(s)")
                if not prune_message_cache_task.is_running():
                    prune_message_cache_task.start()
                    logging.info("[MsgLog] Started message-cache prune task")
        except Exception as exc:
            logging.warning(f"[MsgLog] Could not start prune task: {exc}")

        try:
            for g in bot.guilds:
                # RR count now comes from the extracted ReactionRolesDB accessor
                # (bot.reaction_roles_db). Falls back to 0 if the package isn't
                # loaded yet.
                _rr_db = getattr(bot, 'reaction_roles_db', None)
                rr_count = _rr_db.count_reaction_roles(g.id) if _rr_db else 0
                sticky_on = StickyRoleSystem.is_enabled(g.id)
                ml_cfg = MessageLogSystem.get_config(g.id)
                branding = data_manager.get_branding(g.id)
                logging.info(
                    f"[Premium] {g.name}: RR={rr_count}/250 sticky={'on' if sticky_on else 'off'} "
                    f"msglog={'on' if ml_cfg.get('enabled') else 'off'} "
                    f"branding_footer={'set' if branding.get('embed_footer') else 'default'}"
                )
        except Exception as exc:
            logging.debug(f"[Premium] startup summary failed: {exc}")

        # =========================================================================
        # RESTORE ACTIVE OWS PANEL (so the owner doesn't have to type !ows again)
        # =========================================================================
        if not instance_handles('utility'):
            return
        ows_state = data_manager.load_ows_panel_state()
        if ows_state:
            try:
                channel = bot.get_channel(ows_state['channel_id'])
                if channel:
                    message = await channel.fetch_message(ows_state['message_id'])
                    view = OwnerSettingsView(ows_state['owner_id'])
                    view.current_category = ows_state.get('current_category', OWS_CATEGORIES[0])
                    view._build_components()
                    view.message = message
                    await message.edit(view=view)
                    logging.info(f"[OWS] Restored active owner settings panel from previous session.")
            except discord.NotFound:
                data_manager.delete_ows_panel_state()
                logging.info("[OWS] Active panel message was deleted, cleared panel state.")
            except Exception as exc:
                logging.warning(f"[OWS] Could not restore active panel: {exc}")

    @bot.event
    async def on_member_join(member: discord.Member) -> None:
        # --- FACTIONACCESS AUTOMATION SCOPE ---
        # Automated join behaviors (blacklist auto-ban, welcome message,
        # sticky-role restore) are bound to the home faction's global
        # channel/role config, so they stay HOME-GUILD-ONLY in the
        # multi-guild model. Allied factions get the systems they were
        # granted via commands (which carry their own per-guild config),
        # never unsolicited home-config automation.
        _fa = getattr(state, 'faction_access', None)
        if _fa is not None and not _fa.is_home(member.guild.id):
            return

        # Domain split: moderation bots handle the blacklist auto-ban; utility
        # bots handle welcome messages + sticky roles. (Full bot: both.)
        if instance_handles('mod'):
            try:
                banned = await auto_ban_if_blacklisted(member, source="member_join")
                if banned:
                    return
            except Exception as e:
                logging.error(f"Error in blacklist join check: {str(e)}")

        if instance_handles('utility'):
            try:
                if ows_get("welcome_messages"):
                    channel = bot.get_channel(config.channels.welcome)
                    welcome_message = brand_text(random.choice(WELCOME_TEMPLATES)).format(mention=member.mention, server=member.guild.name)
                
                    embed = discord.Embed(title=f"Welcome to {member.guild.name}!", description=welcome_message, color=discord.Color.green())
                    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
                    embed.add_field(name="Member Count", value=member.guild.member_count)
                    embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d"))
                
                    rules_channel = bot.get_channel(config.channels.rules)
                    if rules_channel:
                        # Per-guild gang identity: the home faction may carry an
                        # identity override too, so resolve through the service
                        # when it is available (falls back to the global config).
                        gang_display = config.gang_name
                        try:
                            if _fa is not None:
                                gang_display = _fa.gang_name_for(member.guild.id) or gang_display
                        except Exception:
                            pass
                        embed.add_field(name="Server Rules", value=f"Make sure to follow {gang_display}'s rules {rules_channel.mention}", inline=False)
                
                    embed.set_footer(text=f"Joined on {member.joined_at.strftime('%Y-%m-%d')}")
                
                    welcome_msg = await channel.send(embed=embed)
                    await welcome_msg.add_reaction('🔥')
                    await welcome_msg.add_reaction('👋')
                    await welcome_msg.add_reaction('💯')
                
                    logging.info(f"Sent welcome message for {member}")
            except Exception as e:
                logging.error(f"Error in welcome system: {str(e)}")

        # --- Sticky Roles: re-apply saved roles on rejoin (Dyno premium clone) ---
        if instance_handles('utility'):
            try:
                restored = await StickyRoleSystem.restore_member_roles(member)
                if restored:
                    logging.info(f"[Sticky] Restored {restored} role(s) to returning member {member}")
            except Exception as exc:
                logging.exception(f"[Sticky] restore on join failed: {exc}")


    @bot.event
    async def on_presence_update(before: discord.Member, after: discord.Member) -> None:
        if not instance_handles('mod'):
            return
        if after.bot:
            return
        # --- FACTIONACCESS AUTOMATION SCOPE ---
        # The blacklist presence scan uses the home faction's global keyword
        # list — it never extends into allied guilds.
        _fa = getattr(state, 'faction_access', None)
        if _fa is not None and not _fa.is_home(after.guild.id):
            return
        if not state.blacklisted_keywords:
            return
    
        before_activities = set(str(a) for a in before.activities)
        after_activities = set(str(a) for a in after.activities)
    
        if before_activities == after_activities:
            return
    
        await auto_ban_if_blacklisted(after, source="presence_update")

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        # --- FACTIONACCESS AUTOMATION SCOPE ---
        # Non-command automations must respect multi-guild licensing:
        #   * home-guild-only automations (blacklist scans, message-log
        #     caching, premium custom-command dispatch) — they read the
        #     home faction's global config;
        #   * bundle-following automations (leveling XP) — the data is
        #     per-guild, so they run wherever the bundle is granted.
        # A missing service (pre-setup_hook) keeps the legacy behavior.
        _fa = getattr(state, 'faction_access', None)
        _fa_home = (_fa is None or message.guild is None
                    or _fa.automation_allowed(message.guild.id, None))
        _fa_leveling = (_fa is None or message.guild is None
                        or _fa.automation_allowed(message.guild.id, 'leveling'))
        _fa_tickets = (_fa is None or message.guild is None
                       or _fa.automation_allowed(message.guild.id, 'tickets'))

        # --- VERIFICATION CHANNEL AUTO-PURGE HOOK ---
        # Any human message in the verification channel counts as activity: it
        # cancels any in-progress purge countdown (during the 2-minute warning
        # window) and re-arms a fresh 3-minute idle watcher. This is what makes
        # "if it becomes active again during those 2 minutes, cancel the purge"
        # work, AND what triggers the initial idle check after the first message.
        # (Domain split: verification belongs to the moderation bot.)
        try:
            if instance_handles('mod') and message.guild is not None and is_verification_channel(message.channel.id, message.channel):
                mgr = get_auto_purge_manager(message.channel.id)
                asyncio.create_task(mgr.record_activity())
        except Exception as exc:
            logging.debug(f"[AutoPurge] record_activity failed: {exc}")

        open_ticket = None
        any_ticket = None
        if message.guild and state.ticket_tool:
            try:
                ticket = data_manager.load_ticket_by_channel(message.channel.id)
                if ticket:
                    any_ticket = ticket
                    if ticket.get('status') == 'open':
                        open_ticket = ticket
            except Exception as e:
                logging.warning(f"[on_message] Ticket lookup failed: {e}")

        # Blacklist message scanning. Ticket channels are exempt UNLESS the OWS
        # `blacklist_in_tickets` toggle is enabled (previously the toggle existed
        # but was never read and ticket channels were unconditionally exempt).
        # The exemption now covers ANY ticket channel — including two-step closed
        # channels, where staff would otherwise be auto-banned for discussing a
        # blacklisted keyword.
        _ticket_exempt = any_ticket is not None and not ows_get("blacklist_in_tickets")
        if (instance_handles('mod') and message.guild and state.blacklisted_keywords
                and not _ticket_exempt and ows_get("auto_ban_message") and _fa_home):
            content = message.content or ""
            if not content.startswith(config.command_prefix):
                found, keyword = check_text_for_keywords(content)
                if found:
                    author = message.author
                    try:
                        await message.delete()
                    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                        pass

                    log_channel = bot.get_channel(config.channels.log)
                    snippet = content if len(content) <= 900 else (content[:900] + "...")
                    if log_channel:
                        try:
                            embed = discord.Embed(
                                title="🚨 Blacklisted Keyword in Message",
                                description=(
                                    f"**User:** {author.mention} (`{author.name}` / `{author.id}`)\n"
                                    f"**Channel:** {message.channel.mention}\n"
                                    f"**Matched Keyword:** `{keyword}`"
                                ),
                                color=discord.Color.red(),
                                timestamp=datetime.now(timezone.utc),
                            )
                            embed.add_field(name="Message Content", value=snippet, inline=False)
                            await log_channel.send(embed=embed)
                        except Exception as e:
                            logging.warning(f"[Blacklist] Could not send message blacklist log: {e}")

                    logging.info(
                        f"[Blacklist] Deleted message from {author} (ID: {author.id}) "
                        f"in #{getattr(message.channel, 'name', '?')} containing '{keyword}'"
                    )

                    if not ows_get("blacklist_alert_only"):
                        if isinstance(author, discord.Member):
                            try:
                                await author.ban(
                                    reason=f"Auto-banned: Blacklisted keyword '{keyword}' in message",
                                )
                            except discord.Forbidden:
                                logging.warning(f"[Blacklist] No permission to ban {author}.")
                            except discord.HTTPException as e:
                                logging.error(f"[Blacklist] HTTP error banning {author}: {e}")
                    return

        if instance_handles('utility') and _fa_leveling:
            await process_leveling(message)

        try:
            if instance_handles('mod') and message.guild is not None and _fa_home:
                MessageLogSystem.cache(message)
        except Exception as exc:
            logging.debug(f"[MsgLog] on_message cache failed: {exc}")

        if instance_handles('ticket') and open_ticket is not None:
            is_staff_msg = message.author.id != open_ticket.get('creator_id')
            if is_staff_msg and ows_get("require_claim_before_reply"):
                # Exempt command messages (e.g. !claim, !close, !unclaim) so staff
                # can still MANAGE the ticket — the gate applies to conversational
                # replies only, matching the toggle's "Staff must claim before
                # responding" description.
                is_command = (message.content or '').startswith(config.command_prefix)
                if not is_command:
                    claimed_by = open_ticket.get('claimed_by')
                    if not claimed_by:
                        # Ticket is not claimed — staff must claim before replying.
                        try:
                            await message.delete()
                        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                            pass
                        try:
                            await message.channel.send(
                                f"{message.author.mention} You must claim this ticket before replying. "
                                f"Click the **Claim** button (or use `{config.command_prefix}claim`).",
                                delete_after=15,
                            )
                        except (discord.HTTPException, discord.Forbidden):
                            pass
                        return
                    if claimed_by != message.author.id:
                        # Claimed by another staff member — only the claimer may reply.
                        try:
                            await message.delete()
                        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                            pass
                        try:
                            await message.channel.send(
                                f"{message.author.mention} This ticket is claimed by <@{claimed_by}>. "
                                f"Only the claimer may reply. Ask them to `{config.command_prefix}unclaim` for a hand-off.",
                                delete_after=15,
                            )
                        except (discord.HTTPException, discord.Forbidden):
                            pass
                        return
            if is_staff_msg:
                # Only the FIRST staff reply needs a write. The ticket dict in
                # hand already tells us whether first_response_at is set, so we
                # skip the write entirely for every subsequent message. The
                # UPDATE itself stays conditional (WHERE ... IS NULL) as a
                # second line of defense, and the sync SQLite call is offloaded
                # to a worker thread so the gateway handler never blocks.
                if not open_ticket.get('first_response_at'):
                    asyncio.create_task(asyncio.to_thread(
                        data_manager.update_ticket_first_response, open_ticket['ticket_id']
                    ))

                # --- PREMIUM TIER 1: on_ticket_message hook ---
                # Records the first staff response in the SLA state row, sets
                # staff_responded_at on the ticket, and cancels any pending
                # 'no_response' automation timer for this ticket.
                if PREMIUM_AVAILABLE:
                    try:
                        await TicketTool.wiring.on_ticket_message(
                            bot=bot, ticket_tool=state.ticket_tool,
                            message=message, ticket=open_ticket, is_staff=is_staff_msg,
                        )
                    except Exception as exc:
                        logging.debug(f"[Premium] on_ticket_message failed: {exc}")

            # Persist this message to the ticket_messages table so it serves as a
            # transcript BACKUP. The transcript generator still reads Discord
            # channel history as the primary source, but falls back to this table
            # if the channel history is empty/unavailable (e.g. messages were
            # bulk-deleted, or the channel was partially lost before close).
            try:
                payload = {
                    'message_id': message.id,
                    'ticket_id': open_ticket['ticket_id'],
                    'author_id': message.author.id,
                    'author_name': message.author.display_name,
                    'author_avatar': str(message.author.avatar.url) if message.author.avatar else str(message.author.default_avatar.url),
                    'content': message.content or '',
                    'attachments': json.dumps([att.url for att in message.attachments]),
                    'created_at': message.created_at.isoformat() if message.created_at else datetime.now(timezone.utc).isoformat(),
                }
                # Fire-and-forget: `async_save_ticket_message` runs the write in
                # a worker thread, but awaiting it here would serialise every
                # ticket message behind the previous write. Scheduling it as a
                # task lets multiple writes pipeline (matches the Short variant
                # and keeps the gateway handler off the disk path).
                asyncio.create_task(data_manager.async_save_ticket_message(payload))
            except Exception as exc:
                logging.debug(f"[TicketMsg] could not schedule persistence for {message.id}: {exc}")

        # --- PREMIUM TIER 2: custom command prefix dispatch ---
        # If the message is a !-prefixed command that isn't a built-in, check if
        # it's a custom command. If it ran, we're done (skip multi-command parsing).
        if instance_handles('ticket') and PREMIUM_AVAILABLE and message.guild and message.content and _fa_tickets:
            try:
                content = message.content.strip()
                if content.startswith(config.command_prefix):
                    # Extract the command name (first token after the prefix).
                    rest = content[len(config.command_prefix):]
                    if rest:
                        cmd_name = rest.split()[0].lower()
                        # Don't shadow built-in hybrid commands — let discord.py
                        # handle those. Only intercept if it's NOT a known command.
                        if not bot.get_command(cmd_name):
                            ran = await TicketTool.wiring.on_prefix_command(
                                bot=bot, message=message, command_name=cmd_name,
                            )
                            if ran:
                                return  # custom command handled it
            except Exception as exc:
                logging.debug(f"[Premium] custom command dispatch failed: {exc}")

        await process_potential_multi_command(message)


    # =========================================================================
    # EVENT HANDLERS for the four premium features
    # =========================================================================

    @bot.event
    async def on_raw_reaction_add(payload: "discord.RawReactionActionEvent") -> None:
        # Ticket reaction panels first (Ticket Tool reaction-based panels).
        if instance_handles('ticket'):
            try:
                await handle_ticket_reaction_panel(payload)
            except Exception as exc:
                logging.exception(f"[ReactionPanel] on_raw_reaction_add error: {exc}")

        # Delegate to the extracted ReactionRoles package. The wiring hook is
        # fail-safe (it catches + logs internally); the outer guard mirrors the
        # pre-extraction behavior exactly.
        if RR_AVAILABLE and instance_handles('utility'):
            try:
                await ReactionRoles.wiring.on_raw_reaction_add(payload, bot)
            except Exception as exc:
                logging.exception(f"[RR] on_raw_reaction_add error: {exc}")


    @bot.event
    async def on_raw_reaction_remove(payload: "discord.RawReactionActionEvent") -> None:
        if RR_AVAILABLE and instance_handles('utility'):
            try:
                await ReactionRoles.wiring.on_raw_reaction_remove(payload, bot)
            except Exception as exc:
                logging.exception(f"[RR] on_raw_reaction_remove error: {exc}")


    @bot.event
    async def on_raw_reaction_clear(payload: "discord.RawReactionClearEvent") -> None:
        """When all reactions are cleared from a message, drop its RR mappings."""
        if RR_AVAILABLE and instance_handles('utility'):
            try:
                await ReactionRoles.wiring.on_raw_reaction_clear(payload, bot)
            except Exception as exc:
                logging.exception(f"[RR] on_raw_reaction_clear error: {exc}")


    @bot.event
    async def on_message_delete(message: discord.Message) -> None:
        if not instance_handles('mod'):
            return
        try:
            # Drop any reaction-role mappings tied to the deleted message.
            if RR_AVAILABLE:
                try:
                    await ReactionRoles.wiring.on_message_delete(message.id, bot)
                except Exception as exc:
                    logging.exception(f"[RR] on_message_delete error: {exc}")
            # Full message logging.
            await MessageLogSystem.log_delete(message)
        except Exception as exc:
            logging.exception(f"[MsgLog] on_message_delete error: {exc}")


    @bot.event
    async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
        if not instance_handles('mod'):
            return
        try:
            await MessageLogSystem.log_edit(before, after)
        except Exception as exc:
            logging.exception(f"[MsgLog] on_message_edit error: {exc}")


    @bot.event
    async def on_member_remove(member: discord.Member) -> None:
        if instance_handles('utility'):
            try:
                StickyRoleSystem.capture_member_roles(member)
            except Exception as exc:
                logging.exception(f"[Sticky] on_member_remove error: {exc}")

        # --- PREMIUM TIER 1: fire 'owner_left' automations ---
        # For every open ticket owned by the leaving member, fire the automation
        # engine's owner_left trigger (which can auto-close, notify staff, etc.).
        if instance_handles('ticket') and PREMIUM_AVAILABLE and member.guild and getattr(bot, 'ticket_tool', None):
            try:
                pdb = getattr(bot, 'premium_db', None)
                if pdb is not None:
                    open_tickets = data_manager.load_tickets_by_creator(member.id, member.guild.id)
                    for t in open_tickets:
                        panel = data_manager.load_ticket_panel(t.get('panel_id') or '') if t.get('panel_id') else None
                        await TicketTool.wiring.on_owner_left(
                            bot=bot, ticket_tool=state.ticket_tool, ticket=t,
                            panel=panel, guild=member.guild,
                        )
            except Exception as exc:
                logging.warning(f"[Premium] on_member_remove owner_left failed: {exc}")


    # --- ERROR HANDLING ---
    @bot.event
    async def on_command_error(ctx: commands.Context, error: Exception) -> None:
        # Domain-split bots: a command this instance doesn't own is another bot's
        # job — ignore it silently instead of erroring in the channel.
        if INSTANCE_DOMAIN != 'full' and isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CheckFailure):
            # Permission checks already produce friendly messages below; generic
            # CheckFailure (e.g. guild-only commands used in DMs) stays quiet.
            if isinstance(error, commands.MissingPermissions):
                await ctx.send("You don't have permission to use this command.")
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You don't have permission to use this command.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Missing required argument.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("Invalid argument provided.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Cooldown. Try again in {error.retry_after:.0f} seconds.")
        elif isinstance(error, commands.CommandNotFound):
            # Full-bot mode: unknown commands stay quiet (typos shouldn't spam).
            return
        else:
            logging.error(f'Error: {str(error)}')
            await ctx.send(f"An error occurred: {str(error)}")





    # --- GETALLROLES AUTO-UPDATE EVENTS ---
    # Whenever a role is created, deleted, or its name changes, refresh every
    # active getallroles embed in that guild so the lists stay live without the
    # owner having to re-run the command.
    @bot.event
    async def on_guild_role_create(role: discord.Role) -> None:
        # Avoid running during startup before data_manager is ready.
        from modules.support.info import refresh_getallroles_messages  # deferred import (cycle-safe)
        if 'data_manager' not in globals() or data_manager is None:
            return
        try:
            await refresh_getallroles_messages(role.guild)
        except Exception as e:
            logging.warning(f"[GetAllRoles] on_guild_role_create refresh failed: {e}")


    @bot.event
    async def on_guild_role_delete(role: discord.Role) -> None:
        from modules.support.info import refresh_getallroles_messages  # deferred import (cycle-safe)
        if 'data_manager' not in globals() or data_manager is None:
            return
        try:
            await refresh_getallroles_messages(role.guild)
        except Exception as e:
            logging.warning(f"[GetAllRoles] on_guild_role_delete refresh failed: {e}")


    @bot.event
    async def on_guild_role_update(before: discord.Role, after: discord.Role) -> None:
        # Only refresh if something visible to the embed changed (name or
        # position). Other updates (permissions, color) don't affect the list.
        from modules.support.info import refresh_getallroles_messages  # deferred import (cycle-safe)
        if before.name != after.name or before.position != after.position:
            if 'data_manager' not in globals() or data_manager is None:
                return
            try:
                await refresh_getallroles_messages(after.guild)
            except Exception as e:
                logging.warning(f"[GetAllRoles] on_guild_role_update refresh failed: {e}")
