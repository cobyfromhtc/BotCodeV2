# -*- coding: utf-8 -*-
"""Message log — full message logging system + msglog config commands."""

# stdlib + discord.py
import discord
import json
import logging
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands
from typing import Dict, List

from core import state  # shared mutable runtime state
from core.state import data_manager
from utils.ui.embeds import EmbedBuilder




# =========================================================================
# 3) FULL MESSAGE LOGGING — Dyno premium (edit/delete content)
# =========================================================================
class MessageLogSystem:
    @staticmethod
    def get_config(guild_id: int) -> Dict:
        return data_manager.get_message_log_config(guild_id)

    @staticmethod
    def save_config(cfg: Dict) -> None:
        data_manager.save_message_log_config(cfg)

    @staticmethod
    def is_channel_ignored(cfg: Dict, channel_id: int) -> bool:
        try:
            ignored = set(json.loads(cfg.get('ignore_channels', '[]') or '[]'))
        except Exception:
            ignored = set()
        return channel_id in ignored

    @staticmethod
    def should_log(cfg: Dict, message: discord.Message, kind: str) -> bool:
        if not cfg.get('enabled'):
            return False
        if cfg.get('ignore_bots', 1) and message.author.bot:
            return False
        if MessageLogSystem.is_channel_ignored(cfg, message.channel.id):
            return False
        if kind == 'edit' and not cfg.get('log_edits', 1):
            return False
        if kind == 'delete' and not cfg.get('log_deletes', 1):
            return False
        return True

    @staticmethod
    def cache(message: discord.Message) -> None:
        """Snapshot a message so we can recover its original content on edit/delete."""
        if message.guild is None:
            return
        cfg = MessageLogSystem.get_config(message.guild.id)
        if not cfg.get('enabled'):
            return
        # Don't cache messages in ignored channels.
        if MessageLogSystem.is_channel_ignored(cfg, message.channel.id):
            return
        try:
            attachments = []
            for a in message.attachments:
                attachments.append({
                    'filename': a.filename,
                    'url': a.url,
                    'proxy_url': getattr(a, 'proxy_url', None),
                    'size': getattr(a, 'size', None),
                })
            data_manager.cache_message({
                'message_id': message.id,
                'guild_id': message.guild.id,
                'channel_id': message.channel.id,
                'author_id': message.author.id,
                'author_name': str(message.author),
                'content': message.content or '',
                'attachments': json.dumps(attachments),
                'created_at': datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            logging.debug(f"[MsgLog] cache failed: {exc}")

    @staticmethod
    async def log_delete(message: "discord.Message") -> None:
        if message.guild is None:
            return
        cfg = MessageLogSystem.get_config(message.guild.id)
        if not MessageLogSystem.should_log(cfg, message, 'delete'):
            return
        log_channel = message.guild.get_channel(cfg.get('log_channel_id') or 0)
        if log_channel is None:
            return

        # Prefer our cached original content (Discord's delete payload sometimes
        # still has it, but the cache is the reliable source of truth).
        cached = data_manager.load_cached_message(message.id)
        original_content = (cached['content'] if cached else None) or message.content or ''
        author_name = (cached['author_name'] if cached else None) or str(message.author)
        author_id = (cached['author_id'] if cached else None) or message.author.id

        snippet = original_content if len(original_content) <= 1024 else (original_content[:1021] + '...')

        embed = discord.Embed(
            title="🗑️ Message Deleted",
            description=(
                f"**Author:** <@{author_id}> (`{author_name}` / `{author_id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Message ID:** `{message.id}`"
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Content",
            value=(snippet if snippet.strip() else "_(empty or embed-only message)_"),
            inline=False,
        )

        # Attachments.
        attachments = []
        if cached:
            try:
                attachments = json.loads(cached.get('attachments', '[]') or '[]')
            except Exception:
                attachments = []
        elif message.attachments:
            attachments = [{'filename': a.filename, 'url': a.url} for a in message.attachments]
        if attachments:
            att_lines = [f"• [{a.get('filename','file')}]({a.get('url')})" for a in attachments[:5]]
            embed.add_field(name="Attachments", value="\n".join(att_lines), inline=False)

        try:
            await log_channel.send(embed=EmbedBuilder.branded(embed, message.guild.id))
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[MsgLog] could not send delete log: {exc}")

        data_manager.delete_cached_message(message.id)

    @staticmethod
    async def log_bulk_delete(messages: List["discord.Message"], deleted_by: "discord.Member", channel: "discord.TextChannel", reason: str = "purge") -> None:
        """Log a batch of messages deleted via `!purge` / `!purgeall`.

        discord.py's `TextChannel.purge()` uses the bulk-delete endpoint,
        which does NOT fire `on_message_delete` for individual messages —
        so the normal MessageLogSystem.log_delete path never sees them.
        This helper is called from the purge command BEFORE the bulk delete
        so each purged message is recorded in the log channel.

        Sends a single compact summary embed (not one embed per message) so
        a `!purge 100` doesn't spam the log channel with 100 embeds.
        """
        if not messages:
            return
        if channel is None or channel.guild is None:
            return
        cfg = MessageLogSystem.get_config(channel.guild.id)
        if not MessageLogSystem.should_log(cfg, messages[0], 'delete'):
            return
        log_channel = channel.guild.get_channel(cfg.get('log_channel_id') or 0)
        if log_channel is None:
            return

        embed = discord.Embed(
            title=f"🧹 Bulk Purge — {len(messages)} message(s)",
            description=(
                f"**Moderator:** {deleted_by.mention} (`{deleted_by}` / `{deleted_by.id}`)\n"
                f"**Channel:** {channel.mention}\n"
                f"**Reason:** `{reason}`"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        # Compact list of purged messages: one line each, truncated.
        # Discord embed field value limit is 1024 chars, so we cap the list
        # and note how many were truncated.
        MAX_FIELD = 1024
        lines: List[str] = []
        current_len = 0
        shown = 0
        truncated = 0
        for m in messages:
            author_disp = str(m.author)
            content = (m.content or '').replace('\n', ' ').strip()
            if not content and m.attachments:
                content = f"[attachment: {m.attachments[0].filename}]"
            if not content:
                content = "(empty)"
            if len(content) > 80:
                content = content[:77] + '...'
            line = f"`{m.id}` **{author_disp}**: {content}"
            line_len = len(line) + 1
            if current_len + line_len > MAX_FIELD - 20 and shown >= 1:
                # Stop adding lines to keep under the field limit.
                truncated = len(messages) - shown
                break
            lines.append(line)
            current_len += line_len
            shown += 1

        list_text = "\n".join(lines)
        if truncated > 0:
            list_text += f"\n..._and {truncated} more_"

        embed.add_field(
            name=f"Deleted messages ({len(messages)} total)",
            value=list_text or "_(no content)_",
            inline=False,
        )

        # Mention the bulk-delete behavior so log readers know individual
        # on_message_delete events did NOT fire for these.
        embed.set_footer(text="Bulk-purged via command • individual delete events were not fired by Discord")

        try:
            await log_channel.send(embed=EmbedBuilder.branded(embed, channel.guild.id))
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[MsgLog] could not send bulk-purge log: {exc}")

        # Also delete the cached copies of the purged messages so the cache
        # doesn't grow stale with rows for messages that no longer exist.
        for m in messages:
            try:
                data_manager.delete_cached_message(m.id)
            except Exception:
                pass

    @staticmethod
    async def log_edit(before: "discord.Message", after: "discord.Message") -> None:
        if before.guild is None:
            return
        cfg = MessageLogSystem.get_config(before.guild.id)
        if not MessageLogSystem.should_log(cfg, before, 'edit'):
            return
        # No-op if content didn't change (pin edits etc. fire on_message_edit).
        if (before.content or '') == (after.content or ''):
            return
        log_channel = before.guild.get_channel(cfg.get('log_channel_id') or 0)
        if log_channel is None:
            return

        old_content = before.content or ''
        new_content = after.content or ''
        old_snip = old_content if len(old_content) <= 1024 else (old_content[:1021] + '...')
        new_snip = new_content if len(new_content) <= 1024 else (new_content[:1021] + '...')

        embed = discord.Embed(
            title="✏️ Message Edited",
            description=(
                f"**Author:** {before.author.mention} (`{before.author.id}`)\n"
                f"**Channel:** {before.channel.mention}\n"
                f"**Message ID:** `{before.id}` — [jump]({after.jump_url})"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Before", value=(old_snip if old_snip.strip() else "_(empty)_"), inline=False)
        embed.add_field(name="After", value=(new_snip if new_snip.strip() else "_(empty)_"), inline=False)
        try:
            await log_channel.send(embed=EmbedBuilder.branded(embed, before.guild.id))
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[MsgLog] could not send edit log: {exc}")

        # Refresh the cache with the new content.
        MessageLogSystem.cache(after)

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    @bot.group(name="msglog", description="Full Message Logging (Dyno premium — edits + deletes with content)")
    @app_commands.default_permissions(manage_guild=True)
    async def msglog_group(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=EmbedBuilder.info(
                "Message Logging",
                "Subcommands: `enable`, `disable`, `channel`, `edits`, `deletes`, `ignore`, `status`.",
            ), ephemeral=True)


    @msglog_group.command(name="enable", description="Turn ON full message logging")
    @app_commands.describe(channel="The channel to send logs to")
    @commands.has_permissions(manage_guild=True)
    async def msglog_enable(ctx: commands.Context, channel: discord.TextChannel) -> None:
        cfg = MessageLogSystem.get_config(ctx.guild.id)
        cfg['enabled'] = 1
        cfg['log_channel_id'] = channel.id
        cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
        MessageLogSystem.save_config(cfg)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Message Logging Enabled ✅",
            f"Edits + deletes will be logged to {channel.mention}.\n_(Bot messages are ignored by default.)_",
        ), ctx.guild.id))


    @msglog_group.command(name="disable", description="Turn OFF full message logging")
    @commands.has_permissions(manage_guild=True)
    async def msglog_disable(ctx: commands.Context) -> None:
        cfg = MessageLogSystem.get_config(ctx.guild.id)
        cfg['enabled'] = 0
        cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
        MessageLogSystem.save_config(cfg)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.warning(
            "Message Logging Disabled", "Edits and deletes will no longer be logged."
        ), ctx.guild.id))


    @msglog_group.command(name="channel", description="Change the log channel")
    @app_commands.describe(channel="The channel to send logs to")
    @commands.has_permissions(manage_guild=True)
    async def msglog_channel(ctx: commands.Context, channel: discord.TextChannel) -> None:
        cfg = MessageLogSystem.get_config(ctx.guild.id)
        cfg['log_channel_id'] = channel.id
        cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
        MessageLogSystem.save_config(cfg)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Log Channel Updated", f"Logs will go to {channel.mention}."
        ), ctx.guild.id))


    @msglog_group.command(name="edits", description="Toggle edit logging on/off")
    @app_commands.describe(enabled="True to log edits, False to stop")
    @commands.has_permissions(manage_guild=True)
    async def msglog_edits(ctx: commands.Context, enabled: bool) -> None:
        cfg = MessageLogSystem.get_config(ctx.guild.id)
        cfg['log_edits'] = 1 if enabled else 0
        cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
        MessageLogSystem.save_config(cfg)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Edit Logging Updated", f"Edit logging is now **{'ON' if enabled else 'OFF'}**."
        ), ctx.guild.id))


    @msglog_group.command(name="deletes", description="Toggle delete logging on/off")
    @app_commands.describe(enabled="True to log deletes, False to stop")
    @commands.has_permissions(manage_guild=True)
    async def msglog_deletes(ctx: commands.Context, enabled: bool) -> None:
        cfg = MessageLogSystem.get_config(ctx.guild.id)
        cfg['log_deletes'] = 1 if enabled else 0
        cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
        MessageLogSystem.save_config(cfg)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Delete Logging Updated", f"Delete logging is now **{'ON' if enabled else 'OFF'}**."
        ), ctx.guild.id))


    @msglog_group.command(name="ignore", description="Add/remove a channel from the ignore list")
    @app_commands.describe(channel="The channel to toggle ignore status for")
    @commands.has_permissions(manage_guild=True)
    async def msglog_ignore(ctx: commands.Context, channel: discord.TextChannel) -> None:
        cfg = MessageLogSystem.get_config(ctx.guild.id)
        try:
            ignored = json.loads(cfg.get('ignore_channels', '[]') or '[]')
        except Exception:
            ignored = []
        if channel.id in ignored:
            ignored.remove(channel.id)
            state = "no longer ignored"
        else:
            ignored.append(channel.id)
            state = "now ignored"
        cfg['ignore_channels'] = json.dumps(ignored)
        cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
        MessageLogSystem.save_config(cfg)
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
            "Ignore List Updated", f"{channel.mention} is **{state}**."
        ), ctx.guild.id))


    @msglog_group.command(name="status", description="Show the current message-logging configuration")
    @commands.has_permissions(manage_guild=True)
    async def msglog_status(ctx: commands.Context) -> None:
        cfg = MessageLogSystem.get_config(ctx.guild.id)
        chan = ctx.guild.get_channel(cfg.get('log_channel_id') or 0)
        try:
            ignored = json.loads(cfg.get('ignore_channels', '[]') or '[]')
        except Exception:
            ignored = []
        embed = discord.Embed(
            title="📝 Message Logging Status",
            description=(
                f"Enabled: **{'Yes ✅' if cfg.get('enabled') else 'No ❌'}**\n"
                f"Log channel: {chan.mention if chan else '_(not set)_'}\n"
                f"Log edits: **{'Yes' if cfg.get('log_edits', 1) else 'No'}**\n"
                f"Log deletes: **{'Yes' if cfg.get('log_deletes', 1) else 'No'}**\n"
                f"Ignore bots: **{'Yes' if cfg.get('ignore_bots', 1) else 'No'}**\n"
                f"Ignored channels: **{len(ignored)}**"
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        if ignored:
            lines = [ctx.guild.get_channel(c).mention if ctx.guild.get_channel(c) else f"`{c}`" for c in ignored]
            embed.add_field(name="Ignored", value="\n".join(lines), inline=False)
        await ctx.send(embed=EmbedBuilder.branded(embed, ctx.guild.id))
