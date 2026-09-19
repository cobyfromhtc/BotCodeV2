# -*- coding: utf-8 -*-
"""Setup wizards — interactive channel/role/server/timing/limits config."""

# stdlib + discord.py
import discord
from dataclasses import dataclass
from datetime import datetime, timezone
from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View
from typing import Any, Dict, List, Optional, Tuple

from core import state  # shared mutable runtime state
from core.state import config
from utils.ui.embeds import EmbedBuilder




# ===========================================================================
# GENERIC INTERACTIVE SETUP FRAMEWORK
# ===========================================================================
# One reusable engine powering five commands:
#   !channelsetup  -> ChannelConfig   (manage_channels)
#   !rolesetup      -> RoleConfig      (manage_roles)
#   !serversetup    -> ServerConfig    (manage_guild)
#   !timingsetup    -> TimingConfig    (manage_guild)
#   !limitssetup    -> LimitsConfig    (manage_guild)
#
# Each config field is described by a SetupSlot. The UI is identical for every
# area: a dropdown lists the slots (step 1); picking one opens either a
# paginated picker (channels / roles / servers) or a number-input modal
# (timing / limits). All values persist to the bot_config SQLite table via
# the Config.save_*_settings() methods and reload on startup via the matching
# load_*_settings() calls in setup_hook().
# ===========================================================================

# --- Slot "kinds" ---
KIND_CHANNEL = "channel"
KIND_ROLE    = "role"
KIND_SERVER  = "server"
KIND_INTEGER = "integer"


@dataclass
class SetupSlot:
    """Describes one configurable field on a Config sub-object.

    target        -> "channels" | "roles" | "servers" | "timing" | "limits"
    attr          -> attribute name on the sub-config (e.g. "welcome", "member")
    label         -> human-readable label shown in the dropdown + embed
    kind          -> one of KIND_CHANNEL / KIND_ROLE / KIND_SERVER / KIND_INTEGER
    channel_types -> (KIND_CHANNEL only) which discord.ChannelType values to list
    guild_source  -> (KIND_CHANNEL only) "current" | "gang" | "server" — which
                     guild's channels to list when assigning
    min_val       -> (KIND_INTEGER only) inclusive lower bound
    max_val       -> (KIND_INTEGER only) inclusive upper bound
    unit          -> (KIND_INTEGER only) suffix shown after the value
                     ("minutes", "seconds", "days", ...)
    restart_note  -> optional note shown in the confirmation message when a
                     restart is required for the change to take full effect
    """
    target: str
    attr: str
    label: str
    kind: str
    channel_types: Tuple[discord.ChannelType, ...] = ()
    guild_source: str = "current"
    min_val: int = 0
    max_val: int = 2_147_483_647
    unit: str = ""
    restart_note: str = ""


# --- Slot definitions per config area ---------------------------------------

