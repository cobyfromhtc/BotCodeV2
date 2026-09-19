# -*- coding: utf-8 -*-
"""Ticket commands — panel management + ticket lifecycle."""

# stdlib + discord.py
from packages import tickettool as TicketTool
import discord
import json
import logging
from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import commands
from typing import Dict, List, Optional, Tuple

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from core.helpers import PREMIUM_AVAILABLE
from core.ows import ows_get
from modules.support.tickets.engine import TICKET_LOG_EVENT_INFO, _build_panel_message_embeds, _reaction_panel_cache, _resolve_command_style_panel, _resolve_ticket_category_for_display, _ticket_automation_paused, _ticket_category_label, build_multi_panel_view, get_ticket_log_events, log_ticket_event, parse_pause_duration, reopen_ticket_in_place
from modules.support.tickets.views import CloseRequestView, ConfirmCloseView, ManualRatingView, MultiPanelView, PRIORITY_COLORS, PRIORITY_EMOJIS, PanelCreatorView, TicketCategoryManagerView, TicketCategorySelectView, TicketControlView, TicketPanelSelectView, TicketPanelView, TicketQuestionsModal, TicketRatingView, TicketSettingsConfigView, _build_ticket_categories_embed




def _format_age(iso_raw: Optional[str]) -> str:
    """Human-readable age of an ISO timestamp ('2d 3h', '45m', …)."""
    if not iso_raw:
        return "—"
    try:
        then = datetime.fromisoformat(str(iso_raw).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return "—"
    seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _build_multi_panel_embeds(panels: List[Dict]) -> List[discord.Embed]:
    """Build the embed(s) shown on a multi-panel message: a header embed
    listing every attached panel with its description."""
    lines = []
    for panel in panels[:25]:
        name = panel.get('name', 'Panel')
        desc = (panel.get('embed_description') or '').strip()
        emoji = panel.get('button_emoji') or '🎫'
        line = f"{emoji} **{name}**"
        if desc:
            line += f"\n> {desc[:200]}"
        lines.append(line)
    embed = discord.Embed(
        title="🎫 Support Tickets",
        description=(
            "Select the type of ticket you need below.\n\n" + "\n\n".join(lines)
        )[:4000],
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"{len(panels)} ticket type(s) available")
    return [embed]


def _parse_panel_id_list(raw: str, guild_id: int) -> Tuple[List[Dict], Optional[str]]:
    """Parse a comma-separated panel-ID list into panel rows.

    Returns (panels, error). Error is None when every ID resolved to an
    active panel in this guild.
    """
    ids = [token.strip() for token in (raw or '').split(',') if token.strip()]
    if len(ids) < 2:
        return [], "Provide at least 2 panel IDs, comma-separated (see `!panels`)."
    if len(ids) > 25:
        return [], "Discord allows at most 25 panels per multi-panel message."
    panels = []
    missing = []
    for pid in ids:
        panel = data_manager.load_ticket_panel(pid)
        if not panel or panel.get('guild_id') != guild_id or not panel.get('is_active', 1):
            missing.append(pid)
        else:
            panels.append(panel)
    if missing:
        return [], f"Panel(s) not found in this server: {', '.join(f'`{m}`' for m in missing)}"
    return panels, None


# =============================================================================
# --- NEW TICKET TOOL FEATURE COMMANDS ---
# =============================================================================

async def _ticket_respond(ctx: commands.Context, content: Optional[str] = None, *,
                          embed: Optional[discord.Embed] = None, ephemeral: bool = False) -> None:
    """Send a message that behaves correctly for prefix (!cmd) invocations.

    Regular (prefix) messages cannot be ephemeral; older discord.py 2.x
    versions raise `TypeError` on `ephemeral=` when the context is not
    interaction-backed.

    The interaction-backed branch below is a safe guard (kept from the
    hybrid-command era): all commands are prefix-only now, so `ephemeral=`
    is ignored and a normal message is sent.
    """
    if ephemeral and ctx.interaction is not None:
        await ctx.send(content, embed=embed, ephemeral=True)
    else:
        await ctx.send(content, embed=embed)

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    # =============================================================================
    # --- TICKET TOOL COMMANDS (Full Ticket Tool Clone) ---
    @bot.command(name="panel", description="Create a new ticket panel")
    @commands.has_permissions(manage_channels=True)
    async def create_panel(ctx: commands.Context) -> None:
        """Open the interactive panel creator."""
        view = PanelCreatorView(ctx.guild.id, ctx.author.id)
        embed = discord.Embed(
            title="Panel Creator",
            description="Use the buttons below to configure your ticket panel.\n\n"
                        "**Steps:**\n"
                        "1. Set Name - Give your panel a name\n"
                        "2. Set Embed - Customize the embed appearance\n"
                        "3. Set Button - Customize the create button\n"
                        "4. Settings - Configure ticket limits and more\n"
                        "5. Ticket Category - Folder tickets from this panel\n"
                        "6. Preview - See how it will look\n"
                        "7. Create Panel - Send the panel to this channel",
            color=discord.Color.blurple()
        )
        await ctx.send(embed=embed, view=view)


    @bot.command(name="tcategory", description="Manage ticket categories (internal ticket folders)")
    @commands.has_permissions(manage_channels=True)
    async def ticket_category_cmd(ctx: commands.Context) -> None:
        """Open the interactive Ticket Category manager.

    Ticket Categories are internal folders that group related tickets
    (e.g. "Staff" containing "Apply for Staff" + "Staff Training"). They are
    NOT Discord channel categories — tickets in the same folder can still
    live in the same Discord channel category.
    """
        view = TicketCategoryManagerView(ctx.guild.id, ctx.author.id)
        embed = _build_ticket_categories_embed(ctx.guild.id, ctx.guild.name)
        message = await ctx.send(embed=embed, view=view)
        view.message = message


    @bot.command(name="setcategory", description="Change the ticket category of this ticket (staff)")
    @commands.has_permissions(manage_channels=True)
    async def set_ticket_category_cmd(ctx: commands.Context) -> None:
        """Change this ticket's Ticket Category (internal folder).

    Works on any ticket — including ones created before Ticket Categories
    existed (they start as Uncategorized). The ticket's channel, claim,
    transcript and all other data are untouched; only the category
    association changes. Also available via the 📁 Category button inside
    the ticket.
    """
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await ctx.send("This is not a ticket channel.")
            return
        categories = data_manager.load_ticket_categories(ctx.guild.id)
        if not categories:
            await ctx.send("No ticket categories exist yet — create one with `!tcategory` first.")
            return
        current = _ticket_category_label(ctx.guild.id, ticket.get('ticket_category_id'))
        await ctx.send(
            f"This ticket is currently in **{current}**. Select a new category "
            f"(or remove it):",
            view=TicketCategorySelectView(ticket['ticket_id'], ctx.guild.id),
            ephemeral=True,
        )


    @bot.command(name="panels", description="List all ticket panels")
    @commands.has_permissions(manage_channels=True)
    async def list_panels(ctx: commands.Context) -> None:
        """List all ticket panels in this server."""
        panels = data_manager.load_ticket_panels_by_guild(ctx.guild.id)
    
        if not panels:
            await ctx.send("No ticket panels found. Use `!panel` to create one.")
            return
    
        embed = discord.Embed(
            title="Ticket Panels",
            description=f"Found **{len(panels)}** panel(s) in this server:",
            color=discord.Color.blurple()
        )
    
        for panel in panels[:10]:
            channel = ctx.guild.get_channel(panel.get('channel_id'))
            channel_name = channel.mention if channel else "Unknown"
            embed.add_field(
                name=f"{panel.get('name', 'Unnamed')} (ID: {panel['panel_id']})",
                value=f"Channel: {channel_name}\nButton: {panel.get('button_label', 'Create Ticket')}",
                inline=False
            )
    
        await ctx.send(embed=embed)


    @bot.command(name="deletepanel", description="Delete a ticket panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="The panel ID to delete")
    async def delete_panel(ctx: commands.Context, panel_id: str) -> None:
        """Delete a ticket panel."""
        panel = data_manager.load_ticket_panel(panel_id)
    
        if not panel or panel['guild_id'] != ctx.guild.id:
            await ctx.send("Panel not found in this server.")
            return
    
        if panel.get('channel_id') and panel.get('message_id'):
            try:
                channel = ctx.guild.get_channel(panel['channel_id'])
                if channel:
                    message = await channel.fetch_message(panel['message_id'])
                    await message.delete()
            except:
                pass
    
        data_manager.delete_ticket_panel(panel_id)
        await ctx.send(f"Panel `{panel_id}` has been deleted.")


    @bot.command(name="claim", description="Claim the current ticket")
    async def claim_ticket_cmd(ctx: commands.Context) -> None:
        """Claim a ticket."""
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
    
        success, message = await state.ticket_tool.claim_ticket(ctx.channel, ctx.author)
    
        if success:
            await ctx.send(embed=discord.Embed(title="Ticket Claimed", description=message, color=discord.Color.green()))
        else:
            await ctx.send(message)


    @bot.command(name="unclaim", description="Release your claim on this ticket")
    async def unclaim_ticket_cmd(ctx: commands.Context) -> None:
        """Release a ticket claim."""
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
    
        success, message = await state.ticket_tool.unclaim_ticket(ctx.channel, ctx.author)
    
        if success:
            await ctx.send(embed=discord.Embed(title="Ticket Unclaimed", description=message, color=discord.Color.orange()))
        else:
            await ctx.send(message)


    @bot.command(name="close", description="Close the current ticket")
    @app_commands.describe(reason="Reason for closing")
    async def close_ticket_cmd(ctx: commands.Context, *, reason: str = "No reason provided") -> None:
        """Close a ticket with optional reason.

    Mirrors the Close-button flow: the ticket creator gets the star-rating
    prompt (when enabled via OWS), everyone else gets a simple confirmation.
    Previously this command ALWAYS showed the rating view to anyone — ignoring
    the ticket_rating_prompt toggle — and posted it non-ephemerally so any
    channel member could click the stars and force the close.
    """
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
    
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await ctx.send("This is not a ticket channel.")
            return
    
        is_creator = ticket.get('creator_id') == ctx.author.id
        if is_creator and ows_get("ticket_rating_prompt"):
            view = TicketRatingView(ticket['ticket_id'], reason, ctx.channel, ctx.author)
            message = "⭐ **Please rate your support experience before closing:**"
        else:
            view = ConfirmCloseView(ticket['ticket_id'], reason)
            message = "Are you sure you want to close this ticket?"
    
        if ctx.interaction is not None:
            await ctx.send(message, view=view, ephemeral=True)
        else:
            await ctx.send(message, view=view)


    @bot.command(name="closerequest", aliases=["ca", "closereq"], description="Request staff to close this ticket (TicketTool-style)")
    @app_commands.describe(reason="Why should this ticket be closed?")
    async def close_request_cmd(ctx: commands.Context, *, reason: str = "No reason provided") -> None:
        """Post a TicketTool-style close request for staff to action.

    The ticket owner (or any member) asks for the ticket to be closed; staff
    confirm via the button. Also fires the premium `close_request` automation
    trigger, which previously existed in the automation engine but was never
    fired anywhere.
    """
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await ctx.send("This is not a ticket channel.")
            return
        if ticket.get('status') != 'open':
            await ctx.send("This ticket is not open.")
            return

        embed = discord.Embed(
            title="🙋 Close Request",
            description=(
                f"{ctx.author.mention} is requesting that this ticket be closed.\n"
                f"**Reason:** {reason}\n\n"
                "A staff member can confirm the close, or the requester can cancel."
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Ticket {ticket['ticket_id']}")
        await ctx.send(embed=embed, view=CloseRequestView(ticket['ticket_id'], ctx.author.id, reason))

        # Fire the premium 'close_request' automation trigger.
        if PREMIUM_AVAILABLE:
            try:
                pdb = getattr(bot, 'premium_db', None)
                if pdb is not None:
                    panel = data_manager.load_ticket_panel(ticket['panel_id']) if ticket.get('panel_id') else None
                    event = TicketTool.automations.AutomationEvent(
                        trigger='close_request', ticket=ticket, panel=panel or {},
                        guild=ctx.guild, bot=bot,
                        actor={'id': ctx.author.id, 'name': ctx.author.display_name},
                    )
                    await TicketTool.automations.fire_event(bot, pdb, event)
            except Exception as exc:
                logging.debug(f"[Premium] close_request trigger failed: {exc}")


    # =============================================================================
    # TICKET AUTOMATION PAUSE / RESUME (Ticket Tool /pause + /resume)
    # =============================================================================

    @bot.command(name="pause", description="Pause ALL automations for this ticket")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(duration="How long: 30m, 1h, 2d, 1w — omit for an indefinite pause")
    async def pause_ticket_cmd(ctx: commands.Context, duration: Optional[str] = None) -> None:
        """TicketTool-style per-ticket automation kill switch.

    While paused the ticket is excluded from ALL automatic actions:
    event-driven automations, delayed/no-response timers, SLA breach pings,
    and idle auto-close. Timed pauses auto-resume (lazy) and the paused time
    is subtracted from pending timers on resume.
    """
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await ctx.send("This is not a ticket channel.")
            return
        if ticket.get('status') != 'open':
            await ctx.send("Only open tickets can be paused.")
            return
        if _ticket_automation_paused(ticket):
            until = ticket.get('automation_paused_until')
            await ctx.send(
                "This ticket is already paused."
                + (f" Auto-resumes <t:{int(datetime.fromisoformat(str(until).replace('Z', '+00:00')).timestamp())}:R>." if until else " Use `!resume` to lift the pause.")
            )
            return

        seconds, error = parse_pause_duration(duration)
        if error:
            await ctx.send(error)
            return

        now = datetime.now(timezone.utc)
        ticket['automation_paused'] = 1
        ticket['automation_paused_at'] = now.isoformat()
        ticket['automation_paused_until'] = (
            (now + timedelta(seconds=seconds)).isoformat() if seconds else None
        )
        data_manager.save_ticket(ticket)

        if seconds:
            until_ts = int((now + timedelta(seconds=seconds)).timestamp())
            desc = (
                f"All automations for this ticket are paused until <t:{until_ts}:F> "
                f"(<t:{until_ts}:R>).\nPaused by {ctx.author.mention}."
            )
            footer = "Automations resume automatically at the deadline"
        else:
            desc = (
                "All automations for this ticket are paused **indefinitely**.\n"
                f"Paused by {ctx.author.mention}. Use `!resume` to lift the pause."
            )
            footer = "Paused — no automatic actions will run for this ticket"

        embed = discord.Embed(
            title="⏸️ Ticket Paused",
            description=desc,
            color=discord.Color.orange(),
            timestamp=now,
        )
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)
        logging.info(f"[Pause] {ctx.author} paused ticket {ticket['ticket_id']} (duration={duration or 'indefinite'})")


    @bot.command(name="resume", description="Resume automations for this paused ticket")
    @commands.has_permissions(manage_channels=True)
    async def resume_ticket_cmd(ctx: commands.Context) -> None:
        """Lift a ticket's automation pause.

    Pending delayed/no-response timers are shifted forward by the pause
    duration (paused time doesn't count), and the auto-close idle clock
    restarts from now.
    """
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await ctx.send("This is not a ticket channel.")
            return
        if not int(ticket.get('automation_paused') or 0):
            await ctx.send("This ticket is not paused.")
            return

        shifted = 0
        paused_seconds = 0
        if PREMIUM_AVAILABLE:
            try:
                pdb = getattr(bot, 'premium_db', None)
                if pdb is not None:
                    result = TicketTool.automations.resume_ticket_automations(bot, pdb, ticket)
                    shifted = result.get('shifted', 0)
                    paused_seconds = result.get('paused_seconds', 0)
                else:
                    ticket['automation_paused'] = 0
                    ticket['automation_paused_at'] = None
                    ticket['automation_paused_until'] = None
                    ticket['automation_resumed_at'] = datetime.now(timezone.utc).isoformat()
                    data_manager.save_ticket(ticket)
            except Exception as exc:
                logging.warning(f"[Resume] premium resume failed, falling back: {exc}")
                ticket['automation_paused'] = 0
                ticket['automation_paused_at'] = None
                ticket['automation_paused_until'] = None
                ticket['automation_resumed_at'] = datetime.now(timezone.utc).isoformat()
                data_manager.save_ticket(ticket)
        else:
            ticket['automation_paused'] = 0
            ticket['automation_paused_at'] = None
            ticket['automation_paused_until'] = None
            ticket['automation_resumed_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket(ticket)

        paused_min = paused_seconds // 60
        extra = ""
        if shifted:
            extra = f"\n⏱️ {shifted} pending timer(s) were pushed forward so paused time doesn't count."
        embed = discord.Embed(
            title="▶️ Ticket Resumed",
            description=(
                f"Automations are active again for this ticket "
                f"(paused for ~{paused_min} minute(s)).{extra}\n"
                f"Resumed by {ctx.author.mention}."
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="The auto-close idle clock restarts from now")
        await ctx.send(embed=embed)
        logging.info(f"[Resume] {ctx.author} resumed ticket {ticket['ticket_id']} (shifted {shifted} timers)")


    @bot.command(name="rate", description="Send the rating prompt to this ticket's creator")
    @commands.has_permissions(manage_channels=True)
    async def rate_ticket_cmd(ctx: commands.Context) -> None:
        """Ticket Tool-style manual CSAT: staff send the star-rating prompt to
    the ticket creator (each ticket can be rated once)."""
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await ctx.send("This is not a ticket channel.")
            return
        if ticket.get('status') != 'open':
            await ctx.send("Only open tickets can be rated.")
            return
        if ticket.get('rating') is not None:
            await ctx.send(
                f"This ticket has already been rated: **{ticket['rating']} star(s)**."
            )
            return
        creator = ctx.guild.get_member(int(ticket.get('creator_id') or 0))
        if creator is None:
            await ctx.send("The ticket creator is no longer in this server — they can't rate it.")
            return

        await ctx.send(
            f"{creator.mention} — please rate your support experience in this ticket "
            f"(requested by {ctx.author.mention}):",
            view=ManualRatingView(ticket['ticket_id'], creator.id),
            allowed_mentions=discord.AllowedMentions(users=True),
        )


    @bot.command(name="ticket-info", description="Show full status info for this ticket")
    async def ticket_info_cmd(ctx: commands.Context) -> None:
        """Ticket Tool-style ticket status overview: creator, category, priority,
    claim, SLA state, automation state, participants, age, activity, and more."""
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await ctx.send("This is not a ticket channel.")
            return

        status = ticket.get('status', 'open')
        status_emoji = {'open': '🟢', 'closed': '🔒', 'closing': '🟠',
                        'pending': '🟡', 'failed': '🔴'}.get(status, '❔')
        priority = ticket.get('priority') or 'normal'
        prio_emoji = PRIORITY_EMOJIS.get(priority, '')
        color = PRIORITY_COLORS.get(priority, discord.Color.blurple())
        if status == 'closed':
            color = discord.Color.dark_grey()

        creator = ctx.guild.get_member(int(ticket.get('creator_id') or 0))
        creator_str = creator.mention if creator else f"<@{ticket.get('creator_id')}>"
        claimer_str = "Unclaimed"
        if ticket.get('claimed_by'):
            claimer = ctx.guild.get_member(int(ticket['claimed_by']))
            claimer_str = claimer.mention if claimer else f"<@{ticket['claimed_by']}>"

        panel = data_manager.load_ticket_panel(ticket['panel_id']) if ticket.get('panel_id') else None
        ticket_type = ticket.get('category') or (panel.get('name') if panel else 'General')
        # Internal Ticket Category folder (Uncategorized when none assigned).
        ticket_category = _resolve_ticket_category_for_display(ctx.guild.id, ticket)

        embed = discord.Embed(
            title=f"{status_emoji} Ticket Info — {ticket['ticket_id']}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="📋 Status", value=status.capitalize(), inline=True)
        embed.add_field(name="🚨 Priority", value=f"{prio_emoji} {priority.capitalize()}", inline=True)
        embed.add_field(name="🧩 Type", value=str(ticket_type), inline=True)
        embed.add_field(name="📁 Category", value=ticket_category, inline=True)
        embed.add_field(name="👤 Creator", value=creator_str, inline=True)
        embed.add_field(name="🙋 Claimed By", value=claimer_str, inline=True)
        embed.add_field(name="🔒 Private", value="Yes" if ticket.get('is_private') else "No", inline=True)
        if ticket.get('subject'):
            embed.add_field(name="📝 Subject", value=str(ticket['subject'])[:1024], inline=False)

        # Activity block.
        created_iso = ticket.get('created_at')
        last_msg_iso = data_manager.get_last_ticket_message_time(ticket['ticket_id'])
        try:
            message_count = len(data_manager.load_ticket_messages(ticket['ticket_id']))
        except Exception:
            message_count = 0
        embed.add_field(
            name="⏱️ Activity",
            value=(
                f"Created: {created_iso[:16].replace('T', ' ')} UTC (age {_format_age(created_iso)})\n"
                f"Last message: {_format_age(last_msg_iso)} ago • Messages: {message_count}"
            ),
            inline=False,
        )
        if ticket.get('first_response_at'):
            embed.add_field(name="⚡ First Response", value=f"{_format_age(ticket['first_response_at'])} after creation", inline=True)
        if ticket.get('rating') is not None:
            embed.add_field(name="⭐ Rating", value=f"{ticket['rating']} star(s)", inline=True)
        if ticket.get('escalation_count'):
            embed.add_field(name="⬆️ Escalations", value=str(ticket['escalation_count']), inline=True)

        # Automation state (pause + premium SLA).
        auto_state = []
        if _ticket_automation_paused(ticket):
            until = ticket.get('automation_paused_until')
            if until:
                ts = int(datetime.fromisoformat(str(until).replace('Z', '+00:00')).timestamp())
                auto_state.append(f"⏸️ Paused — resumes <t:{ts}:R>")
            else:
                auto_state.append("⏸️ Paused (indefinite)")
        else:
            auto_state.append("✅ Active")
        if PREMIUM_AVAILABLE:
            try:
                pdb = getattr(bot, 'premium_db', None)
                if pdb is not None:
                    sla_state = pdb.get_sla_state(ticket['ticket_id'])
                    if sla_state:
                        fr = '✅ met' if sla_state.get('first_response_met_at') else (
                            '⏰ due' if sla_state.get('first_response_due_at') else '—')
                        res = '✅ met' if sla_state.get('resolution_met_at') else (
                            '⏰ due' if sla_state.get('resolution_due_at') else '—')
                        auto_state.append(f"SLA first response: {fr} • resolution: {res}")
            except Exception:
                pass
        embed.add_field(name="🤖 Automations", value='\n'.join(auto_state), inline=False)

        # Participants: members with explicit view overwrites (creator + added).
        participants = []
        try:
            for target, overwrite in getattr(ctx.channel, 'overwrites', {}).items():
                if isinstance(target, discord.Member) and target != ctx.guild.me:
                    participants.append(target.mention)
        except Exception:
            pass
        if participants:
            embed.add_field(name="👥 Participants", value=', '.join(participants[:20]), inline=False)

        if status == 'closed':
            closer = ctx.guild.get_member(int(ticket.get('closed_by') or 0)) if ticket.get('closed_by') else None
            if closer is not None:
                closer_str = closer.mention
            elif ticket.get('closed_by'):
                closer_str = f"<@{ticket.get('closed_by')}>"
            else:
                closer_str = 'Unknown'
            embed.add_field(
                name="🔒 Closure",
                value=(
                    f"Closed {_format_age(ticket.get('closed_at'))} ago by {closer_str}\n"
                    f"Reason: {ticket.get('close_reason') or 'No reason provided'}"
                ),
                inline=False,
            )

        embed.set_footer(text=f"Ticket {ticket['ticket_id']} • /ticket-info")
        await ctx.send(embed=embed)


    @bot.command(name="private", description="Make this ticket private (hidden from other staff)")
    @commands.has_permissions(manage_channels=True)
    async def private_ticket_cmd(ctx: commands.Context) -> None:
        """Ticket Tool-style /private: remove the support role's access so only
    the creator, the claimer, and admins (who bypass overwrites) can see the
    ticket."""
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await ctx.send("This is not a ticket channel.")
            return
        if ticket.get('is_private'):
            await ctx.send("This ticket is already private. Use `!unprivate` to restore staff access.")
            return

        panel = data_manager.load_ticket_panel(ticket['panel_id']) if ticket.get('panel_id') else None
        settings = data_manager.load_ticket_settings(ctx.guild.id) or {}
        support_role_id = (
            (panel.get('support_role_id') if panel else None)
            or settings.get('support_role_id')
        )
        hidden_roles = []
        if support_role_id:
            role = ctx.guild.get_role(int(support_role_id))
            if role:
                try:
                    await ctx.channel.set_permissions(
                        role, view_channel=False, send_messages=False,
                        reason=f"Ticket made private by {ctx.author}",
                    )
                    hidden_roles.append(role.mention)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    await ctx.send(f"Could not hide the support role: {exc}")
                    return
        ticket['is_private'] = 1
        data_manager.save_ticket(ticket)

        desc = "🔒 This ticket is now **private** — only the creator, claimer, and admins can see it."
        if hidden_roles:
            desc += f"\nHidden from: {', '.join(hidden_roles)}"
        await ctx.send(embed=discord.Embed(
            description=desc + f"\nMade private by {ctx.author.mention}.",
            color=discord.Color.dark_theme(),
            timestamp=datetime.now(timezone.utc),
        ))
        logging.info(f"[Tickets] {ctx.author} made ticket {ticket['ticket_id']} private")


    @bot.command(name="unprivate", description="Restore staff access to this private ticket")
    @commands.has_permissions(manage_channels=True)
    async def unprivate_ticket_cmd(ctx: commands.Context) -> None:
        """Lift /private: give the support role its standard ticket access back."""
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await ctx.send("This is not a ticket channel.")
            return
        if not ticket.get('is_private'):
            await ctx.send("This ticket is not private.")
            return

        panel = data_manager.load_ticket_panel(ticket['panel_id']) if ticket.get('panel_id') else None
        settings = data_manager.load_ticket_settings(ctx.guild.id) or {}
        support_role_id = (
            (panel.get('support_role_id') if panel else None)
            or settings.get('support_role_id')
        )
        restored = []
        if support_role_id:
            role = ctx.guild.get_role(int(support_role_id))
            if role:
                try:
                    await ctx.channel.set_permissions(
                        role, view_channel=True, send_messages=True,
                        read_message_history=True, attach_files=True,
                        reason=f"Ticket unprivated by {ctx.author}",
                    )
                    restored.append(role.mention)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    await ctx.send(f"Could not restore the support role: {exc}")
                    return
        ticket['is_private'] = 0
        data_manager.save_ticket(ticket)

        desc = "🔓 This ticket is no longer private — the support team has access again."
        if restored:
            desc += f"\nRestored for: {', '.join(restored)}"
        await ctx.send(embed=discord.Embed(
            description=desc + f"\nRestored by {ctx.author.mention}.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        ))
        logging.info(f"[Tickets] {ctx.author} restored ticket {ticket['ticket_id']} to non-private")


    @bot.command(name="tickethelp", description="Show every ticket-system command by category")
    async def ticket_help_cmd(ctx: commands.Context) -> None:
        """Ticket Tool-style /help: categorized command discovery for the whole
    ticket subsystem (core + premium)."""
        premium_note = "" if PREMIUM_AVAILABLE else "\n*(Premium package not loaded — the ⭐ commands are inactive.)*"
        embed = discord.Embed(
            title="🎫 Ticket System Help",
            description=f"Everything you can do with the ticket system.{premium_note}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="🎫 Panels & Creation",
            value=(
                "`!panel` • `!panels` • `!deletepanel` • `!panelupdate`\n"
                "`!multipanel` • `!dropdownpanel` • `!reactionpanel`\n"
                "`!panelquestion` • `!new` • `!ticket`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎟️ In-Ticket (everyone)",
            value=(
                "`!ticket-info` • `!transcript` • `!closerequest` (`!ca`)\n"
                "`!add @user` • `!remove @user` (staff)"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛠️ Staff Management",
            value=(
                "`!claim` • `!unclaim` • `!close` • `!reopen`\n"
                "`!rename` • `!move` • `!note` • `!notes` • `!priority`\n"
                "`!pause` • `!resume` • `!rate` • `!private` • `!unprivate`"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ Configuration (admin)",
            value=(
                "`!ticketsettings` • `!ticketlog` • `!limitbypass` • `!ticketstats`\n"
                "`!ticketblacklist` • `!ticketunblacklist` • `!tickets` • `!dbcleanup`"
            ),
            inline=False,
        )
        embed.add_field(
            name="⭐ Premium (package)",
            value=(
                "`!naming` • `!schedule` • `!claimconfig` • `!roleauto` • `!automate`\n"
                "`!escalate` • `!escalateroute` • `!transcriptconfig` • `!slaconfig`\n"
                "`!analytics` • `!csat` • `!staffstats` • `!export` • `!kb`\n"
                "`!canned` • `!flow` • `!customcommand` • `!locale` • …"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔍 Diagnostics",
            value="`!ticketdebug` • `!permissionlevel`",
            inline=False,
        )
        embed.set_footer(text="Use commands inside a ticket channel where noted")
        await ctx.send(embed=embed)


    @bot.command(name="transcript", description="Generate a transcript of this ticket")
    @app_commands.describe(
        channel="Optional channel to send the transcript to",
        lines="Max number of messages to include (default: all)",
    )
    async def transcript_cmd(ctx: commands.Context, channel: Optional[discord.TextChannel] = None, lines: Optional[int] = None) -> None:
        """Generate a transcript of the current ticket (TicketTool $transcript)."""
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
    
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await ctx.send("This is not a ticket channel.")
            return
    
        limit = max(1, min(int(lines), 1000)) if lines else None
        transcript = await state.ticket_tool._generate_transcript(ctx.channel, ticket, ctx.author, limit=limit)
        if channel is not None:
            await channel.send(embed=transcript['embed'], file=transcript['file'])
            await _ticket_respond(ctx, f"Transcript sent to {channel.mention}.", ephemeral=True)
            try:
                await log_ticket_event(ctx.guild, 'transcript', ticket, actor=ctx.author,
                                       detail=f"Exported to #{channel.name}")
            except Exception:
                pass
        else:
            await ctx.send(embed=transcript['embed'], file=transcript['file'])


    @bot.command(name="panelquestion", description="Manage the questions (form) shown before a ticket is created")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        action="Action: add, remove, or list",
        panel_id="Panel ID (see /panels)",
        question="Question text (for add)",
        question_type="text (single line) or paragraph (for add)",
        required="Whether answering is required (for add)",
        placeholder="Input placeholder (for add)",
        order_index="Position of the question, 1-5 (for add)",
        question_id="Question ID to delete (for remove)",
    )
    async def panel_question_cmd(
        ctx: commands.Context,
        action: str,
        panel_id: str,
        question: Optional[str] = None,
        question_type: str = "text",
        required: bool = True,
        placeholder: Optional[str] = None,
        order_index: Optional[int] = None,
        question_id: Optional[str] = None,
    ) -> None:
        """TicketTool-style panel forms: up to 5 questions asked in a modal
    before the ticket is created. The questions table + modal existed but no
    UI could ever create questions, leaving the whole feature orphaned."""
        action = (action or '').lower().strip()
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel or panel.get('guild_id') != ctx.guild.id:
            await ctx.send(f"Panel `{panel_id}` not found in this server.")
            return

        if action == 'add':
            if not question:
                await ctx.send("Provide the `question` text to add.")
                return
            existing = data_manager.load_panel_questions(panel_id)
            if len(existing) >= 5:
                await ctx.send("This panel already has the maximum of 5 questions. Remove one first.")
                return
            q_type = 'paragraph' if (question_type or '').lower().startswith('para') else 'text'
            next_index = (max((int(q.get('order_index') or 0) for q in existing), default=0) + 1) if order_index is None else order_index
            question_row = {
                'question_id': str(uuid.uuid4())[:8],
                'panel_id': panel_id,
                'guild_id': ctx.guild.id,
                'question_text': question[:256],
                'question_type': q_type,
                'required': 1 if required else 0,
                'placeholder': (placeholder or '')[:100],
                'order_index': next_index,
                'created_at': datetime.now(timezone.utc).isoformat(),
            }
            data_manager.save_ticket_question(question_row)
            embed = discord.Embed(
                title="❓ Panel Question Added",
                description=(
                    f"**Panel:** {panel.get('name', 'Unknown')} (`{panel_id}`)\n"
                    f"**Question:** {question[:256]}\n"
                    f"**Type:** {q_type}\n"
                    f"**Required:** {'Yes' if required else 'No'}\n"
                    f"**Order:** {next_index}\n"
                    f"**Question ID:** `{question_row['question_id']}`"
                ),
                color=discord.Color.green(),
            )
            embed.set_footer(text=f"{len(existing) + 1}/5 questions on this panel")
            await ctx.send(embed=embed)
            return

        if action == 'remove':
            if not question_id:
                await ctx.send("Provide the `question_id` to remove (see `!panelquestion list`).")
                return
            deleted = data_manager.delete_ticket_question(question_id)
            if deleted:
                await ctx.send(f"Question `{question_id}` removed from panel `{panel_id}`.")
            else:
                await ctx.send(f"Question `{question_id}` not found.")
            return

        if action == 'list':
            questions = data_manager.load_panel_questions(panel_id)
            if not questions:
                await ctx.send(
                    f"Panel `{panel_id}` has no questions. Add one with "
                    "`!panelquestion add`."
                )
                return
            embed = discord.Embed(
                title=f"❓ Panel Questions — {panel.get('name', 'Unknown')}",
                description=f"Panel ID: `{panel_id}`",
                color=discord.Color.blurple(),
            )
            for q in questions[:5]:
                embed.add_field(
                    name="{}. {}".format(q.get('order_index', 0), str(q['question_text'])[:100]),
                    value=(
                        f"ID: `{q['question_id']}` • Type: {q.get('question_type', 'text')} • "
                        f"Required: {'Yes' if q.get('required') else 'No'}"
                    ),
                    inline=False,
                )
            await ctx.send(embed=embed)
            return

        await ctx.send("Unknown action. Use `add`, `remove`, or `list`.")


    @bot.command(name="limitbypass", description="Set roles that bypass the ticket limits for a panel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panel_id="Panel ID (see /panels)",
        roles="Roles to bypass limits (mention them), 'none' to clear",
    )
    async def limit_bypass_cmd(ctx: commands.Context, panel_id: str, roles: str) -> None:
        """TicketTool-style limit bypass roles (per panel). Members holding any
    of these roles skip the per-panel and global open-ticket limits."""
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel or panel.get('guild_id') != ctx.guild.id:
            await ctx.send(f"Panel `{panel_id}` not found in this server.")
            return

        raw = (roles or '').strip()
        if raw.lower() in ('none', 'clear', 'off'):
            panel['limit_bypass_role_ids'] = None
            data_manager.save_ticket_panel(panel)
            await ctx.send(f"Limit bypass roles cleared for panel `{panel_id}`.")
            return

        import re as _re
        role_ids = [int(m) for m in _re.findall(r'<@&(\d+)>', raw)]
        for token in raw.split():
            if token.isdigit():
                role_ids.append(int(token))
        role_ids = list(dict.fromkeys(role_ids))
        valid_ids = []
        for rid in role_ids:
            if ctx.guild.get_role(rid) is not None:
                valid_ids.append(rid)
        if not valid_ids:
            await ctx.send("No valid roles found. Mention roles (`@Role`) or paste their IDs.")
            return
        panel['limit_bypass_role_ids'] = json.dumps(valid_ids)
        data_manager.save_ticket_panel(panel)
        mentions = ' '.join(f"<@&{rid}>" for rid in valid_ids)
        await ctx.send(embed=discord.Embed(
            description=(
                f"✅ Members with {mentions} now bypass the ticket limits on panel "
                f"**{panel.get('name', 'Unknown')}** (`{panel_id}`)."
            ),
            color=discord.Color.green(),
        ))


    @bot.command(name="panelupdate", description="Refresh an existing panel message (TicketTool-style Update)")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="Panel ID to refresh (see /panels)")
    async def panel_update_cmd(ctx: commands.Context, panel_id: str) -> None:
        """Re-render an already-sent panel message with its current embeds and
    button configuration (multi-embed included). TicketTool's dashboard
    "Update" feature: edit the panel in place instead of re-sending."""
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel or panel.get('guild_id') != ctx.guild.id:
            await ctx.send(f"Panel `{panel_id}` not found in this server.")
            return
        channel_id = panel.get('channel_id')
        message_id = panel.get('message_id')
        if not channel_id or not message_id:
            await ctx.send("This panel has no sent message yet. Use `!panel` to create and send one.")
            return
        channel = ctx.guild.get_channel(int(channel_id))
        if channel is None:
            await ctx.send("The channel this panel was sent in no longer exists.")
            return
        try:
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(f"Could not fetch the panel message: {exc}")
            return

        view = TicketPanelView(panel)
        try:
            await message.edit(embeds=_build_panel_message_embeds(panel), view=view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(f"Could not edit the panel message: {exc}")
            return
        # Keep the fresh view registered for persistence.
        ctx.bot.add_view(view)

        # TicketTool "Update" also refreshes multi-panel messages that contain
        # this panel (attached panels / dropdown panels), pruning rows whose
        # message no longer exists.
        refreshed_multi = 0
        pruned_multi = 0
        for row in data_manager.load_multi_panels_by_guild(ctx.guild.id):
            try:
                panel_ids = json.loads(row.get('panel_ids') or '[]')
            except (ValueError, TypeError):
                panel_ids = []
            if panel_id not in panel_ids:
                continue
            mp_channel = ctx.guild.get_channel(int(row.get('channel_id') or 0))
            if mp_channel is None:
                data_manager.delete_multi_panel(row['message_id'])
                pruned_multi += 1
                continue
            try:
                mp_message = await mp_channel.fetch_message(int(row['message_id']))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                data_manager.delete_multi_panel(row['message_id'])
                pruned_multi += 1
                continue
            try:
                mp_panels = [p for p in (data_manager.load_ticket_panel(pid) for pid in panel_ids)
                             if p and p.get('is_active', 1)]
                if not mp_panels:
                    data_manager.delete_multi_panel(row['message_id'])
                    pruned_multi += 1
                    continue
                mp_view = build_multi_panel_view(row, mp_panels)
                await mp_message.edit(embeds=_build_multi_panel_embeds(mp_panels), view=mp_view)
                ctx.bot.add_view(mp_view)
                refreshed_multi += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                logging.warning(f"[PanelUpdate] multi-panel {row['message_id']} refresh failed: {exc}")

        extra = ""
        if refreshed_multi or pruned_multi:
            extra = f" Also refreshed {refreshed_multi} multi-panel message(s)"
            if pruned_multi:
                extra += f" and pruned {pruned_multi} dead multi-panel row(s)."
            else:
                extra += "."
        await ctx.send(
            f"✅ Panel `{panel_id}` updated in {channel.mention} "
            f"({len(_build_panel_message_embeds(panel))} embed(s)).{extra}"
        )


    @bot.command(name="multipanel", description="Combine up to 25 panels into ONE message (TicketTool Attached Panels)")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panels="Comma-separated panel IDs (see /panels), e.g. 'abc123,def456'",
        per_row="Buttons per row, 1-5 (default 5)",
    )
    async def multi_panel_cmd(ctx: commands.Context, panels: str, per_row: int = 5) -> None:
        """Send a TicketTool-style multi-panel: one message, one create-button
    per attached panel. Each button keeps its panel's own label/emoji/style
    and routes through the full gate flow (limits, blacklist, schedule)."""
        panel_rows, error = _parse_panel_id_list(panels, ctx.guild.id)
        if error:
            await ctx.send(error)
            return
        per_row = max(1, min(5, per_row))

        view = MultiPanelView(panel_rows, per_row=per_row)
        message = await ctx.send(embeds=_build_multi_panel_embeds(panel_rows), view=view)

        data_manager.save_multi_panel({
            'message_id': message.id,
            'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'style': 'buttons',
            'panel_ids': json.dumps([p['panel_id'] for p in panel_rows]),
            'per_row': per_row,
            'placeholder': None,
            'created_at': datetime.now(timezone.utc).isoformat(),
        })
        ctx.bot.add_view(view)
        await ctx.send(
            f"✅ Multi-panel created with **{len(panel_rows)}** panels. "
            f"Refresh it after edits with `!panelupdate <panel_id>`.",
            ephemeral=True,
        )
        logging.info(f"[TicketTool] {ctx.author} created a {len(panel_rows)}-panel multi-panel in #{ctx.channel.name}")


    @bot.command(name="dropdownpanel", description="Create a dropdown-style panel (select menu routes to a panel)")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panels="Comma-separated panel IDs (see /panels), e.g. 'abc123,def456'",
        placeholder="Select-menu placeholder text (default 'Select a ticket type…')",
    )
    async def dropdown_panel_cmd(ctx: commands.Context, panels: str, placeholder: Optional[str] = None) -> None:
        """Send a TicketTool-style dropdown panel: a Discord select menu where
    each option (label/description/emoji from the panel config) opens that
    panel's ticket flow."""
        panel_rows, error = _parse_panel_id_list(panels, ctx.guild.id)
        if error:
            await ctx.send(error)
            return

        view = TicketPanelSelectView(panel_rows, placeholder or "Select a ticket type…")
        message = await ctx.send(embeds=_build_multi_panel_embeds(panel_rows), view=view)

        data_manager.save_multi_panel({
            'message_id': message.id,
            'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'style': 'dropdown',
            'panel_ids': json.dumps([p['panel_id'] for p in panel_rows]),
            'per_row': 5,
            'placeholder': placeholder,
            'created_at': datetime.now(timezone.utc).isoformat(),
        })
        ctx.bot.add_view(view)
        await ctx.send(
            f"✅ Dropdown panel created with **{len(panel_rows)}** options. "
            f"Refresh it after edits with `!panelupdate <panel_id>`.",
            ephemeral=True,
        )
        logging.info(f"[TicketTool] {ctx.author} created a {len(panel_rows)}-option dropdown panel in #{ctx.channel.name}")


    @bot.command(name="reactionpanel", description="Create a reaction-based ticket panel (react to open a ticket)")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(
        panels="Comma-separated panel IDs in the SAME order as the emojis (see /panels)",
        emojis="Comma-separated emojis, one per panel (e.g. '🎫,🛠️')",
        title="Optional panel title",
    )
    async def reaction_panel_cmd(ctx: commands.Context, panels: str, emojis: str, title: Optional[str] = None) -> None:
        """Ticket Tool-style reaction panels: users REACT with an emoji to open
    the matching panel's ticket (legacy compatibility with reaction-based
    servers). Each emoji maps to one panel; the bot removes the reaction
    after handling so users can re-react later.
    """
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
        panel_ids = [t.strip() for t in (panels or '').split(',') if t.strip()]
        emoji_list = [e.strip() for e in (emojis or '').split(',') if e.strip()]
        if len(panel_ids) < 1:
            await ctx.send("Provide at least one panel ID (see `!panels`).")
            return
        if len(panel_ids) != len(emoji_list):
            await ctx.send(
                f"Panel/emoji mismatch: {len(panel_ids)} panel(s) but {len(emoji_list)} emoji(s). "
                "List them in the same order, comma-separated."
            )
            return
        if len(panel_ids) > 20:
            await ctx.send("Reaction panels support at most 20 emoji mappings.")

        # Resolve + validate every panel.
        panel_rows = []
        for pid in panel_ids:
            panel = data_manager.load_ticket_panel(pid)
            if not panel or panel.get('guild_id') != ctx.guild.id or not panel.get('is_active', 1):
                await ctx.send(f"Panel `{pid}` not found in this server (see `!panels`).")
                return
            panel_rows.append(panel)

        mapping = {emoji: panel_rows[i]['panel_id'] for i, emoji in enumerate(emoji_list)}
        if len(mapping) != len(emoji_list):
            await ctx.send("Duplicate emojis detected — each emoji must be unique.")
            return

        # Build the panel message.
        lines = []
        for emoji, panel in zip(emoji_list, panel_rows):
            desc = (panel.get('embed_description') or '').strip()
            line = f"{emoji} **{panel.get('name', 'Panel')}**"
            if desc:
                line += f"\n> {desc[:200]}"
            lines.append(line)
        embed = discord.Embed(
            title=title or "🎫 Support Tickets",
            description=(
                "React with the emoji matching your issue to open a ticket.\n\n"
                + "\n\n".join(lines)
            )[:4000],
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"{len(panel_rows)} ticket type(s) • remove your reaction and re-react for another ticket")
        message = await ctx.send(embed=embed)

        # Persist the mapping + react with every emoji.
        data_manager.save_reaction_panel({
            'message_id': message.id,
            'guild_id': ctx.guild.id,
            'channel_id': ctx.channel.id,
            'title': title,
            'mapping': json.dumps(mapping),
            'created_at': datetime.now(timezone.utc).isoformat(),
        })
        failed = []
        for emoji in emoji_list:
            try:
                await message.add_reaction(emoji)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                failed.append(emoji)
        # Warm the in-memory lookup cache.
        _reaction_panel_cache[message.id] = mapping
        if failed:
            await ctx.send(
                f"⚠️ Reaction panel saved, but these emojis could not be added (the bot "
                f"may lack the Add Reactions permission): {' '.join(failed)}",
                ephemeral=True,
            )
        else:
            await ctx.send(
                f"✅ Reaction panel created with **{len(panel_rows)}** mapping(s). "
                "Users react with an emoji to open that ticket type.",
                ephemeral=True,
            )
        logging.info(f"[TicketTool] {ctx.author} created a reaction panel with {len(mapping)} mapping(s) in #{ctx.channel.name}")


    @bot.command(name="new", description="Open a new ticket (command-style, TicketTool $new)")
    @app_commands.describe(
        user="Open on behalf of this user (staff only)",
        panel_id="Panel to open the ticket in (see /panels)",
        reason="Reason / subject for the ticket",
    )
    async def new_ticket_cmd(
        ctx: commands.Context,
        user: Optional[discord.Member] = None,
        panel_id: Optional[str] = None,
        *,
        reason: str = "",
    ) -> None:
        """TicketTool-style command tickets (`$new` / `$ticket`).

    * Self-open: full gate flow (blacklist, limits, schedule) — panels with
      questions require the panel button (the form opens there).
    * Staff opening for another user: opens on their behalf with the given
      reason (questions are skipped — staff intent).
    """
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return

        panel, error = await _resolve_command_style_panel(ctx.guild, panel_id)
        if error:
            await ctx.send(error)
            return

        # Staff-on-behalf mode.
        if user is not None and user.id != ctx.author.id:
            perms = getattr(ctx.author, 'guild_permissions', None)
            if not perms or not (perms.administrator or perms.manage_channels):
                await ctx.send("Only staff can open tickets on behalf of another user.")
                return
            if user.bot:
                await ctx.send("You cannot open a ticket for a bot.")
                return
            channel, result = await state.ticket_tool.create_ticket(
                ctx.guild, user, panel, subject=(reason or None) or None,
            )
            if channel:
                await TicketPanelView(panel)._send_welcome_message(channel, user, panel)
                await ctx.send(f"✅ Ticket opened for {user.mention}: {channel.mention}")
            else:
                await ctx.send(f"Failed to create ticket: {result}")
            return

        # Self-open. Business-hours gate (same as the panel button).
        if PREMIUM_AVAILABLE:
            try:
                pdb = getattr(bot, 'premium_db', None)
                if pdb is not None:
                    role_ids = [r.id for r in ctx.author.roles] if hasattr(ctx.author, 'roles') else []
                    is_open, unavailable_msg = TicketTool.scheduling.is_panel_open_now(pdb, panel, role_ids)
                    if not is_open:
                        next_open = TicketTool.scheduling.next_open_time(pdb, panel)
                        extra = f"\n\n*Opens {next_open}.*" if next_open else ''
                        await ctx.send(f"{unavailable_msg}{extra}")
                        return
            except Exception as exc:
                logging.debug(f"[Premium] /new scheduling gate failed: {exc}")

        questions = data_manager.load_panel_questions(panel['panel_id'])
        if questions:
            await ctx.send(
                "This panel requires a short form before the ticket is created — "
                "please use the panel button."
            )
            return

        channel, result = await state.ticket_tool.create_ticket(
            ctx.guild, ctx.author, panel, subject=(reason or None) or None,
        )
        if channel:
            await TicketPanelView(panel)._send_welcome_message(channel, ctx.author, panel)
            await ctx.send(f"✅ Ticket created: {channel.mention}")
        else:
            await ctx.send(f"Failed to create ticket: {result}")


    @bot.command(name="ticket", description="Open a new ticket (alias of /new)")
    @app_commands.describe(
        user="Open on behalf of this user (staff only)",
        panel_id="Panel to open the ticket in (see /panels)",
        reason="Reason / subject for the ticket",
    )
    async def ticket_cmd_alias(
        ctx: commands.Context,
        user: Optional[discord.Member] = None,
        panel_id: Optional[str] = None,
        *,
        reason: str = "",
    ) -> None:
        """Alias of /new (TicketTool's `$new` / `$ticket` pair)."""
        await new_ticket_cmd(ctx, user, panel_id, reason=reason)


    @bot.command(name="ticketdebug", description="Ticket system diagnostics (TicketTool $debug)")
    async def ticket_debug_cmd(ctx: commands.Context) -> None:
        """Show the ticket-system configuration + the bot's permission status,
    mirroring TicketTool's $debug command."""
        guild = ctx.guild
        settings = data_manager.load_ticket_settings(guild.id) or {}
        panels = data_manager.load_ticket_panels_by_guild(guild.id)
        open_count = data_manager.count_open_tickets_in_guild(guild.id)

        def _flag(ok: bool) -> str:
            return "✅" if ok else "❌"

        category = guild.get_channel(settings.get('category_id') or 0) if settings.get('category_id') else None
        transcripts = guild.get_channel(settings.get('transcripts_channel_id') or 0) if settings.get('transcripts_channel_id') else None
        log_channel = guild.get_channel(settings.get('log_channel_id') or 0) if settings.get('log_channel_id') else None
        support_role = guild.get_role(settings.get('support_role_id') or 0) if settings.get('support_role_id') else None

        me = guild.me
        perms = me.guild_permissions
        embed = discord.Embed(
            title="🎫 Ticket System Diagnostics",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="System",
            value=(
                f"{_flag(ows_get('enable_tickets'))} Tickets enabled\n"
                f"🎫 Panels: **{len(panels)}** • Open tickets: **{open_count}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="Configuration",
            value=(
                f"{_flag(category is not None)} Ticket category: {category.name if category else 'Not set (config fallback)'}\n"
                f"{_flag(transcripts is not None)} Transcripts channel: {transcripts.mention if transcripts else 'Not set'}\n"
                f"{_flag(log_channel is not None)} Log channel: {log_channel.mention if log_channel else 'Not set'}\n"
                f"{_flag(support_role is not None)} Support role: {support_role.mention if support_role else 'Not set'}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Bot Permissions",
            value=(
                f"{_flag(perms.manage_channels)} Manage Channels\n"
                f"{_flag(perms.manage_roles)} Manage Roles\n"
                f"{_flag(perms.view_channel and perms.send_messages)} View + Send in channels"
            ),
            inline=False,
        )
        if panels:
            panel_lines = []
            for p in panels[:10]:
                panel_lines.append(f"`{p['panel_id']}` {p.get('name', 'Unnamed')}")
            embed.add_field(name="Panels", value='\n'.join(panel_lines), inline=False)
        await ctx.send(embed=embed)


    @bot.command(name="permissionlevel", description="Show your ticket-system permission level (TicketTool $permissionlevel)")
    async def permission_level_cmd(ctx: commands.Context) -> None:
        """Report the invoker's effective ticket-system access level,
    mirroring TicketTool's $permissionlevel / $levels command."""
        member = ctx.author
        perms = member.guild_permissions
        settings = data_manager.load_ticket_settings(ctx.guild.id) or {}

        if ctx.guild.owner_id == member.id:
            level, desc = 5, "Server Owner (full access)"
        elif perms.administrator:
            level, desc = 4, "Administrator (full access)"
        elif perms.manage_guild:
            level, desc = 3, "Manage Server (settings + panels)"
        elif perms.manage_channels:
            level, desc = 2, "Manage Channels (staff: claim/close/priority/notes)"
        else:
            support_role_id = settings.get('support_role_id')
            support_role = ctx.guild.get_role(support_role_id) if support_role_id else None
            if support_role and support_role in member.roles:
                level, desc = 2, f"Support Team ({support_role.mention}: claim/close)"
            else:
                level, desc = 1, "User (create tickets, rate, close own)"

        embed = discord.Embed(
            title="🎫 Ticket Permission Level",
            description=f"{member.mention} — **Level {level}**: {desc}",
            color=discord.Color.green() if level >= 2 else discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Levels",
            value=(
                "`5` Server Owner • `4` Administrator • `3` Manage Server\n"
                "`2` Staff (Manage Channels / Support Role) • `1` User"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)


    @bot.command(name="ticketlog", description="Configure the ticket log channel and logged events")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel for ticket logs (leave empty to view the current config)",
        events="Comma-separated events, 'all', or 'none' (e.g. created,closed,transcript)",
    )
    async def ticket_log_cmd(ctx: commands.Context, channel: Optional[discord.TextChannel] = None, events: Optional[str] = None) -> None:
        """TicketTool-style logging channel configuration. Logged events:
    created, closed, reopened, renamed, deleted, transcript, claim, unclaim, priority."""
        settings = data_manager.load_ticket_settings(ctx.guild.id) or {'guild_id': ctx.guild.id}
        if channel is None and events is None:
            current_events = get_ticket_log_events(ctx.guild.id)
            log_channel = ctx.guild.get_channel(settings.get('log_channel_id') or 0) if settings.get('log_channel_id') else None
            embed = discord.Embed(
                title="🎫 Ticket Logging",
                description=(
                    f"**Log channel:** {log_channel.mention if log_channel else 'Not set'}\n"
                    f"**Logged events:** {', '.join(f'`{e}`' for e in current_events) if current_events else 'None'}"
                ),
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="Available events",
                value="`created` `closed` `reopened` `renamed` `deleted` `transcript` `claim` `unclaim` `priority`",
                inline=False,
            )
            embed.set_footer(text="Usage: /ticketlog channel:#logs events:created,closed,transcript")
            await ctx.send(embed=embed)
            return

        if channel is not None:
            settings['log_channel_id'] = channel.id
        if events is not None:
            raw = (events or '').strip().lower()
            if raw == 'all':
                new_events = list(TICKET_LOG_EVENT_INFO.keys())
            elif raw in ('none', 'off', 'disable'):
                new_events = []
            else:
                new_events = [e.strip() for e in raw.split(',') if e.strip() in TICKET_LOG_EVENT_INFO]
            settings['log_events'] = json.dumps(new_events)
        settings['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_ticket_settings(settings)

        log_channel = ctx.guild.get_channel(settings.get('log_channel_id') or 0) if settings.get('log_channel_id') else None
        current_events = get_ticket_log_events(ctx.guild.id)
        await ctx.send(embed=discord.Embed(
            description=(
                f"✅ Ticket logging updated.\n"
                f"**Log channel:** {log_channel.mention if log_channel else 'Not set'}\n"
                f"**Logged events:** {', '.join(f'`{e}`' for e in current_events) if current_events else 'None'}"
            ),
            color=discord.Color.green(),
        ))


    @bot.command(name="ticketsettings", description="Configure ticket system settings")
    @commands.has_permissions(manage_guild=True)
    async def ticket_settings_cmd(ctx: commands.Context) -> None:
        """Open ticket settings configuration."""
        settings = data_manager.load_ticket_settings(ctx.guild.id) or {}
    
        embed = discord.Embed(title="Ticket System Settings", color=discord.Color.blurple())
    
        category_id = settings.get('category_id')
        category = ctx.guild.get_channel(category_id) if category_id else None
    
        transcripts_id = settings.get('transcripts_channel_id')
        transcripts = ctx.guild.get_channel(transcripts_id) if transcripts_id else None
    
        support_id = settings.get('support_role_id')
        support_role = ctx.guild.get_role(support_id) if support_id else None
    
        embed.add_field(name="Ticket Category", value=category.name if category else "Not Set", inline=True)
        embed.add_field(name="Ticket Transcripts Archive Channel", value=transcripts.mention if transcripts else "Not Set", inline=True)
        embed.add_field(name="Support Role", value=support_role.mention if support_role else "Not Set", inline=True)
        embed.add_field(name="Max Tickets/User", value=str(settings.get('max_tickets_per_user', 3)), inline=True)
        embed.add_field(name="Auto-Close Hours", value=str(settings.get('auto_close_hours', 24)), inline=True)
        embed.add_field(name="DM Transcripts", value="Yes" if settings.get('dm_transcripts', 1) else "No", inline=True)

        log_channel = ctx.guild.get_channel(settings.get('log_channel_id') or 0) if settings.get('log_channel_id') else None
        embed.add_field(name="Ticket Log Channel", value=log_channel.mention if log_channel else "Not Set", inline=True)
        closed_category = ctx.guild.get_channel(settings.get('closed_category_id') or 0) if settings.get('closed_category_id') else None
        embed.add_field(name="Closed Ticket Category", value=closed_category.name if closed_category else "Not Set", inline=True)
        logged_events = get_ticket_log_events(ctx.guild.id)
        embed.add_field(name="Logged Events", value=', '.join(f'`{e}`' for e in logged_events) if logged_events else 'None', inline=False)
        embed.add_field(
            name="Limits & Timers",
            value=(
                f"Max Closed/User: {settings.get('max_closed_tickets_per_user', 0) or 0} • "
                f"Max Open (all): {settings.get('max_open_tickets_all', 0) or 0} • "
                f"SLA: {settings.get('sla_hours', 0) or 0}h"
            ),
            inline=False,
        )
    
        embed.set_footer(text="Use the modal to update settings")
    
        await ctx.send(embed=embed, view=TicketSettingsConfigView(ctx.guild.id))


    @bot.command(name="ticketblacklist", description="Blacklist a user from creating tickets")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(user="User to blacklist", reason="Reason for blacklist")
    async def ticket_blacklist_cmd(ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided") -> None:
        """Blacklist a user from creating tickets."""
        import uuid
        blacklist_data = {
            'blacklist_id': str(uuid.uuid4())[:8],
            'guild_id': ctx.guild.id,
            'user_id': user.id,
            'reason': reason,
            'blacklisted_by': ctx.author.id,
            'blacklisted_at': datetime.now(timezone.utc).isoformat(),
            'is_active': 1
        }
        data_manager.save_ticket_blacklist(blacklist_data)
        await ctx.send(embed=discord.Embed(
            title="User Blacklisted",
            description=f"{user.mention} has been blacklisted from creating tickets.\n**Reason:** {reason}",
            color=discord.Color.red()
        ))


    @bot.command(name="ticketunblacklist", description="Remove a user from the ticket blacklist")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(user="User to unblacklist")
    async def ticket_unblacklist_cmd(ctx: commands.Context, user: discord.Member) -> None:
        """Remove a user from the ticket blacklist."""
        success = data_manager.remove_ticket_blacklist(ctx.guild.id, user.id)
        if success:
            await ctx.send(f"{user.mention} has been removed from the blacklist.")
        else:
            await ctx.send(f"{user.mention} is not blacklisted.")


    @bot.command(name="tickets", description="View open tickets")
    @commands.has_permissions(manage_channels=True)
    async def view_tickets_cmd(ctx: commands.Context) -> None:
        tickets = data_manager.load_tickets_by_guild(ctx.guild.id, 'open')
        if not tickets:
            await ctx.send("No open tickets.")
            return

        # Filter out orphaned tickets (channel no longer exists)
        valid_tickets = []
        for ticket in tickets:
            channel = ctx.guild.get_channel(ticket['channel_id'])
            if channel:
                valid_tickets.append(ticket)
            else:
                # Auto-close orphaned tickets
                ticket['status'] = 'closed'
                ticket['close_reason'] = 'Channel no longer exists (auto-cleaned)'
                ticket['closed_at'] = datetime.now(timezone.utc).isoformat()
                data_manager.save_ticket(ticket)

        if not valid_tickets:
            await ctx.send("No open tickets with active channels.")
            return

        embed = discord.Embed(title="Open Tickets", description=f"Found **{len(valid_tickets)}** open ticket(s)", color=discord.Color.blurple())
        for ticket in valid_tickets[:10]:
            creator = ctx.guild.get_member(ticket['creator_id'])
            creator_name = creator.mention if creator else f"<@{ticket['creator_id']}>"
            claimed = ""
            if ticket.get('claimed_by'):
                claimer = ctx.guild.get_member(ticket['claimed_by'])
                claimed = f"\nClaimed: {claimer.mention if claimer else 'Unknown'}"
            embed.add_field(
                name=f"Ticket #{ticket['ticket_id']}",
                value=f"Creator: {creator_name}\nCategory: {ticket.get('category', 'General')}\nChannel: <#{ticket['channel_id']}>{claimed}",
                inline=False
            )
        await ctx.send(embed=embed)


    @bot.command(name="add", description="Add a user or role to the current ticket")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(user="User to add to this ticket", role="Role to add to this ticket")
    async def ticket_add_cmd(ctx: commands.Context, user: Optional[discord.Member] = None, role: Optional[discord.Role] = None) -> None:
        """Add a user OR a role to the ticket (TicketTool $add accepts both)."""
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
            return
        if user is None and role is None:
            await _ticket_respond(ctx, "Mention a user or a role to add (e.g. `!add @user`).", ephemeral=True)
            return
        target = user or role
        try:
            await ctx.channel.set_permissions(
                target,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            await _ticket_respond(ctx, f"Could not add {target.mention}: {e}", ephemeral=True)
            return
        await _ticket_respond(ctx, embed=discord.Embed(
            description=f"✅ {target.mention} has been added to the ticket.",
            color=discord.Color.green()
        ))
        logging.info(f"[Tickets] {ctx.author} added {target} to ticket {ticket['ticket_id']}")


    @bot.command(name="remove", description="Remove a user or role from the current ticket")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(user="User to remove from this ticket", role="Role to remove from this ticket")
    async def ticket_remove_cmd(ctx: commands.Context, user: Optional[discord.Member] = None, role: Optional[discord.Role] = None) -> None:
        """Remove a user OR a role from the ticket (TicketTool $remove accepts both)."""
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
            return
        if user is None and role is None:
            await _ticket_respond(ctx, "Mention a user or a role to remove (e.g. `!remove @user`).", ephemeral=True)
            return
        if user is not None and user.id == ticket.get('creator_id'):
            await _ticket_respond(ctx, "You cannot remove the ticket creator.", ephemeral=True)
            return
        target = user or role
        try:
            await ctx.channel.set_permissions(target, overwrite=None)
        except (discord.Forbidden, discord.HTTPException) as e:
            await _ticket_respond(ctx, f"Could not remove {target.mention}: {e}", ephemeral=True)
            return
        await _ticket_respond(ctx, embed=discord.Embed(
            description=f"✅ {target.mention} has been removed from the ticket.",
            color=discord.Color.orange()
        ))
        logging.info(f"[Tickets] {ctx.author} removed {target} from ticket {ticket['ticket_id']}")


    @bot.command(name="rename", description="Rename the current ticket channel")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(name="New channel name (no spaces)")
    async def ticket_rename_cmd(ctx: commands.Context, *, name: str) -> None:
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
            return
        clean_name = ''.join(c if c.isalnum() or c == '-' else '-' for c in name.lower())[:50]
        old_name = ctx.channel.name
        await ctx.channel.edit(name=clean_name)
        try:
            await log_ticket_event(ctx.guild, 'renamed', ticket, actor=ctx.author,
                                   detail=f"`{old_name}` → `{clean_name}`")
        except Exception:
            pass
        await ctx.send(embed=discord.Embed(
            description=f"✅ Channel renamed from `{old_name}` → `{clean_name}`",
            color=discord.Color.green()
        ))


    @bot.command(name="move", description="Move the ticket to a different panel category")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(panel_id="Panel ID to move this ticket under")
    async def ticket_move_cmd(ctx: commands.Context, panel_id: str) -> None:
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
            return
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel or panel['guild_id'] != ctx.guild.id:
            await _ticket_respond(ctx, f"Panel `{panel_id}` not found in this server.", ephemeral=True)
            return
        category_id = panel.get('category_id')
        if not category_id:
            await _ticket_respond(ctx, "That panel has no category set.", ephemeral=True)
            return
        category = ctx.guild.get_channel(category_id)
        if not category:
            await _ticket_respond(ctx, "Category channel not found.", ephemeral=True)
            return
        await ctx.channel.edit(category=category)
        ticket['panel_id'] = panel_id
        ticket['category'] = panel.get('name', 'General')
        data_manager.save_ticket(ticket)
        await ctx.send(embed=discord.Embed(
            description=f"✅ Ticket moved to **{panel.get('name', 'Unknown')}** (category: {category.name})",
            color=discord.Color.green()
        ))


    @bot.command(name="note", description="Add a private staff note to this ticket")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(content="Note content (only staff can view these)")
    async def ticket_note_cmd(ctx: commands.Context, *, content: str) -> None:
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
            return
        import uuid
        note = {
            'note_id': str(uuid.uuid4())[:8],
            'ticket_id': ticket['ticket_id'],
            'guild_id': ctx.guild.id,
            'author_id': ctx.author.id,
            'content': content,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        data_manager.save_ticket_note(note)
        await _ticket_respond(ctx, embed=discord.Embed(
            title="📝 Note Saved",
            description=content,
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc)
        ).set_footer(text=f"By {ctx.author.display_name} • ID: {note['note_id']}"), ephemeral=True)


    @bot.command(name="notes", description="View all staff notes for this ticket")
    @commands.has_permissions(manage_channels=True)
    async def ticket_notes_cmd(ctx: commands.Context) -> None:
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
            return
        notes = data_manager.load_ticket_notes(ticket['ticket_id'])
        if not notes:
            await _ticket_respond(ctx, "No notes found for this ticket.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"📝 Notes for Ticket #{ticket['ticket_id']}",
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc)
        )
        for note in notes[:10]:
            author = ctx.guild.get_member(note['author_id'])
            author_name = author.display_name if author else f"<@{note['author_id']}>"
            created = note['created_at'][:16].replace('T', ' ')
            embed.add_field(
                name=f"Note {note['note_id']} — {author_name} at {created}",
                value=note['content'][:1024],
                inline=False
            )
        await _ticket_respond(ctx, embed=embed, ephemeral=True)


    @bot.command(name="priority", description="Set the priority of the current ticket")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(level="Priority level: low, normal, high, urgent")
    @app_commands.choices(level=[
        app_commands.Choice(name="🟢 Low", value="low"),
        app_commands.Choice(name="🔵 Normal", value="normal"),
        app_commands.Choice(name="🟠 High", value="high"),
        app_commands.Choice(name="🔴 Urgent", value="urgent"),
    ])
    async def ticket_priority_cmd(ctx: commands.Context, level: str) -> None:
        ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
        if not ticket:
            await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
            return
        ticket['priority'] = level
        data_manager.save_ticket(ticket)
        emoji = PRIORITY_EMOJIS.get(level, '')
        color = PRIORITY_COLORS.get(level, discord.Color.blue())
        try:
            await log_ticket_event(ctx.guild, 'priority', ticket, actor=ctx.author,
                                   detail=f"Priority set to **{level.capitalize()}**")
        except Exception:
            pass
        await ctx.send(embed=discord.Embed(
            description=f"{emoji} Ticket priority set to **{level.capitalize()}** by {ctx.author.mention}",
            color=color,
            timestamp=datetime.now(timezone.utc)
        ))


    @bot.command(name="reopen", description="Reopen a closed ticket")
    @commands.has_permissions(manage_channels=True)
    @app_commands.describe(ticket_id="The ticket ID to reopen")
    async def ticket_reopen_cmd(ctx: commands.Context, ticket_id: str) -> None:
        """Reopen a closed ticket by recreating its channel."""
        if not state.ticket_tool:
            await ctx.send("Ticket system not initialized.")
            return
        ticket = data_manager.load_ticket(ticket_id)
        if not ticket:
            await ctx.send(f"Ticket `{ticket_id}` not found.")
            return
        if ticket['guild_id'] != ctx.guild.id:
            await ctx.send("That ticket does not belong to this server.")
            return
        if ticket.get('status') == 'open':
            existing_channel = ctx.guild.get_channel(ticket['channel_id'])
            if existing_channel:
                await ctx.send(f"That ticket is already open: {existing_channel.mention}")
                return

        # Two-step tickets keep their channel after closing: reopen IN PLACE
        # (restore perms, move back, reset row) instead of recreating a channel.
        existing_channel = ctx.guild.get_channel(ticket.get('channel_id') or 0) if ticket.get('channel_id') else None
        if ticket.get('status') == 'closed' and existing_channel is not None:
            ok, message = await reopen_ticket_in_place(existing_channel, ctx.author)
            if ok:
                await ctx.send(message)
            else:
                await ctx.send(message)
            return

        # Rebuild the channel
        creator = ctx.guild.get_member(ticket['creator_id'])
        if not creator:
            await ctx.send("Cannot reopen — the original ticket creator is no longer in the server.")
            return

        # Use the panel if available, otherwise use default settings
        panel = data_manager.load_ticket_panel(ticket.get('panel_id', '')) or {}
        settings = data_manager.load_ticket_settings(ctx.guild.id) or {}
        # Fall back to config defaults so re-opened tickets also land in the
        # configured Tickets category (same fix as create_ticket).
        category_id = panel.get('category_id') or settings.get('category_id') or config.channels.tickets
        support_role_id = panel.get('support_role_id') or settings.get('support_role_id') or config.roles.ticket_support

        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            creator: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
            ctx.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        if support_role_id:
            role = ctx.guild.get_role(support_role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)

        category = ctx.guild.get_channel(category_id) if category_id else None
        # Unique channel name (same convention as create_ticket): the reopened
        # channel gets a fresh short suffix so it can't collide with a stale one.
        reopen_suffix = str(uuid.uuid4())[:8]
        channel_name = f"ticket-{creator.display_name}".lower()[:40]
        channel_name = ''.join(c if c.isalnum() or c == '-' else '-' for c in channel_name)
        channel_name = f"{channel_name}-{reopen_suffix}"[:90]

        try:
            new_channel = await ctx.guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"Ticket {ticket_id} (reopened) - {creator}"
            )
        except Exception as e:
            await ctx.send(f"Failed to create channel: {e}")
            return

        ticket['channel_id'] = new_channel.id
        ticket['status'] = 'open'
        ticket['closed_at'] = None
        ticket['closed_by'] = None
        ticket['close_reason'] = None
        data_manager.save_ticket(ticket)

        control_view = TicketControlView(ticket_id)
        await new_channel.send(
            embed=discord.Embed(
                title=f"🔓 Ticket Reopened — #{ticket_id}",
                description=f"This ticket was reopened by {ctx.author.mention}.\n{creator.mention} your ticket has been reopened.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            ),
            view=control_view
        )
        await ctx.send(f"Ticket `{ticket_id}` reopened: {new_channel.mention}")
        logging.info(f"[Tickets] {ctx.author} reopened ticket {ticket_id}")

        # TicketTool-style ticket logging: "Ticket Reopened" entry.
        try:
            await log_ticket_event(ctx.guild, 'reopened', ticket, actor=ctx.author)
        except Exception:
            pass

        # --- PREMIUM TIER 1: fire 'reopened' automations ---
        if PREMIUM_AVAILABLE:
            try:
                panel = data_manager.load_ticket_panel(ticket.get('panel_id') or '') if ticket.get('panel_id') else None
                await TicketTool.wiring.on_ticket_reopen(
                    bot=bot, ticket_tool=state.ticket_tool, ticket=ticket,
                    panel=panel, guild=ctx.guild,
                )
            except Exception as exc:
                logging.warning(f"[Premium] on_ticket_reopen failed: {exc}")


    @bot.command(name="ticketstats", description="View ticket statistics for this server")
    @commands.has_permissions(manage_channels=True)
    async def ticket_stats_cmd(ctx: commands.Context) -> None:
        stats = data_manager.load_ticket_stats(ctx.guild.id)
        embed = discord.Embed(
            title=f"📊 Ticket Statistics — {ctx.guild.name}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Total Tickets", value=str(stats['total']), inline=True)
        embed.add_field(name="Open", value=str(stats['open']), inline=True)
        embed.add_field(name="Closed", value=str(stats['closed']), inline=True)

        avg_rating = f"⭐ {stats['avg_rating']}" if stats['avg_rating'] else "No ratings yet"
        embed.add_field(name="Avg Rating", value=avg_rating, inline=True)

        avg_close = f"{stats['avg_close_hours']}h" if stats['avg_close_hours'] is not None else "N/A"
        embed.add_field(name="Avg Close Time", value=avg_close, inline=True)

        priorities = stats.get('priorities', {})
        if priorities:
            prio_text = "\n".join(
                f"{PRIORITY_EMOJIS.get(k, '')} {k.capitalize()}: {v}"
                for k, v in priorities.items()
            )
        else:
            prio_text = "No open tickets"
        embed.add_field(name="Open by Priority", value=prio_text, inline=False)

        await ctx.send(embed=embed)
