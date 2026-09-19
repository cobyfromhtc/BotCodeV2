# -*- coding: utf-8 -*-
'''
ReactionRoles.system — core add/handle logic + emoji key normaliser.

Pure discord.py. Holds module-level refs to `bot` + `data_manager` + the
`ReactionRolesDB` accessor; these are set once by
ReactionRoles.wiring.on_setup_hook() via set_global_refs(). If the refs aren't
set yet (e.g. setup_hook hasn't run), every method fails safe (returns /
no-ops) rather than crashing.
'''

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord import ui

from . import db as rr_db


# =====================================================================
# GLOBAL REFS (set by wiring.on_setup_hook -> set_global_refs)
# =====================================================================
_bot = None
_data_manager = None
_db = None  # ReactionRolesDB accessor


def set_global_refs(bot, data_manager) -> None:
    '''Stash runtime refs so ReactionRoleSystem methods can reach the bot,
    the data manager, and the ReactionRolesDB accessor without them being
    passed on every call (matches TicketTool.automations.set_global_refs).'''
    global _bot, _data_manager, _db
    _bot = bot
    _data_manager = data_manager
    _db = getattr(bot, 'reaction_roles_db', None)
    logging.info("[ReactionRoles] global refs set")


# =====================================================================
# HELPERS
# =====================================================================

def _emoji_key(emoji) -> str:
    '''Normalise a reaction emoji (str | PartialEmoji | Emoji) into a stable
    string key that matches what we store in the DB.'''
    if isinstance(emoji, (discord.Emoji, discord.PartialEmoji)):
        if emoji.id is None:
            return emoji.name  # unicode emoji
        if emoji.animated:
            return f"<a:{emoji.name}:{emoji.id}>"
        return f"<:{emoji.name}:{emoji.id}>"
    return str(emoji)


# =====================================================================
# DESIGN SYSTEM — themed colors used everywhere in this package
# (success / error / warning / info / premium-gold)
# =====================================================================
RR_COLOR_SUCCESS = 0x57F287   # green
RR_COLOR_ERROR   = 0xED4245   # red
RR_COLOR_WARNING = 0xFEE75C   # orange/yellow
RR_COLOR_INFO    = 0x5865F2   # blurple
RR_COLOR_GOLD    = 0xFFD700   # premium


# =====================================================================
# EMBED BUILDER + VIEWS (shared by modal.py + commands.py)
# =====================================================================

class ReactionRoleEmbedBuilder:
    '''Centralized themed embed builder for the ReactionRoles package.

    Applies the design-system color palette (success/error/warning/info/gold),
    always sets a UTC timestamp, and (when an actor is supplied) attaches an
    author attribution + footer with the actor's display name + avatar and a
    short action label so every embed is auditable and visually consistent.

    Per-guild custom branding is layered on top by passing the embed through
    ``bot.embed_builder.branded(...)`` at the call site (see :meth:`branded`).
    '''

    SUCCESS = RR_COLOR_SUCCESS
    ERROR   = RR_COLOR_ERROR
    WARNING = RR_COLOR_WARNING
    INFO    = RR_COLOR_INFO
    GOLD    = RR_COLOR_GOLD

    # ------------------------------------------------------------------
    @staticmethod
    def _apply_attribution(embed: discord.Embed, actor, action: str) -> None:
        '''Attach author + footer attribution to ``embed`` for ``actor``.'''
        if actor is None:
            if action:
                embed.set_footer(text=action)
            return
        name = getattr(actor, 'display_name', None) or str(actor)
        footer_text = f"{action} • by {name}" if action else f"by {name}"
        avatar = getattr(actor, 'display_avatar', None)
        if avatar is not None:
            embed.set_footer(text=footer_text, icon_url=avatar.url)
            try:
                embed.set_author(name=name, icon_url=avatar.url)
            except Exception:
                embed.set_author(name=name)
        else:
            embed.set_footer(text=footer_text)
            embed.set_author(name=name)

    # ------------------------------------------------------------------
    @staticmethod
    def themed(title: str, description: str = "",
               color: int = RR_COLOR_INFO,
               actor=None, action: str = "Reaction Roles") -> discord.Embed:
        '''Build a fully themed embed (color + timestamp + attribution).'''
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        ReactionRoleEmbedBuilder._apply_attribution(embed, actor, action)
        return embed

    @staticmethod
    def success(title: str = "✅ Success", description: str = "",
                actor=None, action: str = "Reaction Roles") -> discord.Embed:
        return ReactionRoleEmbedBuilder.themed(
            title, description, RR_COLOR_SUCCESS, actor, action)

    @staticmethod
    def error(title: str = "❌ Error", description: str = "",
              actor=None, action: str = "Reaction Roles") -> discord.Embed:
        return ReactionRoleEmbedBuilder.themed(
            title, description, RR_COLOR_ERROR, actor, action)

    @staticmethod
    def warning(title: str = "⚠️ Warning", description: str = "",
                actor=None, action: str = "Reaction Roles") -> discord.Embed:
        return ReactionRoleEmbedBuilder.themed(
            title, description, RR_COLOR_WARNING, actor, action)

    @staticmethod
    def info(title: str = "ℹ️ Info", description: str = "",
             actor=None, action: str = "Reaction Roles") -> discord.Embed:
        return ReactionRoleEmbedBuilder.themed(
            title, description, RR_COLOR_INFO, actor, action)

    @staticmethod
    def premium(title: str = "🏆 Reaction Roles", description: str = "",
                actor=None, action: str = "Reaction Roles") -> discord.Embed:
        return ReactionRoleEmbedBuilder.themed(
            title, description, RR_COLOR_GOLD, actor, action)

    # ------------------------------------------------------------------
    @staticmethod
    def branded(embed_builder, embed: discord.Embed,
                guild_id: Optional[int]) -> discord.Embed:
        '''Layer per-guild custom branding on top via bot.embed_builder.branded.'''
        if embed_builder is not None and hasattr(embed_builder, 'branded'):
            try:
                return embed_builder.branded(embed, guild_id)
            except Exception as exc:
                logging.debug(f"[RR] branded() skipped: {exc}")
                return embed
        return embed


