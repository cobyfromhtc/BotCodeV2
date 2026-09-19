# -*- coding: utf-8 -*-
'''
ReactionRoles.commands — registers the !rr prefix group on the bot.

Called once from Bot.py after `bot` is defined:
    ReactionRoles.commands.register(bot)

All five subcommands are permission-gated (manage_roles). The group parent
replies with a help embed when invoked without a subcommand.

Commands added:
  !rr / !rr                     — parent help
  !rr add #ch <msg_id> <emoji> <@role> [mode] [description]
  !rr remove <mapping_id>
  !rr list                       — all mappings in this server (paginated)
  !rr clear <msg_id>             — drop all mappings on a message
  !rr creator <msg_id> [#ch]     — quick modal creator

Embeds are built via :class:`ReactionRoleEmbedBuilder` (in system.py), which
applies the design-system color palette, a UTC timestamp, an author
attribution, and a footer with the actor's name + a short action label. Per-
guild custom branding is layered on top via ``bot.embed_builder.branded(...)``.
'''

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from .system import (
    ReactionRoleSystem,
    ReactionRoleEmbedBuilder,
    ReactionRoleDeleteConfirmView,
    ReactionRoleListView,
)
from .modal import ReactionRoleCreatorModal
from .db import MAX_REACTION_ROLES_PER_GUILD


