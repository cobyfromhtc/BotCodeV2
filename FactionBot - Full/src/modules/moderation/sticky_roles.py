# -*- coding: utf-8 -*-
"""Sticky roles — restore member roles on rejoin."""

# stdlib + discord.py
import discord
import json
import logging
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands
from typing import List, Optional

from core.state import data_manager
from utils.ui.embeds import EmbedBuilder




# =========================================================================
# 2) STICKY ROLES — Dyno premium (re-apply roles on rejoin)
# =========================================================================
class StickyRoleSystem:
    @staticmethod
    def get_eligible_roles(guild_id: int) -> List[int]:
        cfg = data_manager.get_sticky_role_config(guild_id)
        try:
            return [int(r) for r in json.loads(cfg.get('eligible_role_ids', '[]'))]
        except Exception:
            return []

    @staticmethod
    def set_eligible_roles(guild_id: int, role_ids: List[int]) -> None:
        cfg = data_manager.get_sticky_role_config(guild_id)
        cfg['eligible_role_ids'] = json.dumps(role_ids)
        cfg['enabled'] = 1
        cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_sticky_role_config(cfg)

    @staticmethod
    def set_enabled(guild_id: int, enabled: bool) -> None:
        cfg = data_manager.get_sticky_role_config(guild_id)
        cfg['enabled'] = 1 if enabled else 0
        cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_sticky_role_config(cfg)

    @staticmethod
    def is_enabled(guild_id: int) -> bool:
        return bool(data_manager.get_sticky_role_config(guild_id).get('enabled', 0))

    @staticmethod
    def capture_member_roles(member: discord.Member) -> None:
        """Save the member's eligible roles so they can be restored on rejoin."""
        if not StickyRoleSystem.is_enabled(member.guild.id):
            return
        eligible = set(StickyRoleSystem.get_eligible_roles(member.guild.id))
        if not eligible:
            # No eligible list configured → save all non-managed, non-everyone roles.
            saved = [
                r.id for r in member.roles
                if not r.managed and r.id != member.guild.default_role.id
            ]
        else:
            saved = [r.id for r in member.roles if r.id in eligible]
        # Always save at least an empty list so we know we've seen this member.
        data_manager.save_sticky_roles(member.guild.id, member.id, saved)
        logging.info(
            f"[Sticky] Saved {len(saved)} role(s) for {member} leaving {member.guild.id}"
        )

    @staticmethod
    async def restore_member_roles(member: discord.Member) -> int:
        """Re-apply previously saved roles. Returns count of roles restored."""
        if not StickyRoleSystem.is_enabled(member.guild.id):
            return 0
        saved = data_manager.load_sticky_roles(member.guild.id, member.id)
        if not saved:
            return 0

        guild = member.guild
        eligible = set(StickyRoleSystem.get_eligible_roles(guild.id))
        to_add: List[discord.Role] = []
        for rid in saved:
            role = guild.get_role(rid)
            if role is None:
                continue
            if role.managed:
                continue
            if eligible and rid not in eligible:
                continue
            # Hierarchy safety.
            if guild.me.top_role <= role:
                continue
            to_add.append(role)

        if not to_add:
            return 0
        try:
            await member.add_roles(*to_add, reason="Sticky role restore on rejoin")
            logging.info(f"[Sticky] Restored {len(to_add)} role(s) to {member} rejoining {guild.id}")
            return len(to_add)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[Sticky] Could not restore roles to {member}: {exc}")
            return 0

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    @bot.group(name="stickyrole", aliases=["stickyroles", "sr"], description="Sticky Roles (Dyno premium — re-apply on rejoin)")
    @app_commands.default_permissions(manage_roles=True)
    async def sticky_group(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=EmbedBuilder.info(
                "Sticky Roles",
                "Subcommands: `enable`, `disable`, `add`, `remove`, `list`, `status`.",
            ), ephemeral=True)


    @sticky_group.command(name="enable", description="Turn ON sticky roles for this server")
    @commands.has_permissions(manage_roles=True)
    async def sticky_enable(ctx: commands.Context) -> None:
        StickyRoleSystem.set_enabled(ctx.guild.id, True)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Sticky Roles Enabled ✅",
            "Members will now keep their eligible roles when they leave and rejoin.",
        ), ctx.guild.id))


    @sticky_group.command(name="disable", description="Turn OFF sticky roles for this server")
    @commands.has_permissions(manage_roles=True)
    async def sticky_disable(ctx: commands.Context) -> None:
        StickyRoleSystem.set_enabled(ctx.guild.id, False)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.warning(
            "Sticky Roles Disabled",
            "Roles will no longer be saved/restored on rejoin. Existing saved data is kept.",
        ), ctx.guild.id))


    @sticky_group.command(name="add", description="Add a role to the sticky-eligible list")
    @app_commands.describe(role="The role that should be saved/restored on rejoin")
    @commands.has_permissions(manage_roles=True)
    async def sticky_add(ctx: commands.Context, role: discord.Role) -> None:
        if role.managed:
            await ctx.send(embed=EmbedBuilder.error("Managed Role", "Integration/managed roles can't be sticky."), ephemeral=True)
            return
        eligible = StickyRoleSystem.get_eligible_roles(ctx.guild.id)
        if role.id not in eligible:
            eligible.append(role.id)
            StickyRoleSystem.set_eligible_roles(ctx.guild.id, eligible)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Sticky Role Added",
            f"{role.mention} is now sticky-eligible. {len(eligible)} role(s) on the list.",
        ), ctx.guild.id))


    @sticky_group.command(name="remove", description="Remove a role from the sticky-eligible list")
    @app_commands.describe(role="The role to remove from the sticky list")
    @commands.has_permissions(manage_roles=True)
    async def sticky_remove(ctx: commands.Context, role: discord.Role) -> None:
        eligible = StickyRoleSystem.get_eligible_roles(ctx.guild.id)
        if role.id in eligible:
            eligible.remove(role.id)
            StickyRoleSystem.set_eligible_roles(ctx.guild.id, eligible)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Sticky Role Removed",
            f"{role.mention} is no longer sticky-eligible. {len(eligible)} role(s) remain.",
        ), ctx.guild.id))


    @sticky_group.command(name="list", description="Show all sticky-eligible roles")
    @commands.has_permissions(manage_roles=True)
    async def sticky_list(ctx: commands.Context) -> None:
        eligible = StickyRoleSystem.get_eligible_roles(ctx.guild.id)
        enabled = StickyRoleSystem.is_enabled(ctx.guild.id)
        embed = discord.Embed(
            title="📌 Sticky Roles",
            description=f"Status: **{'Enabled ✅' if enabled else 'Disabled ❌'}**\n"
                        f"Eligible roles: **{len(eligible)}**\n"
                        + ("_(No specific roles — all non-managed roles are saved.)_" if not eligible else ""),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        if eligible:
            lines = []
            for rid in eligible:
                r = ctx.guild.get_role(rid)
                lines.append(f"• {r.mention if r else f'~deleted:`{rid}`'}")
            embed.add_field(name="Eligible", value="\n".join(lines), inline=False)
        await ctx.send(embed=EmbedBuilder.branded(embed, ctx.guild.id))


    @sticky_group.command(name="status", description="Show sticky-role status for the server or a member")
    @app_commands.describe(member="Optional member to check their saved sticky roles")
    @commands.has_permissions(manage_roles=True)
    async def sticky_status(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        enabled = StickyRoleSystem.is_enabled(ctx.guild.id)
        if member is None:
            await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.info(
                "Sticky Role Status",
                f"Enabled: **{'Yes' if enabled else 'No'}**\n"
                f"Eligible roles: **{len(StickyRoleSystem.get_eligible_roles(ctx.guild.id))}**",
            ), ctx.guild.id))
            return
        saved = data_manager.load_sticky_roles(ctx.guild.id, member.id)
        roles = [ctx.guild.get_role(r) for r in saved]
        lines = [r.mention if r else f"~deleted:`{r}`" for r in roles]
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.info(
            f"Sticky Roles for {member.display_name}",
            f"Saved roles: **{len(saved)}**\n" + ("\n".join(lines) if lines else "_(none saved)_"),
        ), ctx.guild.id))