class ReactionRoleDeleteConfirmView(ui.View):
    '''Ephemeral View with a single ``🗑️ Undo Mapping`` button, shown after a
    mapping is created. Lets the actor undo immediately. Buttons are disabled
    while the delete is in flight to prevent double-clicks.

    Only the original actor (``actor_id``) can use the button; other users get
    an ephemeral warning via :meth:`interaction_check`.
    '''

    def __init__(self, mapping_id: str, actor_id: int, bot,
                 action_label: str = "Reaction Roles",
                 timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.mapping_id = mapping_id
        self.actor_id = actor_id
        self.bot = bot
        self.action_label = action_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            try:
                await interaction.response.send_message(
                    embed=ReactionRoleEmbedBuilder.warning(
                        "⚠️ Not Allowed",
                        "Only the staff member who created this mapping can undo it from here.",
                        actor=interaction.user, action=self.action_label,
                    ),
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                pass
            return False
        return True

    @ui.button(label="Undo Mapping", emoji="🗑️",
               style=discord.ButtonStyle.danger, custom_id="rr_delete_undo")
    async def undo_button(self, interaction: discord.Interaction,
                          button: ui.Button) -> None:
        # Disable buttons during async processing to prevent double-clicks.
        for child in self.children:
            child.disabled = True
        button.label = "Deleting…"
        # First response: edit the message to show disabled buttons.
        await interaction.response.edit_message(view=self)

        db = getattr(self.bot, 'reaction_roles_db', None)
        if db is None:
            await interaction.followup.send(
                embed=ReactionRoleEmbedBuilder.error(
                    "❌ Not Ready",
                    "Reaction-role database is not ready yet. Try again in a moment.",
                    actor=interaction.user, action=self.action_label,
                ),
                ephemeral=True,
            )
            # Re-enable buttons so the actor can retry.
            for child in self.children:
                child.disabled = False
            button.label = "Undo Mapping"
            await interaction.edit_original_response(view=self)
            return

        deleted = db.delete_reaction_role(self.mapping_id)
        if deleted:
            await interaction.edit_original_response(
                embed=ReactionRoleEmbedBuilder.success(
                    "🗑️ Mapping Removed",
                    f"Mapping `{self.mapping_id}` was deleted successfully.",
                    actor=interaction.user, action=self.action_label,
                ),
                view=None,  # remove the view (no more buttons)
            )
        else:
            # Re-enable buttons since delete failed (mapping already gone?).
            for child in self.children:
                child.disabled = False
            button.label = "Undo Mapping"
            await interaction.edit_original_response(
                embed=ReactionRoleEmbedBuilder.error(
                    "❌ Not Found",
                    f"No mapping with ID `{self.mapping_id}`. It may have already been removed.",
                    actor=interaction.user, action=self.action_label,
                ),
                view=self,
            )
        self.stop()


class ReactionRoleListView(ui.View):
    '''Paginated View for browsing reaction-role mappings in a guild.

    Renders Prev / page-indicator / Next buttons. Only the original caller
    can navigate; other users get an ephemeral warning via
    :meth:`interaction_check`. On timeout, all buttons are disabled in-place.
    '''

    PER_PAGE = 5  # max message-groups rendered per page

    def __init__(self, pages: List[discord.Embed], actor_id: int,
                 action_label: str = "Reaction Roles",
                 timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.page = 0
        self.actor_id = actor_id
        self.action_label = action_label
        self._update_button_state()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            try:
                await interaction.response.send_message(
                    embed=ReactionRoleEmbedBuilder.warning(
                        "⚠️ Not Allowed",
                        "Only the staff member who requested the list can navigate it.",
                        actor=interaction.user, action=self.action_label,
                    ),
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                pass
            return False
        return True

    def _update_button_state(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= len(self.pages) - 1
        self.page_button.label = f"{self.page + 1}/{len(self.pages)}"

    @ui.button(label="Prev", emoji="◀️",
               style=discord.ButtonStyle.secondary, custom_id="rr_list_prev")
    async def prev_button(self, interaction: discord.Interaction,
                          button: ui.Button) -> None:
        if self.page > 0:
            self.page -= 1
        self._update_button_state()
        await interaction.response.edit_message(
            embed=self.pages[self.page], view=self)

    @ui.button(label="1/1", emoji="📄",
               style=discord.ButtonStyle.primary, disabled=True)
    async def page_button(self, interaction: discord.Interaction,
                          button: ui.Button) -> None:
        # Always disabled (label only). Respond gracefully if somehow clicked.
        try:
            await interaction.response.send_message(
                embed=ReactionRoleEmbedBuilder.info(
                    "ℹ️ Page Indicator",
                    f"You're on page {self.page + 1} of {len(self.pages)}.",
                    actor=interaction.user, action=self.action_label,
                ),
                ephemeral=True,
            )
        except discord.InteractionResponded:
            pass

    @ui.button(label="Next", emoji="▶️",
               style=discord.ButtonStyle.secondary, custom_id="rr_list_next")
    async def next_button(self, interaction: discord.Interaction,
                          button: ui.Button) -> None:
        if self.page < len(self.pages) - 1:
            self.page += 1
        self._update_button_state()
        await interaction.response.edit_message(
            embed=self.pages[self.page], view=self)

    async def on_timeout(self) -> None:
        '''Disable all buttons when the view times out (best-effort edit).'''
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, discord.Forbidden):
                pass


# =====================================================================
# REACTION ROLE SYSTEM
# =====================================================================

class ReactionRoleSystem:
    '''
    Modes:
      normal  — toggle (add on react, remove on unreact)   [default]
      verify  — one-way (add on react, never auto-remove)
      reverse — remove role on react, add on unreact
      unique  — only one role per message; reacting removes your other
                roles on the same message and gives the new one
    '''

    VALID_MODES = {"normal", "verify", "reverse", "unique"}

    @staticmethod
    def _resolve_db():
        '''Return the ReactionRolesDB accessor from module ref, falling back
        to bot.reaction_roles_db. Returns None if unavailable (fail-safe).'''
        if _db is not None:
            return _db
        if _bot is not None:
            return getattr(_bot, 'reaction_roles_db', None)
        return None

    @staticmethod
    async def add_mapping(
        guild: discord.Guild,
        channel: discord.TextChannel,
        message_id: int,
        emoji_raw,
        role: discord.Role,
        mode: str = "normal",
        description: str = "",
        actor: Optional[discord.Member] = None,
    ) -> Tuple[bool, str]:
        if mode not in ReactionRoleSystem.VALID_MODES:
            return False, f"Invalid mode. Valid: {', '.join(sorted(ReactionRoleSystem.VALID_MODES))}"

        # Hierarchy check — the bot's top role must be above the target role.
        if guild.me.top_role <= role:
            return False, f"My highest role ({guild.me.top_role.mention}) must be above {role.mention}."

        if role.managed:
            return False, f"{role.mention} is a managed/integration role and can't be assigned."

        db = ReactionRoleSystem._resolve_db()
        if db is None:
            return False, "Reaction-role database is not ready yet. Try again in a moment."

        # Enforce the 250-per-guild premium cap (Carl-bot's signature limit).
        count = db.count_reaction_roles(guild.id)
        if count >= rr_db.MAX_REACTION_ROLES_PER_GUILD:
            return False, (
                f"Reaction role cap reached: **{count}/{rr_db.MAX_REACTION_ROLES_PER_GUILD}**. "
                "Remove an existing mapping before adding more."
            )

        # Fetch the message so we can (a) validate it exists and (b) add the
        # reaction emoji so the button is visible to users.
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return False, "That message no longer exists."
        except discord.Forbidden:
            return False, "I can't read that channel."
        except discord.HTTPException as exc:
            return False, f"Could not fetch message: {exc}"

        emoji_str = _emoji_key(emoji_raw)

        # Duplicate check (same message + emoji already mapped).
        existing = db.find_reaction_role(message_id, emoji_str)
        if existing:
            return False, f"That emoji is already mapped to <@&{existing['role_id']}> on this message."

        mapping_id = str(_uuid.uuid4())
        db.save_reaction_role({
            'mapping_id': mapping_id,
            'guild_id': guild.id,
            'channel_id': channel.id,
            'message_id': message_id,
            'emoji': emoji_str,
            'role_id': role.id,
            'mode': mode,
            'description': description,
            'created_by': actor.id if actor else None,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'is_active': True,
        })

        # Add the bot's reaction so users see the "button".
        try:
            await message.add_reaction(emoji_raw if not isinstance(emoji_raw, str) else emoji_str)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[RR] Could not add reaction {emoji_str} to msg {message_id}: {exc}")

        logging.info(
            f"[RR] Added mapping {emoji_str} -> {role.name} (mode={mode}) in {guild.id} "
            f"by {actor}"
        )
        return True, mapping_id

    @staticmethod
    async def handle_reaction(
        payload: "discord.RawReactionActionEvent", added: bool
    ) -> None:
        '''Core reaction-role logic. `added` = True for add, False for remove.'''
        if payload.guild_id is None or payload.member is None:
            return
        if payload.member.bot:
            return

        if _db is None or _bot is None:
            return

        mapping = _db.find_reaction_role(payload.message_id, _emoji_key(payload.emoji))
        if not mapping:
            return

        guild = _bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(mapping['role_id'])
        if role is None:
            return

        # Hierarchy / permission safety.
        if guild.me.top_role <= role or not guild.me.guild_permissions.manage_roles:
            return

        mode = mapping.get('mode', 'normal')
        member = payload.member

        try:
            if mode == 'verify':
                # One-way: only add, never remove.
                if added:
                    await member.add_roles(role, reason="Reaction role (verify)")
            elif mode == 'reverse':
                if added:
                    await member.remove_roles(role, reason="Reaction role (reverse)")
                else:
                    await member.add_roles(role, reason="Reaction role (reverse) restore")
            elif mode == 'unique':
                if added:
                    # Remove all OTHER roles mapped on the same message first.
                    others = _db.load_reaction_roles_by_message(payload.message_id)
                    other_roles = [
                        guild.get_role(m['role_id']) for m in others
                        if m['role_id'] != mapping['role_id'] and m.get('mode') == 'unique'
                    ]
                    other_roles = [r for r in other_roles if r is not None and guild.me.top_role > r]
                    if other_roles:
                        await member.remove_roles(*other_roles, reason="Reaction role (unique) swap")
                    await member.add_roles(role, reason="Reaction role (unique)")
            else:  # normal
                if added:
                    await member.add_roles(role, reason="Reaction role")
                else:
                    await member.remove_roles(role, reason="Reaction role removal")
        except discord.Forbidden:
            logging.warning(f"[RR] Missing permissions to manage {role.name} in {guild.id}")
        except discord.HTTPException as exc:
            logging.warning(f"[RR] HTTP error managing roles: {exc}")
