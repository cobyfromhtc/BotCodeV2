# -*- coding: utf-8 -*-
'''
ReactionRoles.modal — quick modal-based creator for a single mapping.

Rendered via ``ctx.send_modal(...)`` from the ``!rr creator`` command. The bot
instance is recovered from ``interaction.client`` at submit time, so the modal
carries no global state and is safe to construct anywhere.

The confirmation embed (on successful submit) uses
:class:`ReactionRoleEmbedBuilder` from ``system.py`` so the design-system
color palette, UTC timestamp, author attribution, and actor footer are all
applied consistently with the rest of the package. An optional ``🗑️ Undo
Mapping`` button (:class:`ReactionRoleDeleteConfirmView`) is attached to the
confirmation so the staff member can roll back immediately if they typo'd.
'''

from __future__ import annotations

import discord
from discord.ui import Modal, TextInput

from .system import (
    ReactionRoleSystem,
    ReactionRoleEmbedBuilder,
    ReactionRoleDeleteConfirmView,
)


class ReactionRoleCreatorModal(Modal, title="➕ Add Reaction Role Mapping"):
    '''Quick modal-based creator: emoji + role id + mode.

    Field labels are short and prefixed with an emoji so they read well in
    Discord's compact modal layout (label limit is 45 chars). Placeholders
    carry helpful examples so staff don't have to remember formats.
    '''

    emoji_input = TextInput(
        label="😀 Emoji",
        placeholder="👍  or  :custom_emoji:123456789",
        required=True,
        min_length=1,
        max_length=80,
    )
    role_input = TextInput(
        label="🏷️ Role ID",
        placeholder="123456789012345678  (right-click role → Copy ID)",
        required=True,
        min_length=1,
        max_length=20,
    )
    mode_input = TextInput(
        label="⚙️ Mode (optional)",
        placeholder="normal | verify | reverse | unique",
        required=False,
        max_length=10,
    )

    def __init__(self, channel: discord.TextChannel, message_id: int):
        super().__init__()
        self.channel = channel
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        embed_builder = getattr(bot, 'embed_builder', None)
        actor = interaction.user
        action_label = "rr creator"
        emoji_raw = self.emoji_input.value.strip()
        mode = (self.mode_input.value.strip() or "normal").lower()

        # Parse role id.
        try:
            role_id = int(self.role_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                embed=ReactionRoleEmbedBuilder.branded(
                    embed_builder,
                    ReactionRoleEmbedBuilder.error(
                        "❌ Invalid Role ID",
                        "Role ID must be a numeric Discord snowflake "
                        "(right-click the role → Copy ID).",
                        actor=actor, action=action_label,
                    ),
                    interaction.guild.id if interaction.guild else None,
                ),
                ephemeral=True,
            )
            return

        guild = interaction.guild
        role = guild.get_role(role_id) if guild is not None else None
        if role is None:
            await interaction.response.send_message(
                embed=ReactionRoleEmbedBuilder.branded(
                    embed_builder,
                    ReactionRoleEmbedBuilder.error(
                        "❌ Role Not Found",
                        f"No role with ID `{role_id}` exists in this server.",
                        actor=actor, action=action_label,
                    ),
                    guild.id if guild else None,
                ),
                ephemeral=True,
            )
            return

        ok, msg = await ReactionRoleSystem.add_mapping(
            guild, self.channel, self.message_id, emoji_raw, role,
            mode=mode, actor=actor,
        )
        if ok:
            # Rich confirmation embed: every key field is shown inline so the
            # staff member can verify at a glance that they got it right.
            embed = ReactionRoleEmbedBuilder.success(
                "➕ Reaction Role Added",
                f"{emoji_raw} now grants {role.mention} when reacted.",
                actor=actor, action=action_label,
            )
            embed.add_field(name="📢 Channel", value=self.channel.mention, inline=True)
            embed.add_field(name="🆔 Message ID", value=f"`{self.message_id}`", inline=True)
            embed.add_field(name="😀 Emoji", value=emoji_raw, inline=True)
            embed.add_field(name="🏷️ Role", value=role.mention, inline=True)
            embed.add_field(name="⚙️ Mode", value=f"`{mode}`", inline=True)
            embed.add_field(name="🆔 Mapping ID", value=f"`{msg}`", inline=True)
            embed = ReactionRoleEmbedBuilder.branded(
                embed_builder, embed, guild.id if guild else None)
            # Attach a one-click "Undo" button so the actor can roll back
            # immediately. Buttons are disabled during the async delete to
            # prevent double-clicks (handled inside the View).
            view = ReactionRoleDeleteConfirmView(
                mapping_id=msg, actor_id=actor.id, bot=bot,
                action_label=action_label,
            )
            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(
                embed=ReactionRoleEmbedBuilder.branded(
                    embed_builder,
                    ReactionRoleEmbedBuilder.error(
                        "❌ Could Not Add Reaction Role",
                        msg,
                        actor=actor, action=action_label,
                    ),
                    guild.id if guild else None,
                ),
                ephemeral=True,
            )