def register(bot):
    '''Register the reaction-role prefix group + subcommands on `bot`.'''

    def _db():
        return getattr(bot, 'reaction_roles_db', None)

    def _eb():
        return getattr(bot, 'embed_builder', None)

    def _branded(embed: discord.Embed, guild_id: Optional[int]) -> discord.Embed:
        return ReactionRoleEmbedBuilder.branded(_eb(), embed, guild_id)

    # ----- themed embed shortcuts (with branding) ----------------------
    def _success(title: str, description: str, actor, action: str,
                 guild_id: Optional[int]) -> discord.Embed:
        return _branded(
            ReactionRoleEmbedBuilder.success(title, description, actor, action),
            guild_id)

    def _info(title: str, description: str, actor, action: str,
              guild_id: Optional[int]) -> discord.Embed:
        return _branded(
            ReactionRoleEmbedBuilder.info(title, description, actor, action),
            guild_id)

    def _warning(title: str, description: str, actor, action: str,
                 guild_id: Optional[int]) -> discord.Embed:
        return _branded(
            ReactionRoleEmbedBuilder.warning(title, description, actor, action),
            guild_id)

    def _error(title: str, description: str, actor, action: str,
               guild_id: Optional[int]) -> discord.Embed:
        return _branded(
            ReactionRoleEmbedBuilder.error(title, description, actor, action),
            guild_id)

    def _gid(ctx: commands.Context) -> Optional[int]:
        return ctx.guild.id if ctx.guild else None

    # =================================================================
    # GROUP PARENT
    # =================================================================
    @bot.group(name="reactionrole", aliases=["rr"],
               description="Reaction Roles (Carl-bot style, up to 250)", invoke_without_command=True)
    @app_commands.default_permissions(manage_roles=True)
    async def rr_group(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            embed = ReactionRoleEmbedBuilder.info(
                "📋 Reaction Roles",
                "Carl-bot style reaction roles — up to **250** mappings per server (free).",
                actor=ctx.author, action="rr help",
            )
            embed.add_field(
                name="➕ Add a mapping",
                value=(
                    "`!rr add #channel <message_id> <emoji> <@role> [mode] [description]`\n"
                    "Bind an emoji on a message to a role."
                ),
                inline=False,
            )
            embed.add_field(
                name="🗑️ Remove a mapping",
                value="`!rr remove <mapping_id>`\nDelete a single mapping by its ID.",
                inline=False,
            )
            embed.add_field(
                name="📋 List mappings",
                value="`!rr list`\nBrowse all mappings in this server (paginated).",
                inline=False,
            )
            embed.add_field(
                name="🧹 Clear a message",
                value="`!rr clear <message_id>`\nDrop ALL mappings attached to a message.",
                inline=False,
            )
            embed.add_field(
                name="🛠️ Quick creator (modal)",
                value="`!rr creator <message_id> [#channel]`\nOpen a quick modal to add one mapping.",
                inline=False,
            )
            embed.add_field(
                name="⚙️ Modes",
                value=(
                    "`normal` (toggle) · `verify` (one-way) · "
                    "`reverse` (remove on react) · `unique` (exclusive)"
                ),
                inline=False,
            )
            await ctx.send(embed=_branded(embed, _gid(ctx)), ephemeral=True)

    # =================================================================
    # ADD
    # =================================================================
    @rr_group.command(name="add", description="Bind an emoji on a message to a role")
    @app_commands.describe(
        channel="The channel containing the message",
        message_id="The message ID to attach the reaction role to",
        emoji="Emoji (paste any emoji, or :name:id for a custom one)",
        role="The role to grant/toggle",
        mode="normal (toggle) / verify (one-way) / reverse / unique",
        description="Optional note about this mapping",
    )
    @commands.has_permissions(manage_roles=True)
    async def rr_add(
        ctx: commands.Context,
        channel: discord.TextChannel,
        message_id: str,
        emoji: str,
        role: discord.Role,
        mode: str = "normal",
        description: str = "",
    ) -> None:
        try:
            msg_id_int = int(message_id)
        except ValueError:
            await ctx.send(embed=_error(
                "❌ Bad Message ID",
                "Message ID must be a numeric Discord snowflake (right-click the message → Copy ID).",
                ctx.author, "rr add", _gid(ctx),
            ), ephemeral=True)
            return

        ok, msg = await ReactionRoleSystem.add_mapping(
            ctx.guild, channel, msg_id_int, emoji, role,
            mode=mode, description=description, actor=ctx.author,
        )
        if ok:
            embed = ReactionRoleEmbedBuilder.success(
                "➕ Reaction Role Added",
                f"{emoji} now grants {role.mention} when reacted.",
                actor=ctx.author, action="rr add",
            )
            embed.add_field(name="📢 Channel", value=channel.mention, inline=True)
            embed.add_field(name="🆔 Message ID", value=f"`{msg_id_int}`", inline=True)
            embed.add_field(name="😀 Emoji", value=emoji, inline=True)
            embed.add_field(name="🏷️ Role", value=role.mention, inline=True)
            embed.add_field(name="⚙️ Mode", value=f"`{mode}`", inline=True)
            embed.add_field(name="🆔 Mapping ID", value=f"`{msg}`", inline=True)
            if description:
                embed.add_field(name="📝 Description", value=description, inline=False)
            embed = _branded(embed, _gid(ctx))
            # Provide a one-click "Undo" so the staff member can roll back
            # immediately if they typo'd. Buttons are disabled during the
            # async delete to prevent double-clicks.
            view = ReactionRoleDeleteConfirmView(
                mapping_id=msg, actor_id=ctx.author.id, bot=bot,
                action_label="rr add",
            )
            await ctx.send(embed=embed, view=view, ephemeral=True)
        else:
            await ctx.send(embed=_error(
                "❌ Could Not Add Reaction Role",
                msg,
                ctx.author, "rr add", _gid(ctx),
            ), ephemeral=True)

    # =================================================================
    # REMOVE
    # =================================================================
    @rr_group.command(name="remove", description="Remove a reaction-role mapping by ID")
    @app_commands.describe(mapping_id="The mapping ID shown by !rr list")
    @commands.has_permissions(manage_roles=True)
    async def rr_remove(ctx: commands.Context, mapping_id: str) -> None:
        db = _db()
        if db is None:
            await ctx.send(embed=_error(
                "❌ Not Ready",
                "Reaction-role database is not ready yet. Try again in a moment.",
                ctx.author, "rr remove", _gid(ctx),
            ), ephemeral=True)
            return
        deleted = db.delete_reaction_role(mapping_id.strip())
        if deleted:
            await ctx.send(embed=_success(
                "🗑️ Mapping Removed",
                f"Mapping `{mapping_id.strip()}` was deleted successfully.",
                ctx.author, "rr remove", _gid(ctx),
            ), ephemeral=True)
        else:
            await ctx.send(embed=_error(
                "❌ Not Found",
                f"No mapping with ID `{mapping_id.strip()}`. Use `!rr list` to see valid IDs.",
                ctx.author, "rr remove", _gid(ctx),
            ), ephemeral=True)

    # =================================================================
    # LIST (paginated)
    # =================================================================
    @rr_group.command(name="list", description="List all reaction-role mappings in this server")
    @commands.has_permissions(manage_roles=True)
    async def rr_list(ctx: commands.Context) -> None:
        db = _db()
        if db is None:
            await ctx.send(embed=_error(
                "❌ Not Ready",
                "Reaction-role database is not ready yet. Try again in a moment.",
                ctx.author, "rr list", _gid(ctx),
            ), ephemeral=True)
            return
        mappings = db.load_reaction_roles_by_guild(ctx.guild.id)
        count = len(mappings)
        cap = MAX_REACTION_ROLES_PER_GUILD
        if not mappings:
            embed = ReactionRoleEmbedBuilder.info(
                "📋 Reaction Roles",
                f"No reaction roles configured yet.\nCapacity: **0/{cap}** (Carl-bot style limit, free).",
                actor=ctx.author, action="rr list",
            )
            embed.add_field(
                name="🚀 Get started",
                value=(
                    "Use `!rr creator <message_id>` for a quick modal setup, or "
                    "`!rr add #channel <message_id> <emoji> <@role>` for the full command."
                ),
                inline=False,
            )
            await ctx.send(embed=_branded(embed, _gid(ctx)), ephemeral=True)
            return

        # Group by message for readability.
        by_msg: Dict[int, List[Dict]] = {}
        for m in mappings:
            by_msg.setdefault(m['message_id'], []).append(m)

        msg_groups = list(by_msg.items())
        per_page = ReactionRoleListView.PER_PAGE
        total_pages = max(1, (len(msg_groups) + per_page - 1) // per_page)

        pages: List[discord.Embed] = []
        for page_idx in range(total_pages):
            chunk = msg_groups[page_idx * per_page:(page_idx + 1) * per_page]
            embed = ReactionRoleEmbedBuilder.info(
                "📋 Reaction Roles",
                (
                    f"**{count}/{cap}** mappings · **{len(by_msg)}** message"
                    f"{'s' if len(by_msg) != 1 else ''} in **{ctx.guild.name}**"
                ),
                actor=ctx.author,
                action=f"rr list ({page_idx + 1}/{total_pages})",
            )
            for msg_id, group in chunk:
                chan = ctx.guild.get_channel(group[0]['channel_id'])
                chan_label = chan.mention if chan else f"~deleted#{group[0]['channel_id']}"
                lines: List[str] = []
                for m in group[:20]:
                    role_obj = ctx.guild.get_role(m['role_id'])
                    role_label = role_obj.mention if role_obj else f"~deleted:{m['role_id']}"
                    lines.append(
                        f"• {m['emoji']} → {role_label} · `{m['mode']}` · "
                        f"`{m['mapping_id'][:8]}`"
                    )
                if len(group) > 20:
                    lines.append(f"• …and {len(group) - 20} more")
                value = "\n".join(lines) if lines else "*(no roles mapped)*"
                embed.add_field(
                    name=(
                        f"📢 {chan_label} · msg `{msg_id}` "
                        f"({len(group)} role{'s' if len(group) != 1 else ''})"
                    ),
                    value=value,
                    inline=False,
                )
            pages.append(_branded(embed, _gid(ctx)))

        if len(pages) == 1:
            await ctx.send(embed=pages[0], ephemeral=True)
        else:
            view = ReactionRoleListView(
                pages, ctx.author.id, action_label="rr list")
            await ctx.send(embed=pages[0], view=view, ephemeral=True)

    # =================================================================
    # CLEAR
    # =================================================================
    @rr_group.command(name="clear", description="Remove ALL reaction-role mappings on a message")
    @app_commands.describe(message_id="The message ID whose mappings should be cleared")
    @commands.has_permissions(manage_roles=True)
    async def rr_clear(ctx: commands.Context, message_id: str) -> None:
        try:
            msg_id_int = int(message_id)
        except ValueError:
            await ctx.send(embed=_error(
                "❌ Bad Message ID",
                "Message ID must be a numeric Discord snowflake.",
                ctx.author, "rr clear", _gid(ctx),
            ), ephemeral=True)
            return
        db = _db()
        if db is None:
            await ctx.send(embed=_error(
                "❌ Not Ready",
                "Reaction-role database is not ready yet. Try again in a moment.",
                ctx.author, "rr clear", _gid(ctx),
            ), ephemeral=True)
            return
        removed = db.delete_reaction_roles_for_message(msg_id_int)
        if removed == 0:
            embed = ReactionRoleEmbedBuilder.warning(
                "🧹 Nothing to Clear",
                f"No mappings were attached to message `{msg_id_int}`.",
                actor=ctx.author, action="rr clear",
            )
        else:
            embed = ReactionRoleEmbedBuilder.success(
                "🧹 Reaction Roles Cleared",
                f"Removed **{removed}** mapping(s) from message `{msg_id_int}`.",
                actor=ctx.author, action="rr clear",
            )
            embed.add_field(
                name="🆔 Message ID", value=f"`{msg_id_int}`", inline=True)
            embed.add_field(
                name="🗑️ Mappings Removed", value=f"**{removed}**", inline=True)
        await ctx.send(embed=_branded(embed, _gid(ctx)), ephemeral=True)

    # =================================================================
    # CREATOR (modal)
    # =================================================================
    @rr_group.command(name="creator", description="Open a quick modal to add a reaction role to a message")
    @app_commands.describe(message_id="The message ID to attach the reaction role to",
                           channel="The channel containing the message (defaults to this one)")
    @commands.has_permissions(manage_roles=True)
    async def rr_creator(
        ctx: commands.Context,
        message_id: str,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        try:
            msg_id_int = int(message_id)
        except ValueError:
            await ctx.send(embed=_error(
                "❌ Bad Message ID",
                "Message ID must be a numeric Discord snowflake.",
                ctx.author, "rr creator", _gid(ctx),
            ), ephemeral=True)
            return
        target_channel = channel or ctx.channel
        # Verify the message exists before showing the modal.
        try:
            await target_channel.fetch_message(msg_id_int)
        except discord.NotFound:
            await ctx.send(embed=_error(
                "❌ Message Not Found",
                f"No message with ID `{msg_id_int}` in {target_channel.mention}.",
                ctx.author, "rr creator", _gid(ctx),
            ), ephemeral=True)
            return
        except discord.Forbidden:
            await ctx.send(embed=_error(
                "❌ Missing Access",
                f"I can't read messages in {target_channel.mention}.",
                ctx.author, "rr creator", _gid(ctx),
            ), ephemeral=True)
            return
        except Exception as exc:
            await ctx.send(embed=_error(
                "❌ Message Not Found",
                f"Could not fetch that message: `{exc}`",
                ctx.author, "rr creator", _gid(ctx),
            ), ephemeral=True)
            return
        await ctx.send_modal(ReactionRoleCreatorModal(target_channel, msg_id_int))

    logging.info("[ReactionRoles] registered rr prefix group + 5 subcommands")