CHANNEL_SETUP_SLOTS: List[SetupSlot] = [
    SetupSlot("channels", "invite", "Invite Tracker Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "welcome", "Welcome / New-Member Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "rules", "Server Rules Display Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "log", "Mod Actions & Broadcasts Log Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "auto_scan", "Auto-Blacklist Scan Log Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "verification_main", "Verification Panel Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "verification_submission", "Verification Applications Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "reports", "User Reports Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "tickets", "Tickets Category (Category, NOT a Channel)", KIND_CHANNEL,
              channel_types=(discord.ChannelType.category,), guild_source="current"),
    SetupSlot("channels", "transcripts", "Ticket Transcripts Archive Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "giveaways", "Giveaways Posting Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("servers", "gang_rules_channel", "Gang (In-Game) Rules Source Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="gang"),
    SetupSlot("servers", "server_rules_channel", "Game Server Rules Source Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="server"),
]

ROLE_SETUP_SLOTS: List[SetupSlot] = [
    SetupSlot("roles", "invite_manager", "Invite Manager Role", KIND_ROLE),
    SetupSlot("roles", "member", "Member Role", KIND_ROLE),
    SetupSlot("roles", "staff", "Staff / Moderator Role", KIND_ROLE),
    SetupSlot("roles", "verified", "Verified Role", KIND_ROLE),
    SetupSlot("roles", "verification_ping", "Verification Ping Role", KIND_ROLE),
    SetupSlot("roles", "muted", "Muted Role", KIND_ROLE),
    SetupSlot("roles", "ticket_support", "Ticket Support Role", KIND_ROLE),
    SetupSlot("roles", "server_tester", "Server Tester Role", KIND_ROLE),
]

SERVER_SETUP_SLOTS: List[SetupSlot] = [
    SetupSlot("servers", "gang_server_id", "Gang (Home) Server", KIND_SERVER),
    SetupSlot("servers", "server_server_id", "Game Server", KIND_SERVER),
]

TIMING_SETUP_SLOTS: List[SetupSlot] = [
    SetupSlot("timing", "invite_check_interval_minutes", "Invite Check Interval", KIND_INTEGER,
              min_val=1, max_val=1440, unit="minutes",
              restart_note="Loop intervals apply on next bot restart."),
    SetupSlot("timing", "auto_scan_interval_hours", "Auto-Blacklist Scan Interval", KIND_INTEGER,
              min_val=1, max_val=168, unit="hours",
              restart_note="Loop intervals apply on next bot restart."),
    SetupSlot("timing", "report_message_interval_minutes", "Periodic Broadcast Interval", KIND_INTEGER,
              min_val=1, max_val=1440, unit="minutes",
              restart_note="Loop intervals apply on next bot restart."),
    SetupSlot("timing", "verification_timeout_seconds", "Verification Session Timeout", KIND_INTEGER,
              min_val=30, max_val=3600, unit="seconds"),
    SetupSlot("timing", "confirmation_timeout_seconds", "Confirmation Button Timeout", KIND_INTEGER,
              min_val=10, max_val=300, unit="seconds"),
    SetupSlot("timing", "report_timeout_seconds", "Report Submission Timeout", KIND_INTEGER,
              min_val=60, max_val=600, unit="seconds"),
    SetupSlot("timing", "info_request_timeout_seconds", "Info Request Timeout", KIND_INTEGER,
              min_val=60, max_val=600, unit="seconds"),
]

LIMITS_SETUP_SLOTS: List[SetupSlot] = [
    SetupSlot("limits", "min_account_age_days", "Min Account Age (Verification)", KIND_INTEGER,
              min_val=0, max_val=365, unit="days"),
    SetupSlot("limits", "min_blacklist_keyword_length", "Min Blacklist Keyword Length", KIND_INTEGER,
              min_val=1, max_val=50, unit="characters"),
    SetupSlot("limits", "min_poll_options", "Min Poll Options", KIND_INTEGER,
              min_val=2, max_val=10, unit="options"),
    SetupSlot("limits", "max_poll_options", "Max Poll Options", KIND_INTEGER,
              min_val=2, max_val=10, unit="options"),
    SetupSlot("limits", "min_poll_duration", "Min Poll Duration", KIND_INTEGER,
              min_val=10, max_val=86400, unit="seconds"),
    SetupSlot("limits", "max_poll_duration", "Max Poll Duration", KIND_INTEGER,
              min_val=60, max_val=604800, unit="seconds"),
    SetupSlot("limits", "max_warnings_before_ban", "Max Warnings Before Ban", KIND_INTEGER,
              min_val=1, max_val=50, unit="warnings"),
    SetupSlot("limits", "max_tickets_per_user", "Max Tickets Per User", KIND_INTEGER,
              min_val=1, max_val=50, unit="tickets"),
    SetupSlot("limits", "max_giveaway_winners", "Max Giveaway Winners", KIND_INTEGER,
              min_val=1, max_val=50, unit="winners"),
]

# Map each setup area to (title, permission-label) for the main embed.
_SETUP_AREA_META: Dict[str, Tuple[str, str]] = {
    "channels": ("🔧 Channel Setup",       "manage_channels"),
    "roles":    ("👥 Role Setup",          "manage_roles"),
    "servers":  ("🌐 Server Setup",        "manage_guild"),
    "timing":   ("⏱️ Timing Setup",        "manage_guild"),
    "limits":   ("📊 Limits Setup",        "manage_guild"),
}


# --- Helpers ---------------------------------------------------------------

def _setup_target(area: str):
    """Return the live Config sub-object for an area (config.channels, ...)."""
    if area == "channels":
        return config.channels
    if area == "roles":
        return config.roles
    if area == "servers":
        return config.servers
    if area == "timing":
        return config.timing
    if area == "limits":
        return config.limits
    raise ValueError(f"Unknown setup area: {area}")


def _setup_save(area: str) -> None:
    """Persist the sub-config for an area to its bot_config key."""
    if area == "channels":
        config.save_channel_settings()
    elif area == "roles":
        config.save_role_settings()
    elif area == "servers":
        config.save_server_settings()
    elif area == "timing":
        config.save_timing_settings()
    elif area == "limits":
        config.save_limits_settings()


def _setup_get_value(slot: SetupSlot) -> int:
    """Return the current int value held by a slot."""
    return int(getattr(_setup_target(slot.target), slot.attr, 0))


def _setup_set_value(slot: SetupSlot, value: int) -> None:
    """Write a new int value to a slot and persist it immediately."""
    setattr(_setup_target(slot.target), slot.attr, value)
    _setup_save(slot.target)


def _setup_guild_for(slot: SetupSlot, ctx_guild: Optional[discord.Guild]) -> Optional[discord.Guild]:
    """Resolve which guild's channels/roles to list for a slot.

    For KIND_CHANNEL the slot's guild_source drives the choice (current /
    gang / server). For KIND_ROLE the current guild is always used. Other
    kinds don't use a guild.
    """
    if slot.kind != KIND_CHANNEL:
        return ctx_guild
    if slot.guild_source == "current":
        return ctx_guild
    if slot.guild_source == "gang":
        return state.bot.get_guild(config.servers.gang_server_id)
    if slot.guild_source == "server":
        return state.bot.get_guild(config.servers.server_server_id)
    return ctx_guild


def _setup_display_value(slot: SetupSlot, ctx_guild: Optional[discord.Guild]) -> str:
    """Markdown display string for a slot's current value, WITH validation.

    - 0 / missing                 -> `Not set`
    - non-zero, resolves          -> clickable mention / formatted value
    - non-zero, unresolvable      -> `Not set` *(stale ID `cid`)*
    """
    val = _setup_get_value(slot)

    if slot.kind == KIND_CHANNEL:
        if not val:
            return "`Not set`"
        target_guild = _setup_guild_for(slot, ctx_guild)
        if target_guild is not None and target_guild.get_channel(val) is None:
            return f"`Not set` *(stale ID `{val}`)*"
        return f"<#{val}>"

    if slot.kind == KIND_ROLE:
        if not val:
            return "`Not set`"
        if ctx_guild is not None:
            if ctx_guild.get_role(val) is None:
                return f"`Not set` *(stale ID `{val}`)*"
        return f"<@&{val}>"

    if slot.kind == KIND_SERVER:
        if not val:
            return "`Not set`"
        g = state.bot.get_guild(val)
        if g is None:
            return f"`Not set` *(stale ID `{val}`)*"
        return f"**{g.name}** (`{val}`)"

    if slot.kind == KIND_INTEGER:
        unit = f" {slot.unit}" if slot.unit else ""
        return f"**{val}**{unit}"

    return "`Not set`"


def _setup_current_brief(slot: SetupSlot, ctx_guild: Optional[discord.Guild]) -> str:
    """Plain-text summary for select-option descriptions (no markdown)."""
    val = _setup_get_value(slot)

    if slot.kind == KIND_CHANNEL:
        if not val:
            return "Not set"
        target_guild = _setup_guild_for(slot, ctx_guild)
        if target_guild is not None:
            ch = target_guild.get_channel(val)
            if ch is not None:
                return f"Current: #{ch.name}"
            return f"Current: {val} (stale)"
        return f"Current: {val}"

    if slot.kind == KIND_ROLE:
        if not val:
            return "Not set"
        if ctx_guild is not None:
            role = ctx_guild.get_role(val)
            if role is not None:
                return f"Current: @{role.name}"
            return f"Current: {val} (stale)"
        return f"Current: {val}"

    if slot.kind == KIND_SERVER:
        if not val:
            return "Not set"
        g = state.bot.get_guild(val)
        if g is not None:
            return f"Current: {g.name}"
        return f"Current: {val} (stale)"

    if slot.kind == KIND_INTEGER:
        unit = f" {slot.unit}" if slot.unit else ""
        return f"Current: {val}{unit}"

    return "Not set"


def _setup_main_embed(area: str, slots: List[SetupSlot],
                      ctx_guild: Optional[discord.Guild]) -> discord.Embed:
    """Build the main setup embed showing every slot's current value."""
    title, perm = _SETUP_AREA_META[area]
    embed = discord.Embed(
        title=title,
        description=(
            "Use the dropdown below to configure each setting.\n"
            "1) Pick a setting to configure.\n"
            "2) Choose the value to assign (or type a number for timing/limits).\n"
            "Settings are saved automatically to the database."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    lines = [f"**{s.label}:** {_setup_display_value(s, ctx_guild)}" for s in slots]
    embed.add_field(name="Current Values", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Requires {perm} • Only the starter can interact.")
    return embed


# --- Views -----------------------------------------------------------------

class SetupMainView(View):
    """Step 1: dropdown listing every configurable slot for one config area.

    Picking a slot dispatches to either SetupAssignView (channels / roles /
    servers) or SetupIntegerModal (timing / limits).
    """

    def __init__(self, ctx: commands.Context, area: str, slots: List[SetupSlot]):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.area = area
        self.slots = slots
        self.author_id = ctx.author.id
        self.message: Optional[discord.Message] = None

        options = []
        for slot in slots:
            current_str = _setup_current_brief(slot, ctx.guild)
            options.append(discord.SelectOption(
                label=slot.label[:100],
                value=slot.attr,
                description=current_str[:100],
            ))

        self.slot_select = discord.ui.Select(
            placeholder="Select a setting to configure…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.slot_select.callback = self._on_slot_selected
        self.add_item(self.slot_select)

    def _refresh_main_embed(self) -> discord.Embed:
        return _setup_main_embed(self.area, self.slots, self.ctx.guild)

    def _build_items(self, slot: SetupSlot) -> Tuple[Optional[List[Any]], str, Optional[discord.Guild]]:
        """Build the pickable item list for a channel/role/server slot.

        Returns (items, guild_name, validation_guild). items is None when the
        target guild can't be resolved.
        """
        if slot.kind == KIND_CHANNEL:
            guild = _setup_guild_for(slot, self.ctx.guild)
            if guild is None:
                return None, "", None
            items = sorted(
                [c for c in guild.channels if c.type in slot.channel_types],
                key=lambda c: c.name.lower(),
            )
            return items, guild.name, guild

        if slot.kind == KIND_ROLE:
            guild = self.ctx.guild
            if guild is None:
                return None, "", None
            # Exclude @everyone, bot-managed, and integration roles — they
            # can't/shouldn't be assigned by hand.
            items = sorted(
                [r for r in guild.roles
                 if not r.is_bot_managed() and not r.is_integration() and r != guild.default_role],
                key=lambda r: r.position,
                reverse=True,
            )
            return items, guild.name, guild

        if slot.kind == KIND_SERVER:
            items = sorted(state.bot.guilds, key=lambda g: g.name.lower())
            return items, "All Servers", None

        return None, "", None

    async def _on_slot_selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return

        attr = self.slot_select.values[0]
        slot = next((s for s in self.slots if s.attr == attr), None)
        if not slot:
            await interaction.response.send_message("Invalid selection.", ephemeral=True)
            return

        # Integer slots open a modal instead of a picker.
        if slot.kind == KIND_INTEGER:
            modal = SetupIntegerModal(slot, self)
            await interaction.response.send_modal(modal)
            return

        # Channel / role / server -> paginated picker.
        items, guild_name, validation_guild = self._build_items(slot)
        if items is None:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Guild Not Found", "Could not find the target guild for this slot."),
                ephemeral=True,
            )
            return
        if not items:
            kind_word = {"channel": "channels", "role": "roles", "server": "servers"}.get(slot.kind, "items")
            await interaction.response.send_message(
                embed=EmbedBuilder.error("None Found", f"No matching {kind_word} found for **{slot.label}**."),
                ephemeral=True,
            )
            return

        assign_view = SetupAssignView(slot, self, items, guild_name, validation_guild)
        await interaction.response.edit_message(embed=assign_view.render_embed(), view=assign_view)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        try:
            if getattr(self, "message", None) is not None:
                await self.message.edit(view=self)
        except Exception:
            pass


class SetupAssignView(View):
    """Step 2: paginated dropdown of real Discord channels/roles/servers to
    assign to the chosen slot.

    Discord caps each select menu at 25 options, so this view pages through the
    full list using Prev / Next buttons. The select is rebuilt in place on
    each page navigation.
    """

    PAGE_SIZE = 25  # Discord hard limit for select options

    def __init__(self, slot: SetupSlot, main_view: SetupMainView,
                 items: List[Any], guild_name: str,
                 validation_guild: Optional[discord.Guild] = None):
        super().__init__(timeout=300)
        self.author_id = main_view.author_id
        self.slot = slot
        self.main_view = main_view
        self.guild_name = guild_name
        self.validation_guild = validation_guild
        self.all_items = items
        self.total_pages = max(1, (len(items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = 0

        self.item_select = discord.ui.Select(
            placeholder=f"Choose a value for: {slot.label}"[:150],
            min_values=1,
            max_values=1,
            options=self._page_options(),
        )
        self.item_select.callback = self._on_item_selected
        self.add_item(self.item_select)

        self.prev_btn = Button(label="⬅ Prev", style=discord.ButtonStyle.secondary)
        self.prev_btn.callback = self._on_prev
        self.add_item(self.prev_btn)

        self.next_btn = Button(label="Next ➡", style=discord.ButtonStyle.secondary)
        self.next_btn.callback = self._on_next
        self.add_item(self.next_btn)

        self.back_btn = Button(label="⬅ Back", style=discord.ButtonStyle.secondary)
        self.back_btn.callback = self._on_back
        self.add_item(self.back_btn)

        self.clear_btn = Button(label="Clear", style=discord.ButtonStyle.danger)
        self.clear_btn.callback = self._on_clear
        self.add_item(self.clear_btn)

        self._sync_nav_state()

    # --- pagination helpers ---
    def _page_options(self) -> List[discord.SelectOption]:
        start = self.page * self.PAGE_SIZE
        page_items = self.all_items[start:start + self.PAGE_SIZE]
        options: List[discord.SelectOption] = []
        for it in page_items:
            if self.slot.kind == KIND_CHANNEL:
                label = f"#{it.name}"
            else:
                label = it.name  # role or guild name
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(it.id),
                description=f"ID: {it.id}"[:100],
            ))
        return options

    def _sync_nav_state(self) -> None:
        multi = self.total_pages > 1
        self.prev_btn.disabled = (not multi) or self.page <= 0
        self.next_btn.disabled = (not multi) or self.page >= self.total_pages - 1

    def render_embed(self) -> discord.Embed:
        current_str = _setup_display_value(self.slot, self.validation_guild)
        kind_word = {"channel": "channels", "role": "roles", "server": "servers"}.get(self.slot.kind, "items")
        embed = discord.Embed(
            title=f"🔧 Configure: {self.slot.label}",
            description=(
                f"Choose the value to assign to **{self.slot.label}**.\n"
                f"**Source:** {self.guild_name}\n"
                f"**Current value:** {current_str}\n"
                f"**Available {kind_word}:** {len(self.all_items)}"
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        if self.total_pages > 1:
            start = self.page * self.PAGE_SIZE
            end = min(start + self.PAGE_SIZE, len(self.all_items))
            embed.set_footer(
                text=f"Page {self.page + 1}/{self.total_pages} • {start + 1}–{end} of {len(self.all_items)} (A–Z) • Use Prev/Next"
            )
        return embed

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return
        if self.page <= 0:
            await interaction.response.defer()
            return
        self.page -= 1
        self.item_select.options = self._page_options()
        self._sync_nav_state()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return
        if self.page >= self.total_pages - 1:
            await interaction.response.defer()
            return
        self.page += 1
        self.item_select.options = self._page_options()
        self._sync_nav_state()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=self.main_view._refresh_main_embed(), view=self.main_view)

    async def _on_clear(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return
        _setup_set_value(self.slot, 0)
        await interaction.response.edit_message(embed=self.main_view._refresh_main_embed(), view=self.main_view)
        await interaction.followup.send(
            embed=EmbedBuilder.success("Setting Cleared", f"**{self.slot.label}** has been cleared."),
            ephemeral=True,
        )

    async def _on_item_selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return
        value_id = int(self.item_select.values[0])
        _setup_set_value(self.slot, value_id)

        # Build a friendly confirmation string for the chosen value.
        if self.slot.kind == KIND_CHANNEL:
            display = f"<#{value_id}> (`{value_id}`)"
        elif self.slot.kind == KIND_ROLE:
            display = f"<@&{value_id}> (`{value_id}`)"
        else:  # KIND_SERVER
            g = state.bot.get_guild(value_id)
            display = f"**{g.name}** (`{value_id}`)" if g else f"`{value_id}`"

        await interaction.response.edit_message(embed=self.main_view._refresh_main_embed(), view=self.main_view)
        await interaction.followup.send(
            embed=EmbedBuilder.success("Setting Updated", f"**{self.slot.label}** set to {display}."),
            ephemeral=True,
        )


class SetupIntegerModal(Modal):
    """Number-input modal for KIND_INTEGER slots (timing / limits).

    Opened via interaction.response.send_modal(). On submit it validates the
    value, writes it, then returns the user to the main setup view with a
    refreshed embed showing the new value.
    """

    def __init__(self, slot: SetupSlot, main_view: SetupMainView):
        super().__init__(title=f"Set: {slot.label}"[:45], timeout=300)
        self.slot = slot
        self.main_view = main_view

        current = _setup_get_value(slot)
        unit_hint = f" ({slot.unit})" if slot.unit else ""
        self.input = TextInput(
            label=slot.label[:45],
            placeholder=f"Enter a whole number{unit_hint} ({slot.min_val}–{slot.max_val})",
            default_value=str(current) if current else "",
            required=True,
            max_length=12,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.main_view.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return

        raw = self.input.value.strip()
        try:
            val = int(raw)
        except ValueError:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Invalid Number", f"`{raw}` is not a valid whole number."),
                ephemeral=True,
            )
            return

        if val < self.slot.min_val or val > self.slot.max_val:
            await interaction.response.send_message(
                embed=EmbedBuilder.error(
                    "Out of Range",
                    f"Value must be between **{self.slot.min_val}** and **{self.slot.max_val}**.",
                ),
                ephemeral=True,
            )
            return

        _setup_set_value(self.slot, val)

        # Return to the main view with a refreshed embed.
        await interaction.response.edit_message(
            embed=self.main_view._refresh_main_embed(),
            view=self.main_view,
        )

        unit = f" {self.slot.unit}" if self.slot.unit else ""
        note = f"\n\nℹ️ {self.slot.restart_note}" if self.slot.restart_note else ""
        await interaction.followup.send(
            embed=EmbedBuilder.success(
                "Setting Updated",
                f"**{self.slot.label}** set to **{val}**{unit}.{note}",
            ),
            ephemeral=True,
        )

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    # --- OWNER BRANDING AND CHANNEL SETUP COMMANDS ---
    @bot.command(name="abrev", aliases=["Abrev", "ABREV"])
    @commands.is_owner()
    async def abbrev_cmd(ctx: commands.Context, abbreviation: str) -> None:
        value = abbreviation.strip()
        if not value:
            await ctx.send("Usage: `!abrev BTD`")
            return

        config.gang_abbreviation = value.upper()
        config.save_branding_settings()
        await ctx.send(embed=EmbedBuilder.success("Gang Abbreviation Updated", f"Abbreviation set to **{config.gang_abbreviation}**"))


    @bot.command(name="gangname", aliases=["GangName", "GANGNAME"])
    @commands.is_owner()
    async def gangname_cmd(ctx: commands.Context, *, name: str) -> None:
        value = name.strip()
        if not value:
            await ctx.send("Usage: `!gangname \"Brothers Till Death\"`")
            return

        config.gang_name = value
        config.bot_status = f"{config.gang_name} On Top"
        config.save_branding_settings()
        await ctx.send(embed=EmbedBuilder.success("Gang Name Updated", f"Gang name set to **{config.gang_name}**"))


    @bot.command(name="setchannel")
    @commands.is_owner()
    async def setchannel_cmd(ctx: commands.Context, channel_type: str, channel: discord.TextChannel) -> None:
        valid_types = {
            "welcome": "welcome",
            "rules": "rules",
            "verification": "verification_main",
            "verify": "verification_main",
            "reports": "reports",
            "tickets": "tickets",
            "log": "log",
            "logs": "log",
            "giveaways": "giveaways",
            "auto_scan": "auto_scan",
        }
        key = valid_types.get(channel_type.lower())
        if not key:
            await ctx.send("Usage: `!setchannel <welcome|rules|verification|reports|tickets|log|giveaways> #channel`")
            return

        setattr(config.channels, key, channel.id)
        config.save_channel_settings()
        await ctx.send(embed=EmbedBuilder.success("Channel Updated", f"Set **{channel_type.lower()}** to {channel.mention}"))


    # ===========================================================================
    # SETUP COMMANDS — one per config area
    # All use the same SetupMainView engine; only the slot list + permission differ.
    # ===========================================================================

    @bot.command(name="channelsetup",
                        aliases=["ChannelSetup", "CHANNELSETUP", "csetup", "CSetup", "CSETUP"])
    @commands.has_permissions(manage_channels=True)
    @commands.guild_only()
    async def channelsetup_cmd(ctx: commands.Context) -> None:
        """Interactive channel setup via dropdown menus."""
        view = SetupMainView(ctx, "channels", CHANNEL_SETUP_SLOTS)
        view.message = await ctx.send(embed=_setup_main_embed("channels", CHANNEL_SETUP_SLOTS, ctx.guild), view=view)


    @bot.command(name="rolesetup",
                        aliases=["RoleSetup", "ROLESETUP", "rsetup", "RSetup", "RSETUP"])
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def rolesetup_cmd(ctx: commands.Context) -> None:
        """Interactive role setup via dropdown menus."""
        view = SetupMainView(ctx, "roles", ROLE_SETUP_SLOTS)
        view.message = await ctx.send(embed=_setup_main_embed("roles", ROLE_SETUP_SLOTS, ctx.guild), view=view)


    @bot.command(name="serversetup",
                        aliases=["ServerSetup", "SERVERSETUP", "ssetup", "SSetup", "SSETUP"])
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def serversetup_cmd(ctx: commands.Context) -> None:
        """Interactive server setup via dropdown menus."""
        view = SetupMainView(ctx, "servers", SERVER_SETUP_SLOTS)
        view.message = await ctx.send(embed=_setup_main_embed("servers", SERVER_SETUP_SLOTS, ctx.guild), view=view)


    @bot.command(name="timingsetup",
                        aliases=["TimingSetup", "TIMINGSETUP", "tsetup", "TSetup", "TSETUP"])
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def timingsetup_cmd(ctx: commands.Context) -> None:
        """Interactive timing setup via dropdown menus + number input."""
        view = SetupMainView(ctx, "timing", TIMING_SETUP_SLOTS)
        view.message = await ctx.send(embed=_setup_main_embed("timing", TIMING_SETUP_SLOTS, ctx.guild), view=view)


    @bot.command(name="limitssetup",
                        aliases=["LimitsSetup", "LIMITSSETUP", "lsetup", "LSetup", "LSETUP"])
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def limitssetup_cmd(ctx: commands.Context) -> None:
        """Interactive limits setup via dropdown menus + number input."""
        view = SetupMainView(ctx, "limits", LIMITS_SETUP_SLOTS)
        view.message = await ctx.send(embed=_setup_main_embed("limits", LIMITS_SETUP_SLOTS, ctx.guild), view=view)
