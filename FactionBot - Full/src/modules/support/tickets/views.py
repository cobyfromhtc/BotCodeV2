# -*- coding: utf-8 -*-
"""Ticket UI — panels, controls, modals, category manager views."""

# stdlib + discord.py
import asyncio
import discord
import logging
from datetime import datetime, timezone
from discord.ui import Button, Modal, Select, TextInput, View
from typing import Dict, List, Optional

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from core.ows import ows_get
from utils.ui.embeds import EmbedBuilder
from modules.support.tickets.engine import PANEL_BUTTON_PREFIX, PANEL_SELECT_CUSTOM_ID, UNCATEGORIZED_LABEL, _build_panel_message_embeds, _resolve_ticket_category_for_display, _ticket_category_label, _validate_category_emoji, build_ticket_commands_embed, log_ticket_event, process_panel_create_request, reopen_ticket_in_place




class TicketModeratorView(View):
    """TicketTool-style moderator message buttons on two-step closed tickets.

    PERSISTENCE: a single generic instance is registered in setup_hook; every
    handler resolves the ticket from the channel id, so the view keeps working
    after restarts.
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def _resolve_ticket(self, interaction: discord.Interaction) -> Optional[Dict]:
        if not state.ticket_tool:
            return None
        return await state.ticket_tool.data_manager.async_load_ticket_by_channel(interaction.channel.id)

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, 'guild_permissions', None)
        return bool(perms and (perms.administrator or perms.manage_channels))

    @discord.ui.button(label="Re-Open Ticket", style=discord.ButtonStyle.success, emoji="🔓", custom_id="ticketmod_reopen")
    async def reopen_button(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_staff(interaction):
            await interaction.response.send_message("Only staff can re-open tickets.", ephemeral=True)
            return
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        if ticket.get('status') != 'closed':
            await interaction.response.send_message("This ticket is not closed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        ok, message = await reopen_ticket_in_place(interaction.channel, interaction.user)
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Delete Ticket", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="ticketmod_delete")
    async def delete_button(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_staff(interaction):
            await interaction.response.send_message("Only staff can delete tickets.", ephemeral=True)
            return
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        # Log the deletion before the channel (and its context) disappears.
        try:
            await log_ticket_event(interaction.guild, 'deleted', ticket, actor=interaction.user)
        except Exception:
            pass
        try:
            await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
            await interaction.followup.send(f"Could not delete the ticket channel: {exc}", ephemeral=True)

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="ticketmod_transcript")
    async def transcript_button(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_staff(interaction):
            await interaction.response.send_message("Only staff can export transcripts.", ephemeral=True)
            return
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            transcript = await state.ticket_tool._generate_transcript(interaction.channel, ticket, interaction.user)
            settings = data_manager.load_ticket_settings(interaction.guild.id) or {}
            posted = False
            transcripts_channel_id = (
                settings.get('transcripts_channel_id') or getattr(config.channels, 'transcripts', None)
            )
            if transcripts_channel_id:
                transcripts_channel = interaction.guild.get_channel(int(transcripts_channel_id))
                if transcripts_channel:
                    await transcripts_channel.send(embed=transcript['embed'], file=transcript['file'])
                    posted = True
                    try:
                        await log_ticket_event(interaction.guild, 'transcript', ticket, actor=interaction.user)
                    except Exception:
                        pass
            await interaction.followup.send(
                "Transcript generated." + (" It has been posted to the transcripts channel." if posted else ""),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"Transcript generation failed: {exc}", ephemeral=True)


class CloseRequestView(View):
    """TicketTool-style close request: the ticket owner asks staff to close.

    Staff confirm the close with the reason; the requester (or staff) can
    cancel. Firing the premium `close_request` automation trigger is handled
    by the /closerequest command.
    """

    def __init__(self, ticket_id: str, requester_id: int, reason: str):
        super().__init__(timeout=300)
        self.ticket_id = ticket_id
        self.requester_id = requester_id
        self.reason = reason
        self.handled = False

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, 'guild_permissions', None)
        return bool(perms and (perms.administrator or perms.manage_channels))

    @discord.ui.button(label="✅ Close Ticket", style=discord.ButtonStyle.danger)
    async def confirm_close(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_staff(interaction):
            await interaction.response.send_message(
                "Only staff can action a close request. The requester can cancel it.",
                ephemeral=True,
            )
            return
        if self.handled:
            await interaction.response.defer()
            return
        self.handled = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Close request accepted by {interaction.user.mention}. Closing…",
            view=self,
        )
        if not state.ticket_tool:
            return
        await state.ticket_tool.close_ticket(interaction.channel, interaction.user, self.reason)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_request(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.requester_id and not self._is_staff(interaction):
            await interaction.response.send_message(
                "Only the requester or staff can cancel this close request.",
                ephemeral=True,
            )
            return
        if self.handled:
            await interaction.response.defer()
            return
        self.handled = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Close request cancelled.", view=self,
        )


class MultiPanelView(View):
    """TicketTool-style multi-panel (Attached Panels): up to 25 panels
    combined into ONE message, each contributing its own create button.

    Buttons reuse the standard `create_ticket:{panel_id}` custom_ids, so
    persistence routing and the shared gate flow are identical to
    single-panel messages.
    """

    def __init__(self, panels: List[Dict], per_row: int = 5):
        super().__init__(timeout=None)
        self.panels = panels
        per_row = max(1, min(5, int(per_row or 5)))
        for i, panel in enumerate(panels[:25]):
            row = min(i // per_row, 4)
            btn = Button(
                style=discord.ButtonStyle(panel.get('button_style', 3)),
                label=(panel.get('button_label') or panel.get('name') or 'Create Ticket')[:80],
                emoji=panel.get('button_emoji') or None,
                custom_id=f"{PANEL_BUTTON_PREFIX}{panel['panel_id']}",
                row=row,
            )
            btn.callback = self._on_click
            self.add_item(btn)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        # For assigned callbacks discord.py does not pass the clicked item,
        # so read the custom_id from the interaction payload (attribute on
        # modern ComponentInteractionData, dict-style in older builds).
        data = getattr(interaction, 'data', None)
        custom_id = getattr(data, 'custom_id', None)
        if custom_id is None and isinstance(data, dict):
            custom_id = data.get('custom_id')
        if custom_id and custom_id.startswith(PANEL_BUTTON_PREFIX):
            panel_id = custom_id[len(PANEL_BUTTON_PREFIX):]
            panel = None
            if state.ticket_tool:
                panel = state.ticket_tool.data_manager.load_ticket_panel(panel_id)
            if panel:
                await process_panel_create_request(interaction, panel)
                return
            await interaction.response.send_message("This ticket panel no longer exists.", ephemeral=True)
            return
        await interaction.response.defer()


class TicketPanelSelectView(View):
    """TicketTool-style dropdown panel: a Discord select menu where each
    option routes to a different panel (per-option: label, description,
    emoji). Placeholder text is configurable.

    PERSISTENCE: the select uses the stable custom_id `ticket_panel_select`,
    so interactions after a restart are dispatched to the generic instance
    registered in setup_hook; the selected panel is re-resolved from the DB
    at click time, keeping option labels/descriptions fresh via /panelupdate.
    """

    def __init__(self, panels: List[Dict], placeholder: str = "Select a ticket type…"):
        super().__init__(timeout=None)
        self.panels = panels
        options = []
        for panel in panels[:25]:
            label = (panel.get('name') or panel.get('button_label') or 'Panel')[:100]
            description = (panel.get('embed_description') or '')[:100] or None
            options.append(discord.SelectOption(
                label=label,
                value=str(panel['panel_id']),
                description=description,
                emoji=panel.get('button_emoji') or None,
            ))
        if not options:
            # discord.py requires at least one option at construction; the
            # generic persistent instance uses a placeholder that is never
            # selectable in practice (real views always have panels).
            options.append(discord.SelectOption(label='Tickets', value='__none__'))
        self.select = Select(
            placeholder=(placeholder or 'Select a ticket type…')[:150],
            options=options,
            custom_id=PANEL_SELECT_CUSTOM_ID,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        selected = self.select.values[0] if self.select.values else None
        if not selected:
            await interaction.response.defer()
            return
        panel = None
        if state.ticket_tool:
            panel = state.ticket_tool.data_manager.load_ticket_panel(selected)
        if not panel:
            await interaction.response.send_message(
                "This ticket panel no longer exists. Use /panelupdate to refresh the menu.",
                ephemeral=True,
            )
            return
        await process_panel_create_request(interaction, panel)


class TicketPanelView(View):
    """The panel that users interact with to create tickets.

    PERSISTENCE FIX: each panel's button uses a UNIQUE custom_id of the form
    `create_ticket:{panel_id}`. Previously every panel used the same
    `create_ticket_button` custom_id, so discord.py could only keep ONE
    registered panel config after restart — all panel messages would then
    route to that single panel's config. With per-panel custom_ids, each
    panel message correctly resolves to its own panel even after restart.
    """

    def __init__(self, panel: Dict):
        super().__init__(timeout=None)
        self.panel = panel
        panel_id = panel.get('panel_id', 'unknown')

        # Configure button based on panel settings, AND give it a unique
        # custom_id so persistence routing is unambiguous.
        self.create_button.style = discord.ButtonStyle(panel.get('button_style', 3))
        self.create_button.label = panel.get('button_label', 'Create Ticket')
        if panel.get('button_emoji'):
            self.create_button.emoji = panel['button_emoji']
        self.create_button.custom_id = f"{PANEL_BUTTON_PREFIX}{panel_id}"

    @staticmethod
    def _panel_id_from_custom_id(custom_id: str) -> Optional[str]:
        if custom_id and custom_id.startswith(PANEL_BUTTON_PREFIX):
            return custom_id[len(PANEL_BUTTON_PREFIX):]
        return None

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.success, custom_id="create_ticket:placeholder")
    async def create_button(self, interaction: discord.Interaction, button: Button) -> None:
        panel_id = self._panel_id_from_custom_id(button.custom_id)
        panel = None
        if panel_id and state.ticket_tool:
            panel = state.ticket_tool.data_manager.load_ticket_panel(panel_id)
        if not panel:
            panel = self.panel
        await process_panel_create_request(interaction, panel)

    async def _send_welcome_message(self, channel: discord.TextChannel, user: discord.Member, panel: Dict) -> None:
        """Send the welcome message in the ticket channel."""
        ticket = await state.ticket_tool.data_manager.async_load_ticket_by_channel(channel.id)
        if not ticket:
            return

        view = TicketControlView(ticket['ticket_id'])

        # Show the available ticket commands FIRST, before the welcome message.
        try:
            await channel.send(embed=build_ticket_commands_embed(panel))
        except discord.DiscordException as e:
            logging.warning(f"[Tickets] Could not send ticket-commands embed: {e}")

        welcome_text = panel.get('welcome_message', "Support will be with you shortly.")

        # Type = the panel name (ticket type); Category = the internal
        # Ticket Category folder inherited from the panel (Uncategorized when
        # the panel has none / it was created before the feature).
        category_line = _resolve_ticket_category_for_display(channel.guild.id, ticket)
        embed = discord.Embed(
            title=f"Ticket #{ticket['ticket_id']}",
            description=(
                f"Welcome {user.mention}!\n\n{welcome_text}\n\n"
                f"**Type:** {panel.get('name', 'General')}\n"
                f"**Category:** {category_line}"
            ),
            color=discord.Color(panel.get('embed_color', 0x5865F2)),
            timestamp=datetime.now(timezone.utc)
        )

        if panel.get('embed_thumbnail'):
            embed.set_thumbnail(url=panel['embed_thumbnail'])
        if panel.get('embed_image'):
            embed.set_image(url=panel['embed_image'])

        embed.set_footer(text=f"Created by {user.display_name}")

        mention_text = ""
        guild_settings = None
        try:
            guild_settings = data_manager.load_ticket_settings(channel.guild.id)
        except Exception:
            guild_settings = None
        if ows_get("mention_support_on_create") and (guild_settings.get('mention_on_create', 1) if guild_settings else True):
            support_role_id = panel.get('support_role_id')
            if support_role_id:
                role = channel.guild.get_role(support_role_id)
                if role:
                    mention_text = f"{role.mention} "

        welcome_msg = await channel.send(f"{mention_text}{user.mention}", embed=embed, view=view)

        # TicketTool "Auto Pin Ticket": pin the ticket message so the control
        # buttons stay reachable. Gated by the OWS pin_ticket_message toggle.
        if ows_get("pin_ticket_message"):
            try:
                await welcome_msg.pin(reason="Ticket message pinned (control buttons)")
            except (discord.Forbidden, discord.HTTPException):
                pass


class TicketQuestionsModal(Modal, title="Create Ticket"):
    """Modal for ticket questions."""
    
    def __init__(self, panel: Dict, questions: List[Dict]):
        super().__init__()
        self.panel = panel
        self.questions = questions
        self.answers = {}
        
        for i, q in enumerate(questions[:5]):  # Discord limits to 5 items
            text_input = TextInput(
                label=q['question_text'][:45],
                placeholder=q.get('placeholder', ''),
                style=discord.TextStyle.paragraph if q.get('question_type') == 'paragraph' else discord.TextStyle.short,
                required=q.get('required', True),
                custom_id=f"question_{q['question_id']}"
            )
            self.add_item(text_input)
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Collect answers
        answers = {}
        for child in self.children:
            qid = child.custom_id.replace('question_', '')
            answers[qid] = child.value
        
        # Create ticket
        channel, ticket_id = await state.ticket_tool.create_ticket(
            interaction.guild, interaction.user, self.panel, answers=answers
        )
        
        if channel:
            # Send welcome message with answers
            await self._send_welcome_with_answers(channel, interaction.user, answers)
            await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)
        else:
            await interaction.followup.send(f"Failed to create ticket: {ticket_id}", ephemeral=True)
    
    async def _send_welcome_with_answers(self, channel: discord.TextChannel, user: discord.Member, answers: Dict) -> None:
        """Send welcome message with question answers."""
        ticket = state.ticket_tool.data_manager.load_ticket_by_channel(channel.id)
        if not ticket:
            return

        view = TicketControlView(ticket['ticket_id'])

        # Show the available ticket commands FIRST, before the welcome message.
        try:
            await channel.send(embed=build_ticket_commands_embed(self.panel))
        except discord.DiscordException as e:
            logging.warning(f"[Tickets] Could not send ticket-commands embed: {e}")

        welcome_text = self.panel.get('welcome_message', "Support will be with you shortly.")
        
        embed = discord.Embed(
            title=f"Ticket #{ticket['ticket_id']}",
            color=discord.Color(self.panel.get('embed_color', 0x5865F2)),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Creator", value=user.mention, inline=True)
        embed.add_field(name="Type", value=self.panel.get('name', 'General'), inline=True)
        embed.add_field(
            name="Category",
            value=_resolve_ticket_category_for_display(channel.guild.id, ticket),
            inline=True,
        )
        
        # Add answers - keys are bare question IDs (prefix stripped in on_submit)
        for q in self.questions:
            answer = answers.get(q['question_id'], 'No answer')
            embed.add_field(name=q['question_text'][:256], value=answer[:1024], inline=False)
        
        embed.add_field(name="Info", value=welcome_text, inline=False)
        embed.set_footer(text=f"Created by {user.display_name}")
        
        # Mention gate now matches _send_welcome_message: the support-role ping
        # respects BOTH the OWS toggle and the ticket_settings column
        # (previously this variant pinged unconditionally).
        mention_text = ""
        if ows_get("mention_support_on_create"):
            settings = data_manager.load_ticket_settings(channel.guild.id)
            if settings is None or settings.get('mention_on_create', 1):
                support_role_id = self.panel.get('support_role_id')
                if support_role_id:
                    role = channel.guild.get_role(support_role_id)
                    if role:
                        mention_text = f"{role.mention} "

        welcome_msg = await channel.send(f"{mention_text}{user.mention}", embed=embed, view=view)

        # TicketTool "Auto Pin Ticket" (gated by the OWS toggle).
        if ows_get("pin_ticket_message"):
            try:
                await welcome_msg.pin(reason="Ticket message pinned (control buttons)")
            except (discord.Forbidden, discord.HTTPException):
                pass


# --- TICKET CONTROL VIEW (Inside ticket channels) ---
class TicketControlView(View):
    """Buttons inside a ticket channel for control.

    PERSISTENCE FIX: on startup a single `TicketControlView("")` is
    registered (see on_ready). After restart, `self.ticket_id` is the empty
    string, so any handler that used `self.ticket_id` directly (close,
    transcript, note, priority) would fail. Every handler now resolves the
    ticket from `interaction.channel.id` via the DB, which is always correct
    regardless of what the view instance was constructed with.
    """

    def __init__(self, ticket_id: str = ""):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id

    async def _resolve_ticket(self, interaction: discord.Interaction) -> Optional[Dict]:
        """Look up the ticket for the current channel (robust after restart)."""
        if not state.ticket_tool:
            return None
        ticket_id = self.ticket_id
        ticket = None
        if ticket_id:
            ticket = await state.ticket_tool.data_manager.async_load_ticket(ticket_id)
        if not ticket and interaction.channel:
            # Fallback: resolve by channel — always correct inside a ticket channel.
            ticket = await state.ticket_tool.data_manager.async_load_ticket_by_channel(interaction.channel.id)
        return ticket

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="🙋", custom_id="ticket_claim")
    async def claim_button(self, interaction: discord.Interaction, button: Button) -> None:
        if not state.ticket_tool:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("System Not Ready", "The ticket system is still starting up. Try again in a moment."),
                ephemeral=True,
            )
            return

        # Defer first so a slow DB write never surfaces as "interaction failed".
        await interaction.response.defer(thinking=True, ephemeral=True)

        success, message = await state.ticket_tool.claim_ticket(interaction.channel, interaction.user)

        if success:
            button.disabled = True
            # Show WHO claimed right on the button so it's obvious at a glance.
            claim_label = f"Claimed by {interaction.user.display_name}"
            if len(claim_label) > 80:
                claim_label = claim_label[:77] + "..."
            button.label = claim_label
            button.style = discord.ButtonStyle.secondary
            button.emoji = "✅"
            try:
                await interaction.message.edit(view=self)
            except (discord.HTTPException, AttributeError):
                pass
            await interaction.channel.send(
                embed=discord.Embed(
                    description=f"🙋 {interaction.user.mention} claimed this ticket.",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc),
                )
            )
            await interaction.followup.send(
                embed=EmbedBuilder.success("Ticket Claimed", f"You now own this ticket. Use `!unclaim` to release it."),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=EmbedBuilder.warning("Could Not Claim", message or "This ticket could not be claimed."),
                ephemeral=True,
            )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket_close")
    async def close_button(self, interaction: discord.Interaction, button: Button) -> None:
        # Resolve the real ticket_id from the channel so closing works even
        # when this view instance was the empty-string startup registration.
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Ticket Not Found", "This channel doesn't appear to be an active ticket."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(CloseTicketModal(ticket['ticket_id']))

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="ticket_transcript")
    async def transcript_button(self, interaction: discord.Interaction, button: Button) -> None:
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Ticket Not Found", "This channel doesn't appear to be an active ticket."),
                ephemeral=True,
            )
            return
        # Defer so the (potentially slow) transcript generation never surfaces
        # as an "interaction failed" error to the user.
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            transcript = await state.ticket_tool._generate_transcript(
                interaction.channel, ticket, interaction.user
            )
            await interaction.followup.send(
                embed=transcript['embed'],
                file=transcript['file'],
                ephemeral=True,
            )
        except Exception as exc:
            logging.exception("[Tickets] transcript button failed: %s", exc)
            await interaction.followup.send(
                embed=EmbedBuilder.error("Transcript Failed", f"Could not generate the transcript: `{exc}`"),
                ephemeral=True,
            )

    @discord.ui.button(label="Note", style=discord.ButtonStyle.secondary, emoji="📝", row=1, custom_id="ticket_note")
    async def note_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Add a staff note. See AddNoteModal — notes are now stored privately
        (ephemeral confirmation only, no public channel post)."""
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Ticket Not Found", "This channel doesn't appear to be an active ticket."),
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Staff Only", "Only staff with Manage Channels permission can add notes."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(AddNoteModal(ticket['ticket_id']))

    @discord.ui.button(label="Priority", style=discord.ButtonStyle.secondary, emoji="🚨", row=1, custom_id="ticket_priority")
    async def priority_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Set the priority of this ticket."""
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Staff Only", "Only staff with Manage Channels permission can set priority."),
                ephemeral=True,
            )
            return
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Ticket Not Found", "This channel doesn't appear to be an active ticket."),
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="🚨 Set Ticket Priority",
            description="Choose a priority level below. This updates the ticket status and notifies staff.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Ticket #{ticket['ticket_id']} • Requested by {interaction.user.display_name}")
        await interaction.response.send_message(
            embed=embed,
            view=PrioritySelectView(ticket['ticket_id']),
            ephemeral=True,
        )

    @discord.ui.button(label="Category", style=discord.ButtonStyle.secondary, emoji="📁", row=1, custom_id="ticket_category")
    async def category_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Set the Ticket Category (internal folder) of this ticket.

        Works on ANY ticket — including ones created before Ticket Categories
        existed (they start as Uncategorized). Changing the category only
        updates the ticket's DB association; the Discord channel, claim info,
        transcripts and all other metadata are untouched.
        """
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Staff Only", "Only staff with Manage Channels permission can set the ticket category."),
                ephemeral=True,
            )
            return
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Ticket Not Found", "This channel doesn't appear to be an active ticket."),
                ephemeral=True,
            )
            return
        categories = data_manager.load_ticket_categories(interaction.guild.id)
        if not categories:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning(
                    "No Categories Yet",
                    "No ticket categories exist yet — create one with `!tcategory` first.",
                ),
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="📁 Set Ticket Category",
            description="Select a category (or remove the current one) from the menu below.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Ticket #{ticket['ticket_id']} • Requested by {interaction.user.display_name}")
        await interaction.response.send_message(
            embed=embed,
            view=TicketCategorySelectView(ticket['ticket_id'], interaction.guild.id),
            ephemeral=True,
        )


class CloseTicketModal(Modal, title="🔒 Close Ticket"):
    reason_input = TextInput(
        label="Close Reason (optional)",
        placeholder="e.g. Issue resolved, user no longer needs help…",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )
    
    def __init__(self, ticket_id: str):
        super().__init__()
        self.ticket_id = ticket_id
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = self.reason_input.value.strip() or "No reason provided"
        
        ticket = state.ticket_tool.data_manager.load_ticket(self.ticket_id)
        is_creator = ticket and ticket.get('creator_id') == interaction.user.id
        
        if is_creator and ows_get("ticket_rating_prompt"):
            view = TicketRatingView(self.ticket_id, reason, interaction.channel, interaction.user)
            rating_embed = discord.Embed(
                title="⭐ Rate Your Support Experience",
                description=(
                    "Before we close your ticket, please rate the support you received.\n\n"
                    "Your feedback helps us improve and recognize great staff! 💛"
                ),
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc),
            )
            rating_embed.set_footer(text=f"Ticket #{self.ticket_id} • Closes automatically in 2 min")
            await interaction.response.send_message(
                embed=rating_embed,
                view=view,
                ephemeral=True
            )
        else:
            view = ConfirmCloseView(self.ticket_id, reason)
            confirm_embed = discord.Embed(
                title="🔒 Confirm Ticket Closure",
                description=(
                    f"You're about to close **Ticket #{self.ticket_id}**.\n\n"
                    f"**Reason:** {reason}"
                ),
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            confirm_embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            await interaction.response.send_message(
                embed=confirm_embed,
                view=view,
                ephemeral=True
            )


class ConfirmCloseView(View):
    """Confirmation view for staff closing tickets."""
    
    def __init__(self, ticket_id: str, reason: str):
        super().__init__(timeout=120)
        self.ticket_id = ticket_id
        self.reason = reason
    
    async def on_timeout(self) -> None:
        """Disable all buttons when the confirmation times out (no silent close)."""
        for child in self.children:
            child.disabled = True
    
    @discord.ui.button(label="✅ Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm_close(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Disable buttons immediately to prevent double-clicks.
        for child in self.children:
            child.disabled = True
        button.label = "Closing…"
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass
        
        # Now close the ticket
        success = await state.ticket_tool.close_ticket(interaction.channel, interaction.user, self.reason)
        if not success:
            await interaction.followup.send(
                embed=EmbedBuilder.error("Close Failed", "The ticket could not be closed. Check my permissions and try again."),
                ephemeral=True,
            )
    
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_close(self, interaction: discord.Interaction, button: Button) -> None:
        for child in self.children:
            child.disabled = True
        cancel_embed = EmbedBuilder.info("Close Cancelled", "The ticket was not closed. You can reopen this prompt anytime.")
        await interaction.response.edit_message(
            embed=cancel_embed,
            view=self
        )


class TicketRatingView(View):
    """Rating view BEFORE ticket close - gives user time to rate.

    Star buttons are color-graded so the rating scale reads at a glance:
    red (1-2 = poor), orange (3 = okay), green (4-5 = great). The 5-star
    option uses the success style to draw the eye to the "ideal" rating.
    """

    # Themed color per rating tier (used for the thank-you embed).
    _RATING_COLORS = {
        1: discord.Color.red(),
        2: discord.Color.red(),
        3: discord.Color.orange(),
        4: discord.Color.green(),
        5: discord.Color.green(),
    }
    _RATING_LABELS = {
        1: "Very Poor",
        2: "Poor",
        3: "Okay",
        4: "Good",
        5: "Excellent",
    }
    
    def __init__(self, ticket_id: str, reason: str, channel: discord.TextChannel, user: discord.Member):
        super().__init__(timeout=120)  # 2 minutes to rate
        self.ticket_id = ticket_id
        self.reason = reason
        self.channel = channel
        self.user = user
        self.rated = False
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the user who triggered the close may rate / skip.

        Previously the view was posted non-ephemerally with no user check, so
        ANY channel member could click the stars or force the close.
        """
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning(
                    "Not Your Prompt",
                    "Only the member closing this ticket can submit a rating.",
                ),
                ephemeral=True,
            )
            return False
        return True
    
    async def on_timeout(self) -> None:
        """Close ticket after timeout if not rated."""
        if not self.rated and self.channel:
            try:
                await state.ticket_tool.close_ticket(self.channel, self.user, self.reason)
            except Exception:
                pass  # Channel might already be deleted
    
    @discord.ui.button(label="⭐", style=discord.ButtonStyle.danger, row=0)
    async def rate_1(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit_rating(interaction, 1)
    
    @discord.ui.button(label="⭐⭐", style=discord.ButtonStyle.danger, row=0)
    async def rate_2(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit_rating(interaction, 2)
    
    @discord.ui.button(label="⭐⭐⭐", style=discord.ButtonStyle.secondary, row=0)
    async def rate_3(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit_rating(interaction, 3)
    
    @discord.ui.button(label="⭐⭐⭐⭐", style=discord.ButtonStyle.success, row=1)
    async def rate_4(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit_rating(interaction, 4)
    
    @discord.ui.button(label="⭐⭐⭐⭐⭐", style=discord.ButtonStyle.success, row=1)
    async def rate_5(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit_rating(interaction, 5)
    
    @discord.ui.button(label="⏭️ Skip & Close", style=discord.ButtonStyle.secondary, row=2)
    async def skip_rating(self, interaction: discord.Interaction, button: Button) -> None:
        self.rated = True
        await interaction.response.defer(thinking=True)
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(
                embed=EmbedBuilder.info("Closing Without Rating", "No rating recorded. Closing your ticket now…"),
                view=self
            )
        except discord.HTTPException:
            pass
        
        # Close the ticket
        success = await state.ticket_tool.close_ticket(self.channel, self.user, self.reason)
        if not success:
            await interaction.followup.send(
                embed=EmbedBuilder.error("Close Failed", "The ticket could not be closed. Check my permissions and try again."),
                ephemeral=True,
            )
    
    async def _submit_rating(self, interaction: discord.Interaction, rating: int) -> None:
        # Prevent double-submit (e.g. a second star click while closing).
        if self.rated:
            await interaction.response.defer(ephemeral=True)
            return
        self.rated = True
        
        # Save rating
        ticket = state.ticket_tool.data_manager.load_ticket(self.ticket_id)
        if ticket:
            ticket['rating'] = rating
            state.ticket_tool.data_manager.save_ticket(ticket)
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True
        
        color = self._RATING_COLORS.get(rating, discord.Color.gold())
        label = self._RATING_LABELS.get(rating, "Rated")
        stars = "⭐" * rating
        thanks = discord.Embed(
            title="💛 Thank You for Your Feedback!",
            description=(
                f"You rated your support: **{stars}**\n"
                f"_{label}_\n\n"
                f"Closing your ticket in 3 seconds…"
            ),
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        thanks.set_footer(text=f"Ticket #{self.ticket_id}")
        await interaction.response.edit_message(
            embed=thanks,
            view=self
        )
        
        # Wait a moment then close
        await asyncio.sleep(3)
        
        # Close the ticket
        try:
            await state.ticket_tool.close_ticket(self.channel, self.user, self.reason)
        except Exception:
            pass  # Channel might already be deleted


# --- ADD NOTE MODAL ---
class AddNoteModal(Modal, title="📝 Add Staff Note"):
    note_input = TextInput(
        label="Note (only staff can see this)",
        style=discord.TextStyle.paragraph,
        placeholder="Internal note visible only to staff...",
        max_length=1000,
        required=True
    )

    def __init__(self, ticket_id: str):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        import uuid
        note = {
            'note_id': str(uuid.uuid4())[:8],
            'ticket_id': self.ticket_id,
            'guild_id': interaction.guild.id,
            'author_id': interaction.user.id,
            'content': self.note_input.value,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        await data_manager.async_save_ticket_note(note)

        confirm_embed = discord.Embed(
            title="📝 Staff Note Added",
            description="Your note has been saved privately and is visible to staff via `!notes`.\n\n"
                       f"```\n{self.note_input.value[:1800]}\n```",
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc)
        )
        confirm_embed.add_field(name="Ticket", value=f"#{self.ticket_id}", inline=True)
        confirm_embed.add_field(name="Note ID", value=note['note_id'], inline=True)
        confirm_embed.set_footer(text=f"Added by {interaction.user.display_name} • Note ID: {note['note_id']}")

        # PRIVACY FIX: the previous implementation ALSO posted the note content
        # to the ticket channel via `interaction.channel.send(embed=...)`.
        # Because the ticket creator can read that channel, the "private"
        # staff note was anything but private. We now ONLY send the ephemeral
        # confirmation to the staff member and persist the note to the DB,
        # where it can be reviewed with the `!notes` command (staff-only).
        # The transcript still records the note via the saved DB row when
        # generated for staff review.
        await interaction.response.send_message(embed=confirm_embed, ephemeral=True)

        # Optional: forward to a dedicated staff-notes channel if one is
        # configured in ticket settings (`notes_channel_id`). This keeps the
        # note out of the ticket channel the creator can read, while still
        # giving staff a shared place to see new notes.
        settings = data_manager.load_ticket_settings(interaction.guild.id)
        notes_channel_id = settings.get('notes_channel_id') if settings else None
        if notes_channel_id:
            notes_channel = interaction.guild.get_channel(notes_channel_id)
            if notes_channel:
                try:
                    staff_embed = discord.Embed(
                        title=f"📝 Staff Note • Ticket {self.ticket_id}",
                        description=self.note_input.value,
                        color=discord.Color.yellow(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    staff_embed.set_footer(text=f"By {interaction.user.display_name}")
                    await notes_channel.send(embed=staff_embed)
                except Exception as e:
                    logging.warning(f"[Tickets] Could not post note to staff notes channel: {e}")


# --- PRIORITY SELECT VIEW ---
PRIORITY_COLORS = {
    'low':    discord.Color.green(),
    'normal': discord.Color.blue(),
    'high':   discord.Color.orange(),
    'urgent': discord.Color.red(),
}
PRIORITY_EMOJIS = {'low': '🟢', 'normal': '🔵', 'high': '🟠', 'urgent': '🔴'}


class PrioritySelectView(View):
    def __init__(self, ticket_id: str):
        super().__init__(timeout=60)
        self.ticket_id = ticket_id

    @discord.ui.button(label="🟢 Low", style=discord.ButtonStyle.success)
    async def low(self, interaction: discord.Interaction, button: Button) -> None:
        await self._set_priority(interaction, 'low')

    @discord.ui.button(label="🔵 Normal", style=discord.ButtonStyle.primary)
    async def normal(self, interaction: discord.Interaction, button: Button) -> None:
        await self._set_priority(interaction, 'normal')

    @discord.ui.button(label="🟠 High", style=discord.ButtonStyle.secondary)
    async def high(self, interaction: discord.Interaction, button: Button) -> None:
        await self._set_priority(interaction, 'high')

    @discord.ui.button(label="🔴 Urgent", style=discord.ButtonStyle.danger)
    async def urgent(self, interaction: discord.Interaction, button: Button) -> None:
        await self._set_priority(interaction, 'urgent')

    async def _set_priority(self, interaction: discord.Interaction, priority: str) -> None:
        ticket = data_manager.load_ticket(self.ticket_id)
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        ticket['priority'] = priority
        data_manager.save_ticket(ticket)

        emoji = PRIORITY_EMOJIS.get(priority, '')
        color = PRIORITY_COLORS.get(priority, discord.Color.blue())
        try:
            await log_ticket_event(interaction.guild, 'priority', ticket, actor=interaction.user,
                                   detail=f"Priority set to **{priority.capitalize()}**")
        except Exception:
            pass
        embed = discord.Embed(
            title=f"{emoji} Priority Set: {priority.capitalize()}",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Set by {interaction.user.display_name}")
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        await interaction.channel.send(
            embed=discord.Embed(
                description=f"{emoji} Ticket priority set to **{priority.capitalize()}** by {interaction.user.mention}",
                color=color
            )
        )
        self.stop()


class TicketCategorySelectView(View):
    """Ephemeral select menu for changing a ticket's Ticket Category (folder).

    Shown from the ticket-channel 📁 Category button and from `!setcategory`.
    Includes a "None (Uncategorized)" option so the category can be removed
    or reverted. Works for tickets created before the feature — they simply
    have no category yet.
    """
    _NONE_VALUE = '__none__'

    def __init__(self, ticket_id: str, guild_id: int):
        super().__init__(timeout=120)
        self.ticket_id = ticket_id
        self.guild_id = guild_id
        self._build_select()

    def _build_select(self) -> None:
        self.clear_items()
        options = [discord.SelectOption(
            label='None (Uncategorized)',
            value=self._NONE_VALUE,
            description="Remove this ticket's category",
            emoji='📁',
        )]
        for cat in data_manager.load_ticket_categories(self.guild_id)[:24]:
            emoji = (cat.get('emoji') or '').strip()
            option = discord.SelectOption(
                label=str(cat.get('name', 'Category'))[:100],
                value=cat['category_id'],
                description=(str(cat.get('description'))[:100] or None) if cat.get('description') else None,
                emoji=emoji or None,
            )
            options.append(option)
        select = Select(
            placeholder='Choose a ticket category…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_category_selected
        self.add_item(select)

    async def on_category_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        raw = select.values[0]
        category_id = None if raw == self._NONE_VALUE else raw

        # Single-column atomic UPDATE — every other ticket field is preserved.
        updated = await data_manager.async_set_ticket_category(self.ticket_id, category_id)
        if not updated:
            await interaction.response.edit_message(
                content="Ticket not found — it may have been deleted.",
                view=None,
            )
            return

        label = _ticket_category_label(self.guild_id, category_id)
        ticket = await data_manager.async_load_ticket(self.ticket_id)
        if ticket is not None:
            try:
                await log_ticket_event(
                    interaction.guild, 'category', ticket,
                    actor=interaction.user,
                    detail=f"Ticket category set to **{label}**",
                )
            except Exception:
                pass

        embed = discord.Embed(
            title=f"📁 Ticket Category Set: {label}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Set by {interaction.user.display_name}")
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        # Public in-channel announcement (mirrors the priority flow).
        try:
            await interaction.channel.send(
                embed=discord.Embed(
                    description=(
                        f"📁 Ticket category set to **{label}** "
                        f"by {interaction.user.mention}"
                    ),
                    color=discord.Color.blurple(),
                )
            )
        except discord.DiscordException:
            pass
        self.stop()


# --- PANEL CREATOR VIEW (For creating/editing panels) ---
class PanelCreatorView(View):
    """Interactive panel creator."""
    
    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.user_id = user_id
        self.panel_data = {
            'panel_id': str(uuid.uuid4())[:8],
            'guild_id': guild_id,
            'name': 'New Panel',
            'embed_title': 'Support Tickets',
            'embed_description': 'Click the button below to create a ticket.',
            'embed_color': 0x5865F2,
            'button_label': 'Create Ticket',
            'button_style': 3,
            'ticket_limit': 3,
            'auto_close_hours': 24,
            'welcome_message': 'Support will be with you shortly.',
            'created_at': datetime.now(timezone.utc).isoformat()
        }
    
    def _create_preview_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.panel_data['embed_title'],
            description=self.panel_data['embed_description'],
            color=discord.Color(self.panel_data['embed_color'])
        )
        # Show which Ticket Category (internal folder) tickets from this
        # panel will be filed under.
        embed.add_field(
            name="Ticket Category",
            value=_ticket_category_label(self.guild_id, self.panel_data.get('ticket_category_id')),
            inline=False,
        )
        embed.set_footer(text=f"Panel: {self.panel_data['name']}")
        return embed
    
    @discord.ui.button(label="Set Name", style=discord.ButtonStyle.primary, emoji="🏷️", row=0)
    async def set_name(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        await interaction.response.send_modal(PanelNameModal(self))
    
    @discord.ui.button(label="Set Embed", style=discord.ButtonStyle.primary, emoji="🎨", row=0)
    async def set_embed(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        await interaction.response.send_modal(PanelEmbedModal(self))
    
    @discord.ui.button(label="Set Button", style=discord.ButtonStyle.secondary, emoji="🔘", row=0)
    async def set_button(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        await interaction.response.send_modal(PanelButtonModal(self))
    
    @discord.ui.button(label="Settings", style=discord.ButtonStyle.secondary, emoji="⚙️", row=1)
    async def settings(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        await interaction.response.send_modal(PanelSettingsModal(self))
    
    @discord.ui.button(label="Category & Role", style=discord.ButtonStyle.secondary, emoji="🎯", row=1)
    async def target(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        await interaction.response.send_modal(PanelTargetModal(self))
    
    @discord.ui.button(label="Preview", style=discord.ButtonStyle.success, emoji="👁️", row=1)
    async def preview(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        
        preview_view = View(timeout=30)
        preview_view.add_item(Button(
            label=self.panel_data['button_label'],
            style=discord.ButtonStyle(self.panel_data['button_style']),
            disabled=True
        ))
        
        await interaction.response.send_message(
            embed=self._create_preview_embed(),
            view=preview_view,
            ephemeral=True
        )
    
    @discord.ui.button(label="Ticket Category", style=discord.ButtonStyle.primary, emoji="📁", row=2)
    async def ticket_category(self, interaction: discord.Interaction, button: Button) -> None:
        """Pick the Ticket Category (internal folder) this panel's tickets
        will belong to. Uses an ephemeral select so no typing is needed."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        categories = data_manager.load_ticket_categories(self.guild_id)
        if not categories:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning(
                    "No Categories Yet",
                    "No ticket categories exist yet. Create one first with `!tcategory`, "
                    "then come back — tickets from this panel will currently be **Uncategorized**.",
                ),
                ephemeral=True,
            )
            return
        cat_embed = discord.Embed(
            title="📁 Choose Ticket Category",
            description="Select the Ticket Category for this panel. Tickets created from it will be grouped in that folder.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        cat_embed.set_footer(text=f"Panel Builder • {interaction.user.display_name}")
        await interaction.response.send_message(
            embed=cat_embed,
            view=PanelTicketCategorySelectView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="Create Panel", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def create_panel(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        
        # Save panel
        self.panel_data['channel_id'] = interaction.channel.id
        data_manager.save_ticket_panel(self.panel_data)
        
        # Send the actual panel (multi-embed set when configured via
        # /panelembed, otherwise the classic single embed)
        view = TicketPanelView(self.panel_data)
        message = await interaction.channel.send(
            embeds=_build_panel_message_embeds(self.panel_data),
            view=view
        )
        
        # Update panel with message ID
        self.panel_data['message_id'] = message.id
        data_manager.save_ticket_panel(self.panel_data)
        
        # Register view for persistence
        interaction.client.add_view(view)

        category_note = ""
        if self.panel_data.get('ticket_category_id'):
            label = _ticket_category_label(self.guild_id, self.panel_data['ticket_category_id'])
            category_note = f"\n**Ticket Category:** {label}"
        await interaction.response.send_message(
            embed=EmbedBuilder.success(
                "Panel Created",
                f"Your ticket panel is live!\n**Panel ID:** `{self.panel_data['panel_id']}`{category_note}",
            ),
            ephemeral=True
        )
        self.stop()


class PanelNameModal(Modal, title="Panel Name"):
    name_input = TextInput(label="Panel Name", placeholder="e.g., Support Tickets", max_length=50)
    
    def __init__(self, view: PanelCreatorView):
        super().__init__()
        self.view = view
        self.name_input.default = view.panel_data.get('name', '')
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view.panel_data['name'] = self.name_input.value
        await interaction.response.send_message(f"Panel name set to: {self.name_input.value}", ephemeral=True)


class PanelEmbedModal(Modal, title="Embed Settings"):
    title_input = TextInput(label="Embed Title", max_length=100)
    desc_input = TextInput(label="Embed Description", style=discord.TextStyle.paragraph, max_length=1000, required=False)
    color_input = TextInput(label="Color (Hex)", max_length=7, placeholder="#5865F2", required=False)
    
    def __init__(self, view: PanelCreatorView):
        super().__init__()
        self.view = view
        self.title_input.default = view.panel_data.get('embed_title', '')
        self.desc_input.default = view.panel_data.get('embed_description', '')
        self.color_input.default = hex(view.panel_data.get('embed_color', 0x5865F2))[2:]
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view.panel_data['embed_title'] = self.title_input.value
        
        if self.desc_input.value:
            self.view.panel_data['embed_description'] = self.desc_input.value
        
        if self.color_input.value:
            try:
                color_hex = self.color_input.value.strip('#')
                self.view.panel_data['embed_color'] = int(color_hex, 16)
            except ValueError:
                pass
        
        await interaction.response.send_message("Embed settings updated!", ephemeral=True)


class PanelButtonModal(Modal, title="Button Settings"):
    label_input = TextInput(label="Button Label", max_length=80, placeholder="Create Ticket")
    emoji_input = TextInput(label="Button Emoji", max_length=50, required=False, placeholder="🎫")
    style_input = TextInput(label="Style (1-4)", max_length=1, placeholder="3")
    
    def __init__(self, view: PanelCreatorView):
        super().__init__()
        self.view = view
        self.label_input.default = view.panel_data.get('button_label', '')
        self.emoji_input.default = view.panel_data.get('button_emoji', '')
        self.style_input.default = str(view.panel_data.get('button_style', 3))
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view.panel_data['button_label'] = self.label_input.value
        
        if self.emoji_input.value:
            self.view.panel_data['button_emoji'] = self.emoji_input.value
        
        try:
            style = int(self.style_input.value)
            if 1 <= style <= 4:
                self.view.panel_data['button_style'] = style
        except ValueError:
            pass
        
        await interaction.response.send_message("Button settings updated!", ephemeral=True)


class PanelSettingsModal(Modal, title="Panel Settings"):
    limit_input = TextInput(label="Max Tickets Per User (panel)", max_length=2, placeholder="3")
    close_input = TextInput(label="Auto-Close Hours (0=off)", max_length=3, placeholder="24")
    twostep_input = TextInput(label="Two-Step Close (yes/no)", max_length=3, placeholder="no")
    welcome_input = TextInput(label="Welcome Message", style=discord.TextStyle.paragraph, max_length=500, required=False)
    
    def __init__(self, view: PanelCreatorView):
        super().__init__()
        self.view = view
        self.limit_input.default = str(view.panel_data.get('ticket_limit', 3))
        self.close_input.default = str(view.panel_data.get('auto_close_hours', 24))
        self.twostep_input.default = 'yes' if view.panel_data.get('two_step_ticket') else 'no'
        self.welcome_input.default = view.panel_data.get('welcome_message', '')
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self.view.panel_data['ticket_limit'] = int(self.limit_input.value)
        except ValueError:
            pass
        
        try:
            hours = int(self.close_input.value)
            self.view.panel_data['auto_close_hours'] = max(0, hours)
        except ValueError:
            pass

        # TicketTool "Two Step Ticket": closed tickets keep their channel with
        # a moderator message (Re-Open / Delete / Transcript) instead of being
        # deleted immediately.
        self.view.panel_data['two_step_ticket'] = 1 if (self.twostep_input.value or '').strip().lower().startswith('y') else 0
        
        if self.welcome_input.value:
            self.view.panel_data['welcome_message'] = self.welcome_input.value
        
        await interaction.response.send_message(
            "Settings updated! (auto-close runs when the `Auto-Close Idle Tickets` toggle is on)",
            ephemeral=True,
        )


class PanelTargetModal(Modal, title="Panel Category & Role"):
    """Set the panel's ticket category and support role (TicketTool per-panel
    overrides — previously configurable only by editing the database)."""
    category_input = TextInput(label="Category ID (blank = guild default)", max_length=20, required=False)
    role_input = TextInput(label="Support Role ID (blank = guild default)", max_length=20, required=False)

    def __init__(self, view: PanelCreatorView):
        super().__init__()
        self.view = view
        if view.panel_data.get('category_id'):
            self.category_input.default = str(view.panel_data['category_id'])
        if view.panel_data.get('support_role_id'):
            self.role_input.default = str(view.panel_data['support_role_id'])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cat_raw = (self.category_input.value or '').strip()
        role_raw = (self.role_input.value or '').strip()
        if cat_raw:
            try:
                category_id = int(cat_raw)
                category = interaction.guild.get_channel(category_id)
                if category and isinstance(category, discord.CategoryChannel):
                    self.view.panel_data['category_id'] = category_id
                else:
                    await interaction.response.send_message("Invalid category ID — not changed.", ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message("Category ID must be a number — not changed.", ephemeral=True)
                return
        else:
            self.view.panel_data['category_id'] = None

        if role_raw:
            try:
                role_id = int(role_raw)
                if interaction.guild.get_role(role_id):
                    self.view.panel_data['support_role_id'] = role_id
                else:
                    await interaction.response.send_message("Invalid role ID — not changed.", ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message("Role ID must be a number — not changed.", ephemeral=True)
                return
        else:
            self.view.panel_data['support_role_id'] = None

        await interaction.response.send_message("Panel category & support role updated!", ephemeral=True)


class PanelTicketCategorySelectView(View):
    """Ephemeral select for assigning a Ticket Category (internal folder) to
    the panel being built in PanelCreatorView. Includes a "None" option to
    keep the panel's tickets Uncategorized."""
    _NONE_VALUE = '__none__'

    def __init__(self, creator: PanelCreatorView):
        super().__init__(timeout=120)
        self.creator = creator
        self._build_select()

    def _build_select(self) -> None:
        self.clear_items()
        options = [discord.SelectOption(
            label='None (Uncategorized)',
            value=self._NONE_VALUE,
            description="Tickets from this panel stay Uncategorized",
            emoji='📁',
        )]
        for cat in data_manager.load_ticket_categories(self.creator.guild_id)[:24]:
            emoji = (cat.get('emoji') or '').strip()
            options.append(discord.SelectOption(
                label=str(cat.get('name', 'Category'))[:100],
                value=cat['category_id'],
                description=(str(cat.get('description'))[:100] or None) if cat.get('description') else None,
                emoji=emoji or None,
            ))
        select = Select(
            placeholder='Choose the ticket category…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_category_selected
        self.add_item(select)

    async def on_category_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        raw = select.values[0]
        category_id = None if raw == self._NONE_VALUE else raw
        self.creator.panel_data['ticket_category_id'] = category_id
        label = _ticket_category_label(self.creator.guild_id, category_id)
        await interaction.response.edit_message(
            content=(
                f"Ticket Category set to **{label}** for this panel.\n"
                f"Every ticket created from this panel will be filed under that category. "
                f"You can change or remove it any time before creating the panel."
            ),
            view=None,
        )
        self.stop()


# =============================================================================
# TICKET CATEGORY MANAGER (!tcategory)
# =============================================================================
# Interactive embed-based management UI for Ticket Categories — the internal
# "folders" that group related tickets (e.g. a "Staff" category holding the
# "Apply for Staff" and "Staff Training" ticket types/panels).
#
# Flow:  !tcategory → embed with buttons
#          ├─ Create Category  → modal (name / description / emoji)
#          ├─ Edit Category    → select → prefilled modal
#          ├─ Delete Category  → select → confirmation (safe fallback)
#          └─ Assign to Panel  → select panel → select category
#
# Permission model: the command itself is gated by the same
# manage_channels permission used by every other ticket-management command.
# =============================================================================

def _build_ticket_categories_embed(guild_id: int, guild_name: str = '') -> discord.Embed:
    """Embed listing every Ticket Category in the guild (the !tcategory home)."""
    categories = data_manager.load_ticket_categories(guild_id) if data_manager else []
    embed = discord.Embed(
        title="📁 Ticket Categories",
        description=(
            "Ticket Categories are internal **folders** that group related "
            "tickets together — they are separate from Discord channel "
            "categories. Assign one to a panel in `!panel` (Ticket Category "
            "button) and every ticket from that panel is filed under it.\n\n"
            "Use the buttons below to manage categories."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    if categories:
        lines = []
        for cat in categories[:25]:
            emoji = (cat.get('emoji') or '').strip()
            name = str(cat.get('name', 'Category'))[:50]
            desc = (cat.get('description') or '').strip()
            ticket_count = data_manager.count_tickets_in_category(guild_id, cat['category_id'])
            panel_count = data_manager.count_panels_in_category(guild_id, cat['category_id'])
            line = f"{emoji + ' ' if emoji else ''}**{name}** — {ticket_count} ticket(s), {panel_count} panel(s)"
            if desc:
                line += f"\n> {desc[:150]}"
            lines.append(line)
        embed.add_field(
            name=f"Categories ({len(categories)})",
            value='\n'.join(lines)[:1024],
            inline=False,
        )
        if len(categories) > 25:
            embed.set_footer(text=f"…and {len(categories) - 25} more")
    else:
        embed.add_field(
            name="No categories yet",
            value=(
                "Create your first category with **Create Category** — for "
                "example `Staff`, then assign panels like *Apply for Staff* "
                "and *Staff Training* to it."
            ),
            inline=False,
        )
    if guild_name:
        embed.set_author(name=guild_name)
    return embed


class TicketCategoryManagerView(View):
    """Main !tcategory management view (Create / Edit / Delete / Assign)."""
    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.user_id = user_id
        self.message: Optional[discord.Message] = None

    async def _refresh(self) -> None:
        """Re-render the category list after a change."""
        if self.message is None:
            return
        try:
            await self.message.edit(embed=_build_ticket_categories_embed(self.guild_id), view=self)
        except discord.DiscordException:
            pass

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Create Category", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def create_category(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not your category manager.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketCategoryCreateModal(self))

    @discord.ui.button(label="Edit Category", style=discord.ButtonStyle.primary, emoji="✏️", row=0)
    async def edit_category(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not your category manager.", ephemeral=True)
            return
        categories = data_manager.load_ticket_categories(self.guild_id)
        if not categories:
            await interaction.response.send_message("No categories to edit yet.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select a category to edit:",
            view=TicketCategoryEditSelectView(self, categories),
            ephemeral=True,
        )

    @discord.ui.button(label="Delete Category", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def delete_category(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not your category manager.", ephemeral=True)
            return
        categories = data_manager.load_ticket_categories(self.guild_id)
        if not categories:
            await interaction.response.send_message("No categories to delete.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select a category to delete:",
            view=TicketCategoryDeleteSelectView(self, categories),
            ephemeral=True,
        )

    @discord.ui.button(label="Assign to Panel", style=discord.ButtonStyle.secondary, emoji="🎫", row=1)
    async def assign_to_panel(self, interaction: discord.Interaction, button: Button) -> None:
        """Change the Ticket Category of an EXISTING panel (panels don't need
        to be recreated — use /panelupdate afterwards to refresh the panel
        message if desired)."""
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not your category manager.", ephemeral=True)
            return
        panels = data_manager.load_ticket_panels_by_guild(self.guild_id)
        if not panels:
            await interaction.response.send_message("No active ticket panels in this server.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select the panel whose Ticket Category you want to change:",
            view=PanelCategoryAssignSelectView(self, panels),
            ephemeral=True,
        )

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary, row=1)
    async def done(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not your category manager.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=_build_ticket_categories_embed(self.guild_id),
            view=self,
        )
        self.stop()


class TicketCategoryCreateModal(Modal, title="Create Ticket Category"):
    name_input = TextInput(
        label="Category Name",
        placeholder="e.g., Staff",
        max_length=50,
        required=True,
    )
    description_input = TextInput(
        label="Description (optional)",
        style=discord.TextStyle.paragraph,
        max_length=200,
        required=False,
    )
    emoji_input = TextInput(
        label="Emoji / Icon (optional)",
        placeholder="🎫 or <:name:123456789012345678>",
        max_length=64,
        required=False,
    )

    def __init__(self, manager: TicketCategoryManagerView):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = (self.name_input.value or '').strip()
        if not name:
            await interaction.response.send_message("Category name cannot be empty.", ephemeral=True)
            return
        if len(name) > 50:
            await interaction.response.send_message("Category name must be 50 characters or fewer.", ephemeral=True)
            return
        # Duplicate check (case-insensitive).
        if data_manager.load_ticket_category_by_name(self.manager.guild_id, name):
            await interaction.response.send_message(
                f"A ticket category named **{name}** already exists. Pick a different name.",
                ephemeral=True,
            )
            return
        emoji_raw = (self.emoji_input.value or '').strip()
        emoji_ok, emoji_clean = _validate_category_emoji(emoji_raw)
        if not emoji_ok:
            await interaction.response.send_message(
                "Invalid emoji — use a single emoji (e.g. 🎫) or a full custom "
                "emoji like `<:name:123456789012345678>`.",
                ephemeral=True,
            )
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        data_manager.save_ticket_category({
            'category_id': str(uuid.uuid4())[:8],
            'guild_id': self.manager.guild_id,
            'name': name,
            'description': (self.description_input.value or '').strip() or None,
            'emoji': emoji_clean or None,
            'created_by': interaction.user.id,
            'created_at': now_iso,
            'updated_at': now_iso,
        })
        await interaction.response.send_message(
            f"📁 Ticket category **{name}** created! Assign it to a panel with "
            f"`!panel` (Ticket Category button) or **Assign to Panel** in `!tcategory`.",
            ephemeral=True,
        )
        await self.manager._refresh()


class TicketCategoryEditSelectView(View):
    """Ephemeral select listing categories for editing."""
    def __init__(self, manager: TicketCategoryManagerView, categories: List[Dict]):
        super().__init__(timeout=120)
        self.manager = manager
        options = []
        for cat in categories[:25]:
            emoji = (cat.get('emoji') or '').strip()
            options.append(discord.SelectOption(
                label=str(cat.get('name', 'Category'))[:100],
                value=cat['category_id'],
                description=(str(cat.get('description'))[:100] or None) if cat.get('description') else None,
                emoji=emoji or None,
            ))
        select = Select(
            placeholder='Choose a category to edit…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_category_selected
        self.add_item(select)

    async def on_category_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        category = data_manager.load_ticket_category(select.values[0])
        if not category:
            await interaction.response.edit_message(content="That category no longer exists.", view=None)
            return
        await interaction.response.send_modal(TicketCategoryEditModal(self.manager, category))
        self.stop()


class TicketCategoryEditModal(Modal, title="Edit Ticket Category"):
    name_input = TextInput(label="Category Name", max_length=50, required=True)
    description_input = TextInput(
        label="Description (optional)",
        style=discord.TextStyle.paragraph,
        max_length=200,
        required=False,
    )
    emoji_input = TextInput(
        label="Emoji / Icon (optional)",
        placeholder="🎫 or <:name:123456789012345678>",
        max_length=64,
        required=False,
    )

    def __init__(self, manager: TicketCategoryManagerView, category: Dict):
        super().__init__()
        self.manager = manager
        self.category = category
        self.name_input.default = str(category.get('name') or '')
        self.description_input.default = str(category.get('description') or '')
        self.emoji_input.default = str(category.get('emoji') or '')

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = (self.name_input.value or '').strip()
        if not name:
            await interaction.response.send_message("Category name cannot be empty.", ephemeral=True)
            return
        # Duplicate check must ignore the category being edited itself.
        existing = data_manager.load_ticket_category_by_name(self.manager.guild_id, name)
        if existing and existing['category_id'] != self.category['category_id']:
            await interaction.response.send_message(
                f"Another category named **{name}** already exists. Pick a different name.",
                ephemeral=True,
            )
            return
        emoji_raw = (self.emoji_input.value or '').strip()
        emoji_ok, emoji_clean = _validate_category_emoji(emoji_raw)
        if not emoji_ok:
            await interaction.response.send_message(
                "Invalid emoji — use a single emoji (e.g. 🎫) or a full custom "
                "emoji like `<:name:123456789012345678>`.",
                ephemeral=True,
            )
            return
        # Preserve identity + audit fields; renaming does NOT detach tickets
        # or panels (they reference the immutable category_id).
        self.category['name'] = name
        self.category['description'] = (self.description_input.value or '').strip() or None
        self.category['emoji'] = emoji_clean or None
        self.category['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_ticket_category(self.category)
        await interaction.response.send_message(
            f"📁 Ticket category **{name}** updated. Existing tickets and panels "
            f"keep their assignment (nothing was detached).",
            ephemeral=True,
        )
        await self.manager._refresh()


class TicketCategoryDeleteSelectView(View):
    """Ephemeral select listing categories for deletion."""
    def __init__(self, manager: TicketCategoryManagerView, categories: List[Dict]):
        super().__init__(timeout=120)
        self.manager = manager
        options = []
        for cat in categories[:25]:
            emoji = (cat.get('emoji') or '').strip()
            options.append(discord.SelectOption(
                label=str(cat.get('name', 'Category'))[:100],
                value=cat['category_id'],
                description=(str(cat.get('description'))[:100] or None) if cat.get('description') else None,
                emoji=emoji or None,
            ))
        select = Select(
            placeholder='Choose a category to delete…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_category_selected
        self.add_item(select)

    async def on_category_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        category = data_manager.load_ticket_category(select.values[0])
        if not category:
            await interaction.response.edit_message(content="That category no longer exists.", view=None)
            return
        guild_id = self.manager.guild_id
        ticket_count = data_manager.count_tickets_in_category(guild_id, category['category_id'])
        panel_count = data_manager.count_panels_in_category(guild_id, category['category_id'])
        emoji = (category.get('emoji') or '').strip()
        display = f"{emoji + ' ' if emoji else ''}{category['name']}"
        warn_lines = [
            f"You are about to delete the ticket category **{display}**.",
            "",
            f"• **{ticket_count}** ticket(s) currently use this category",
            f"• **{panel_count}** panel(s) currently assign this category",
            "",
            "Tickets are **not** deleted and their Discord channels are left "
            "untouched — they simply fall back to **Uncategorized**. Panels "
            "also fall back to no category.",
        ]
        embed = discord.Embed(
            title="🗑️ Delete Ticket Category?",
            description='\n'.join(warn_lines),
            color=discord.Color.orange(),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=TicketCategoryConfirmDeleteView(self.manager, category['category_id'], display),
        )
        self.stop()


class TicketCategoryConfirmDeleteView(View):
    """Final confirmation for deleting a ticket category."""
    def __init__(self, manager: TicketCategoryManagerView, category_id: str, display: str):
        super().__init__(timeout=120)
        self.manager = manager
        self.category_id = category_id
        self.display = display

    @discord.ui.button(label="Delete Category", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(self, interaction: discord.Interaction, button: Button) -> None:
        # delete_ticket_category NULLs ticket/panel references first, so
        # nothing points at a deleted row and no ticket data is lost.
        deleted = await asyncio.to_thread(
            data_manager.delete_ticket_category, self.category_id,
        )
        if deleted:
            await interaction.response.edit_message(
                content=(
                    f"🗑️ Ticket category **{self.display}** deleted.\n"
                    f"Its tickets now show as **Uncategorized** — ticket data, "
                    f"channels and transcripts were left fully intact."
                ),
                embed=None,
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content="That category no longer exists — nothing to delete.",
                embed=None,
                view=None,
            )
        await self.manager._refresh()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.edit_message(
            content="Deletion cancelled — the category is unchanged.",
            embed=None,
            view=None,
        )
        self.stop()


class PanelCategoryAssignSelectView(View):
    """Ephemeral select listing panels for category assignment."""
    def __init__(self, manager: TicketCategoryManagerView, panels: List[Dict]):
        super().__init__(timeout=120)
        self.manager = manager
        options = []
        for panel in panels[:25]:
            current_id = panel.get('ticket_category_id')
            current = (
                _ticket_category_label(panel.get('guild_id'), current_id)
                if current_id else UNCATEGORIZED_LABEL
            )
            options.append(discord.SelectOption(
                label=str(panel.get('name', 'Panel'))[:100],
                value=panel['panel_id'],
                description=f"Current category: {current}"[:100],
                emoji=str(panel.get('button_emoji') or '')[:1] or None,
            ))
        select = Select(
            placeholder='Choose a panel…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_panel_selected
        self.add_item(select)

    async def on_panel_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        panel = data_manager.load_ticket_panel(select.values[0])
        if not panel or panel.get('guild_id') != self.manager.guild_id:
            await interaction.response.edit_message(content="That panel no longer exists.", view=None)
            return
        categories = data_manager.load_ticket_categories(self.manager.guild_id)
        if not categories:
            await interaction.response.edit_message(
                content="No ticket categories exist yet — create one with **Create Category** first.",
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=(
                f"Panel: **{panel.get('name', 'Panel')}** — now choose its new "
                f"Ticket Category (tickets created from this panel are filed under it):"
            ),
            view=PanelCategoryTargetSelectView(self.manager, panel, categories),
        )
        self.stop()


class PanelCategoryTargetSelectView(View):
    """Ephemeral select choosing the category for a previously-picked panel."""
    _NONE_VALUE = '__none__'

    def __init__(self, manager: TicketCategoryManagerView, panel: Dict, categories: List[Dict]):
        super().__init__(timeout=120)
        self.manager = manager
        self.panel = panel
        options = [discord.SelectOption(
            label='None (Uncategorized)',
            value=self._NONE_VALUE,
            description="Tickets from this panel stay Uncategorized",
            emoji='📁',
        )]
        for cat in categories[:24]:
            emoji = (cat.get('emoji') or '').strip()
            options.append(discord.SelectOption(
                label=str(cat.get('name', 'Category'))[:100],
                value=cat['category_id'],
                description=(str(cat.get('description'))[:100] or None) if cat.get('description') else None,
                emoji=emoji or None,
            ))
        select = Select(
            placeholder='Choose the ticket category…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_category_selected
        self.add_item(select)

    async def on_category_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        raw = select.values[0]
        category_id = None if raw == self._NONE_VALUE else raw
        # Atomic single-column update — the panel row (embed settings, message
        # id, automations, …) is otherwise untouched.
        updated = await asyncio.to_thread(
            data_manager.set_panel_category, self.panel['panel_id'], category_id,
        )
        if not updated:
            await interaction.response.edit_message(
                content="That panel no longer exists — nothing was changed.",
                view=None,
            )
            return
        label = _ticket_category_label(self.manager.guild_id, category_id)
        await interaction.response.edit_message(
            content=(
                f"✅ Panel **{self.panel.get('name', 'Panel')}** now files its tickets "
                f"under **{label}**.\n"
                f"Existing tickets keep their current category — new tickets from "
                f"this panel will be filed under **{label}**. "
                f"Use `!panelupdate {self.panel['panel_id']}` if you want to refresh "
                f"the panel message."
            ),
            view=None,
        )
        self.stop()


# =============================================================================
# MANUAL RATING (Ticket Tool /rate) + TICKET INFO + PRIVATE + HELP
# =============================================================================

class ManualRatingView(View):
    """Ticket Tool-style manual rating prompt: the creator rates the support
    without closing the ticket (unlike TicketRatingView, which closes after).

    One rating per ticket — enforced on submit. Only the ticket creator can
    click the stars."""

    def __init__(self, ticket_id: str, creator_id: int):
        super().__init__(timeout=600)
        self.ticket_id = ticket_id
        self.creator_id = creator_id
        self.rated = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "Only the ticket creator can rate this ticket.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="⭐", style=discord.ButtonStyle.secondary, row=0)
    async def rate_1(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit(interaction, 1)

    @discord.ui.button(label="⭐⭐", style=discord.ButtonStyle.secondary, row=0)
    async def rate_2(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit(interaction, 2)

    @discord.ui.button(label="⭐⭐⭐", style=discord.ButtonStyle.secondary, row=0)
    async def rate_3(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit(interaction, 3)

    @discord.ui.button(label="⭐⭐⭐⭐", style=discord.ButtonStyle.secondary, row=1)
    async def rate_4(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit(interaction, 4)

    @discord.ui.button(label="⭐⭐⭐⭐⭐", style=discord.ButtonStyle.success, row=1)
    async def rate_5(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit(interaction, 5)

    async def _submit(self, interaction: discord.Interaction, rating: int) -> None:
        if self.rated:
            await interaction.response.defer()
            return
        self.rated = True
        ticket = data_manager.load_ticket(self.ticket_id)
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        if ticket.get('rating') is not None:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content="This ticket has already been rated. Thanks anyway!", view=self,
            )
            return
        ticket['rating'] = rating
        data_manager.save_ticket(ticket)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"⭐ Thank you for rating this support experience! You gave **{rating} star(s)**.",
            view=self,
        )
        self.stop()


class TicketSettingsConfigView(View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
    
    @discord.ui.button(label="Set Category", style=discord.ButtonStyle.primary)
    async def set_category(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetTicketCategoryModal(self.guild_id))
    
    @discord.ui.button(label="Set Transcripts", style=discord.ButtonStyle.primary)
    async def set_transcripts(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetTranscriptsChannelModal(self.guild_id))
    
    @discord.ui.button(label="Set Support Role", style=discord.ButtonStyle.secondary)
    async def set_support_role(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetSupportRoleModal(self.guild_id))

    @discord.ui.button(label="Set Log Channel", style=discord.ButtonStyle.secondary, row=1)
    async def set_log_channel(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetTicketLogChannelModal(self.guild_id))

    @discord.ui.button(label="Set Closed Category", style=discord.ButtonStyle.secondary, row=1)
    async def set_closed_category(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetClosedCategoryModal(self.guild_id))

    @discord.ui.button(label="Set Limits", style=discord.ButtonStyle.primary, row=2)
    async def set_limits(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetTicketLimitsModal(self.guild_id))


class SetTicketLimitsModal(Modal, title="Ticket Limits & Timers"):
    """TicketTool-style Limit Options (guild-wide):
    max tickets/user, max closed tickets/user, max open tickets overall,
    auto-close hours, and the first-response SLA.
    Previously these columns had no configuration UI at all."""

    max_per_user = TextInput(label="Max Open Tickets / User (0=off)", max_length=3, placeholder="3")
    max_closed = TextInput(label="Max CLOSED Tickets / User (0=off)", max_length=3, placeholder="0")
    max_open_all = TextInput(label="Max Open Tickets Overall (0=off)", max_length=4, placeholder="0")
    auto_close = TextInput(label="Auto-Close Idle Hours (0=off)", max_length=4, placeholder="24")
    sla = TextInput(label="First-Response SLA Hours (0=off)", max_length=4, placeholder="0")

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        self.max_per_user.default = str(settings.get('max_tickets_per_user', 3) or 3)
        self.max_closed.default = str(settings.get('max_closed_tickets_per_user', 0) or 0)
        self.max_open_all.default = str(settings.get('max_open_tickets_all', 0) or 0)
        self.auto_close.default = str(settings.get('auto_close_hours', 24) or 0)
        self.sla.default = str(settings.get('sla_hours', 0) or 0)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        def _to_int(value: str, default: int = 0, cap: int = 10000) -> int:
            try:
                return max(0, min(cap, int((value or '').strip() or default)))
            except ValueError:
                return default

        settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
        settings['max_tickets_per_user'] = _to_int(self.max_per_user.value, 3)
        settings['max_closed_tickets_per_user'] = _to_int(self.max_closed.value, 0)
        settings['max_open_tickets_all'] = _to_int(self.max_open_all.value, 0)
        settings['auto_close_hours'] = _to_int(self.auto_close.value, 0, cap=24*30)
        settings['sla_hours'] = _to_int(self.sla.value, 0, cap=24*30)
        settings['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_ticket_settings(settings)
        await interaction.response.send_message(
            "✅ Ticket limits updated:\n"
            f"• Max open tickets/user: **{settings['max_tickets_per_user']}**\n"
            f"• Max closed tickets/user: **{settings['max_closed_tickets_per_user']}** "
            "(checked at creation)\n"
            f"• Max open tickets overall: **{settings['max_open_tickets_all']}**\n"
            f"• Auto-close idle hours: **{settings['auto_close_hours']}** "
            "(needs the `Auto-Close Idle Tickets` toggle)\n"
            f"• First-response SLA: **{settings['sla_hours']}h**",
            ephemeral=True,
        )


class SetTicketLogChannelModal(Modal, title="Set Ticket Log Channel"):
    channel_input = TextInput(label="Channel ID", placeholder="Enter the ticket-log channel ID")

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        if settings.get('log_channel_id'):
            self.channel_input.default = str(settings['log_channel_id'])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.channel_input.value.strip()
        settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
        if raw.lower() in ('none', 'off', 'clear', '0'):
            settings['log_channel_id'] = None
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message("Ticket log channel cleared.", ephemeral=True)
            return
        try:
            channel_id = int(raw)
            channel = interaction.guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message("Invalid channel ID.", ephemeral=True)
                return
            settings['log_channel_id'] = channel_id
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message(
                f"Ticket log channel set to {channel.mention}. Configure logged events with `!ticketlog`.",
                ephemeral=True,
            )
        except ValueError:
            await interaction.response.send_message("Please enter a valid number (or 'none' to clear).", ephemeral=True)


class SetClosedCategoryModal(Modal, title="Set Closed Ticket Category"):
    category_input = TextInput(label="Category ID", placeholder="Category for two-step closed tickets")

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        if settings.get('closed_category_id'):
            self.category_input.default = str(settings['closed_category_id'])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.category_input.value.strip()
        settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
        if raw.lower() in ('none', 'off', 'clear', '0'):
            settings['closed_category_id'] = None
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message("Closed-ticket category cleared.", ephemeral=True)
            return
        try:
            category_id = int(raw)
            category = interaction.guild.get_channel(category_id)
            if not category or not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message("Invalid category ID.", ephemeral=True)
                return
            settings['closed_category_id'] = category_id
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message(
                f"Closed tickets (two-step panels) will move to **{category.name}**.", ephemeral=True,
            )
        except ValueError:
            await interaction.response.send_message("Please enter a valid number (or 'none' to clear).", ephemeral=True)


class SetTicketCategoryModal(Modal, title="Set Ticket Category"):
    category_input = TextInput(label="Category ID", placeholder="Enter category channel ID")
    
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        if settings.get('category_id'):
            self.category_input.default = str(settings['category_id'])
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            category_id = int(self.category_input.value)
            category = interaction.guild.get_channel(category_id)
            if not category or not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message("Invalid category ID.", ephemeral=True)
                return
            settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
            settings['category_id'] = category_id
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message(f"Ticket category set to **{category.name}**", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)


class SetTranscriptsChannelModal(Modal, title="Set Transcripts Channel"):
    channel_input = TextInput(label="Channel ID", placeholder="Enter transcripts channel ID")
    
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        if settings.get('transcripts_channel_id'):
            self.channel_input.default = str(settings['transcripts_channel_id'])
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            channel_id = int(self.channel_input.value)
            channel = interaction.guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message("Invalid channel ID.", ephemeral=True)
                return
            settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
            settings['transcripts_channel_id'] = channel_id
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message(f"Transcripts channel set to {channel.mention}", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)


class SetSupportRoleModal(Modal, title="Set Support Role"):
    role_input = TextInput(label="Role ID", placeholder="Enter support role ID")
    
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        if settings.get('support_role_id'):
            self.role_input.default = str(settings['support_role_id'])
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            role_id = int(self.role_input.value)
            role = interaction.guild.get_role(role_id)
            if not role:
                await interaction.response.send_message("Invalid role ID.", ephemeral=True)
                return
            settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
            settings['support_role_id'] = role_id
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message(f"Support role set to {role.mention}", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
