# -*- coding: utf-8 -*-
"""Channels — auto-purge manager, purge/nopurge commands."""

# stdlib + discord.py
import asyncio
import discord
import logging
import re
import time
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands
from typing import Dict, List, Optional, Set

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from utils.ui.embeds import EmbedBuilder
from modules.moderation.msglog import MessageLogSystem




# =========================================================================
# VERIFICATION CHANNEL AUTO-PURGE SYSTEM
# =========================================================================
# Once the verification channel has 12+ messages and is inactive for 3+
# minutes, the bot posts a warning: the messages will be deleted in 2 minutes
# if the channel stays inactive. If anyone sends a message during the warning
# window the purge is cancelled. If it stays quiet, the messages are deleted.
#
# Messages marked with !nopurge are NEVER deleted here (or by !purge/!purgeall)
# — they are skipped during deletion. The bot's own warning message is tracked
# separately and deleted/cleaned up so it doesn't count toward the next cycle.
# =========================================================================
class AutoPurgeManager:
    """Manages the verification-channel auto-purge lifecycle.

    One instance per verification channel. The manager is stateful:
      - last_activity_ts:   updated on every human message in the channel
      - warning_task:        a background asyncio task that waits 3 min, posts
                             the warning, waits 2 more min, then purges
      - warning_message_id:  the warning message's id (so we can delete it)
      - armed:               whether a purge cycle is currently in progress

    Activity in the channel cancels the current cycle (if armed) and
    re-evaluates. The re-evaluation happens via a 3-minute idle wait, so we
    don't hammer the channel.
    """

    THRESHOLD_MESSAGES = 12   # 12+ messages -> eligible for purge
    IDLE_BEFORE_WARNING = 180  # 3 minutes of inactivity before the warning
    WARNING_WINDOW = 120       # 2 minutes after the warning before deletion

    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        self.last_activity_ts: float = time.time()
        self.warning_task: Optional[asyncio.Task] = None
        self.warning_message_id: Optional[int] = None
        self.armed: bool = False
        self._lock = asyncio.Lock()

    async def record_activity(self) -> None:
        """Call on EVERY message in the verification channel.

        Cancels an in-progress warning cycle (if any), then re-arms a fresh
        one after the idle window — but only if the channel is over the
        message threshold. Cancelling on activity is what satisfies the
        "if it becomes active again during those 2 minutes, cancel the purge"
        requirement.
        """
        self.last_activity_ts = time.time()
        async with self._lock:
            await self._cancel_warning_task()
        # (Re)arm a fresh idle watcher.
        await self._schedule_idle_check()

    async def _schedule_idle_check(self) -> None:
        """Spawn (or replace) the idle-watcher background task."""
        # Cancel any existing watcher first — only one idle check at a time.
        if self.warning_task and not self.warning_task.done():
            self.warning_task.cancel()
            try:
                await self.warning_task
            except (asyncio.CancelledError, Exception):
                pass
        self.warning_task = asyncio.create_task(self._idle_watch_loop())

    async def _cancel_warning_task(self) -> None:
        """Cancel any running purge cycle and clean up the warning message."""
        self.armed = False
        if self.warning_task and not self.warning_task.done():
            self.warning_task.cancel()
            try:
                await self.warning_task
            except (asyncio.CancelledError, Exception):
                pass
            self.warning_task = None
        # Delete the warning message so it doesn't linger / count toward
        # the next cycle's message count.
        if self.warning_message_id is not None:
            channel = state.bot.get_channel(self.channel_id)
            if channel is not None:
                try:
                    msg = await channel.fetch_message(self.warning_message_id)
                    await msg.delete()
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    pass
            self.warning_message_id = None

    async def _idle_watch_loop(self) -> None:
        """Wait IDLE_BEFORE_WARNING seconds; if still idle, arm the purge."""
        try:
            await asyncio.sleep(self.IDLE_BEFORE_WARNING)
        except asyncio.CancelledError:
            return  # Activity happened — cancelled, fresh cycle started elsewhere.
        # Did activity happen while we were sleeping? If so, bail; the new
        # cycle (started by record_activity) owns the next idle window.
        if time.time() - self.last_activity_ts < self.IDLE_BEFORE_WARNING:
            return
        # Threshold check: only purge if the channel has 12+ messages.
        channel = state.bot.get_channel(self.channel_id)
        if channel is None:
            return
        try:
            recent = [m async for m in channel.history(limit=self.THRESHOLD_MESSAGES)]
        except (discord.HTTPException, discord.Forbidden):
            return
        if len(recent) < self.THRESHOLD_MESSAGES:
            return  # Not enough messages yet — nothing to do.
        # Enough messages + idle -> post the warning and arm the purge.
        await self._post_warning_and_arm(channel)

    async def _post_warning_and_arm(self, channel: discord.TextChannel) -> None:
        """Post the warning embed and start the 2-minute countdown."""
        self.armed = True
        warning_embed = discord.Embed(
            title="⚠️ Auto-Purge Warning",
            description=(
                f"This channel has been inactive for **{self.IDLE_BEFORE_WARNING // 60} minutes** "
                f"with **{self.THRESHOLD_MESSAGES}+** messages.\n\n"
                f"The messages will be **automatically deleted in {self.WARNING_WINDOW // 60} minutes** "
                f"if the channel remains inactive.\n\n"
                f"Send a message to **cancel** the purge. Messages marked with "
                f"`!nopurge` are always preserved."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        warning_embed.set_footer(text="Verification Auto-Purge • Inactivity cleanup")
        try:
            warning_msg = await channel.send(embed=warning_embed)
            self.warning_message_id = warning_msg.id
        except (discord.HTTPException, discord.Forbidden):
            self.armed = False
            return
        logging.info(
            f"[AutoPurge] Warning posted in verification channel {channel.id} "
            f"({len(await self._count_deletable(channel))} deletable messages)"
        )
        # Wait the warning window. If activity arrives, record_activity()
        # cancels this task and we never reach the purge.
        try:
            await asyncio.sleep(self.WARNING_WINDOW)
        except asyncio.CancelledError:
            # Cancelled by new activity -> purge cancelled.
            return
        # If we got here without being cancelled, the channel stayed quiet.
        if not self.armed:
            return
        await self._execute_purge(channel)

    async def _count_deletable(self, channel: discord.TextChannel) -> List[discord.Message]:
        """Return the list of messages that WOULD be deleted (excludes no-purge + the warning)."""
        deletable: List[discord.Message] = []
        async for m in channel.history(limit=200):
            # Never delete no-purge messages.
            if data_manager.is_no_purge(m.id):
                continue
            # Never delete our own warning message via this path.
            if self.warning_message_id is not None and m.id == self.warning_message_id:
                continue
            deletable.append(m)
        return deletable

    async def _execute_purge(self, channel: discord.TextChannel) -> None:
        """Delete all non-protected messages in the channel."""
        deletable = await self._count_deletable(channel)
        if not deletable:
            # Nothing to delete (all protected). Still clean up the warning.
            await self._cancel_warning_task()
            return
        # Log to the message-log system first (bulk-delete doesn't fire on_delete).
        try:
            await MessageLogSystem.log_bulk_delete(
                deletable, state.bot.user, channel,
                reason="verification auto-purge (inactivity)",
            )
        except Exception as e:
            logging.warning(f"[AutoPurge] msglog sync failed (non-fatal): {e}")
        # Bulk-delete in chunks of 100 (Discord hard limit).
        deleted_count = 0
        for i in range(0, len(deletable), 100):
            batch = deletable[i:i + 100]
            try:
                await channel.delete_messages(batch)
                deleted_count += len(batch)
            except discord.HTTPException:
                # Fallback: delete one at a time (too old for bulk).
                for m in batch:
                    try:
                        await m.delete()
                        deleted_count += 1
                    except (discord.HTTPException, discord.NotFound):
                        pass
            except (discord.Forbidden, discord.NotFound):
                pass
            if i + 100 < len(deletable):
                await asyncio.sleep(1)  # be nice to the rate limiter
        logging.info(f"[AutoPurge] Deleted {deleted_count} messages from verification channel {channel.id}")
        # Clean up the warning message.
        await self._cancel_warning_task()
        self.armed = False
        # Post a brief completion notice (auto-deletes itself).
        try:
            done_embed = discord.Embed(
                title="🧞️ Auto-Purge Complete",
                description=f"Deleted **{deleted_count}** inactive messages. "
                            f"No-purge-protected messages were preserved.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            done_embed.set_footer(text="Verification Auto-Purge")
            done_msg = await channel.send(embed=done_embed, delete_after=15)
        except (discord.HTTPException, discord.Forbidden):
            pass


# Per-channel auto-purge managers (keyed by verification channel id).
# There's normally one verification channel per server, but this map keeps
# the design clean if multiple servers share the bot.
auto_purge_managers: Dict[int, AutoPurgeManager] = {}


def get_auto_purge_manager(channel_id: int) -> AutoPurgeManager:
    """Get (or create) the AutoPurgeManager for a verification channel."""
    mgr = auto_purge_managers.get(channel_id)
    if mgr is None:
        mgr = AutoPurgeManager(channel_id)
        auto_purge_managers[channel_id] = mgr
    return mgr


def is_verification_channel(channel_id: int, channel=None) -> bool:
    """True if the given channel is the verification channel.

    Checks in this order:
      1. Exact match against config.channels.verification_main (if set/nonzero).
      2. Name-based fallback: the channel's cleaned name is "verify" or
         "verification" (after stripping emoji/decorations).

    The name fallback is STRICT — it only matches channels whose name IS
    "verify"/"verification" (after cleaning), NOT sub-channels like
    "verification-responses" or "verification-help". This prevents the
    auto-purge from arming in the wrong channel.

    Pass `channel=` (the discord channel object) when available to avoid a
    bot.get_channel() cache lookup that can return None if the channel isn't
    cached yet (which was causing !nopurge to be rejected in the verification
    channel even when the user was standing in it).
    """
    # 1. Config-based exact match.
    if channel_id == config.channels.verification_main:
        return True
    # 2. Name-based fallback: use the provided channel object, or look it up.
    ch = channel if channel is not None else state.bot.get_channel(channel_id)
    if ch is not None and hasattr(ch, 'name') and ch.name:
        # Strip everything except a-z0-9 so emoji/decorations are removed.
        # "✅・Verification" -> "verification" -> matches.
        # "verification-responses" -> "verificationresponses" -> does NOT match.
        cleaned = re.sub(r'[^a-z0-9]', '', ch.name.lower())
        return cleaned in ('verify', 'verification')
    return False

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    @bot.command(name="nopurge", description="Protect message(s) from auto-purge/purge/purgeall (verification channel only)")
    @commands.has_permissions(manage_messages=True)
    @app_commands.describe(message_ids="One or more message IDs, separated by commas or spaces (right-click message -> Copy Message ID)")
    async def nopurge_cmd(ctx: commands.Context, *, message_ids: str) -> None:
        """Mark one or more messages as permanently excluded from all purge systems.

    - Works ONLY in the verification channel.
    - Supports MULTIPLE IDs, separated by commas and/or spaces:
        !nopurge 123456789, 987654321, 111222333
        !nopurge 123456789 987654321 111222333
        !nopurge 123456789,987654321
    - Each valid message ID is persisted to SQLite, so it survives restarts.
    - Protected messages are never deleted by the auto-purge system, !purge,
    or !purgeall — all three check the same exclusion set.
    """
        # Enforce verification-channel-only usage.
        if not is_verification_channel(ctx.channel.id, ctx.channel):
            await ctx.send(
                embed=EmbedBuilder.warning(
                    "Verification Channel Only",
                    "`!nopurge` can only be used in the verification channel.",
                ),
                delete_after=15,
            )
            return

        # Parse the message IDs. Accept commas, spaces, or newlines as separators.
        # This handles all of: "123, 456, 789", "123 456 789", "123,456,789".
        raw_tokens = re.split(r'[,\s]+', message_ids.strip())
        parsed_ids: List[int] = []
        invalid_tokens: List[str] = []
        for token in raw_tokens:
            token = token.strip()
            if not token:
                continue
            try:
                parsed_ids.append(int(token))
            except ValueError:
                invalid_tokens.append(token)

        if not parsed_ids:
            await ctx.send(
                embed=EmbedBuilder.error(
                    "No Valid Message IDs",
                    "Please provide at least one valid message ID.\n"
                    "**Usage:** `!nopurge 123456789` or `!nopurge 123, 456, 789`",
                ),
                delete_after=15,
            )
            return

        # Deduplicate while preserving order.
        seen: Set[int] = set()
        unique_ids: List[int] = []
        for mid in parsed_ids:
            if mid not in seen:
                seen.add(mid)
                unique_ids.append(mid)

        guild_id = ctx.guild.id if ctx.guild else 0

        # Process each message ID: validate it exists in this channel, then persist.
        protected: List[int] = []
        already_protected: List[int] = []
        not_found: List[int] = []
        errors: List[str] = []

        for msg_id in unique_ids:
            # Skip if already protected (don't waste API calls).
            if data_manager.is_no_purge(msg_id):
                already_protected.append(msg_id)
                continue
            # Validate the message actually exists in this channel.
            try:
                target = await ctx.channel.fetch_message(msg_id)
            except discord.NotFound:
                not_found.append(msg_id)
                continue
            except (discord.Forbidden, discord.HTTPException) as exc:
                errors.append(f"`{msg_id}`: {exc}")
                continue
            # Persist to SQLite + update the in-memory cache.
            data_manager.save_no_purge_message(msg_id, ctx.channel.id, guild_id, ctx.author.id)
            protected.append(msg_id)
            logging.info(f"[NoPurge] {ctx.author} protected message {msg_id} in verification channel {ctx.channel.id}")

        # Build the result embed.
        status_lines: List[str] = []
        if protected:
            status_lines.append(f"✅ **Protected ({len(protected)}):** {', '.join(f'`{m}`' for m in protected)}")
        if already_protected:
            status_lines.append(f"⏳ **Already protected ({len(already_protected)}):** {', '.join(f'`{m}`' for m in already_protected)}")
        if not_found:
            status_lines.append(f"❌ **Not found ({len(not_found)}):** {', '.join(f'`{m}`' for m in not_found)}")
        if invalid_tokens:
            status_lines.append(f"⚠️ **Invalid IDs:** {', '.join(f'`{t}`' for t in invalid_tokens)}")
        if errors:
            status_lines.append(f"⚠️ **Errors:** {len(errors)} message(s) could not be fetched")

        if not protected and not already_protected:
            # Nothing was protected — report the errors.
            embed = EmbedBuilder.error(
                "No Messages Protected",
                "\n".join(status_lines) or "No valid message IDs were found.",
            )
            await ctx.send(embed=embed, delete_after=30)
            return

        embed = discord.Embed(
            title="🛡️ Messages Protected",
            description=(
                "\n".join(status_lines) +
                f"\n\nProtected messages are excluded from:\n"
                f"• The verification **auto-purge** system\n"
                f"• `!purge`\n"
                f"• `!purgeall`\n\n"
                f"This protection is **permanent** and survives bot restarts."
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Protected by {ctx.author.display_name}")
        await ctx.send(embed=embed)


    # --- PURGE / CHANNEL COMMANDS ---
    @bot.command(name="purge", aliases=["purgeall"], description="Purge a specific amount of messages or the entire channel")
    @commands.has_permissions(manage_messages=True)
    @app_commands.describe(
        amount="Number of messages to delete. Leave empty to delete the whole channel.",
        flags="Optional flags. Use '-del' to also delete the !purge command message and skip the confirmation reply.",
    )
    async def purge_cmd(
        ctx: commands.Context,
        amount: Optional[int] = None,
        *,
        flags: str = "",
    ) -> None:
        """Purges a specific amount of messages, or the entire channel if no amount is given.

    NEW FLAGS / FIXES:
      - `-del` flag (e.g. `!purge 2 -del`):
          * Also deletes the `!purge` command message itself after purging.
          * Skips the "Deleted N messages." confirmation reply entirely.
          * The cleanup-command prompt (for chained commands) is also skipped,
            since the command message is gone.
      - Message-log sync:
          * discord.py's `TextChannel.purge()` uses the bulk-delete endpoint,
            which does NOT fire `on_message_delete` for individual messages.
            So previously, purged messages never appeared in the msg log.
          * We now fetch the target messages first, log them via
            `MessageLogSystem.log_bulk_delete`, THEN bulk-delete. The log
            channel gets a single compact summary embed (not one per msg).
      - `before=ctx.message`:
          * Keeps the command message out of the fetch set entirely (the old
            `check=` approach wasted a `limit` slot on the command message).
    """
        # `flags` is a keyword-only string that absorbs any trailing tokens after
        # `amount` for prefix invocations (e.g. `!purge 2 -del` -> amount=2,
        # flags="-del"). The user passes flags="-del" explicitly.
        delete_command = False
        # Check both the raw message content (most reliable for prefix) and the
        # parsed `flags` kwarg (covers edge cases).
        if ctx.message is not None and ctx.message.content:
            content_lower = ctx.message.content.lower()
            if ' -del' in content_lower or content_lower.endswith('-del'):
                delete_command = True
        if flags and '-del' in flags.lower():
            delete_command = True

        before_obj = ctx.message
        cmd_id = ctx.message.id if ctx.message else None

        def _skip_command(m: discord.Message) -> bool:
            return cmd_id is None or m.id != cmd_id

        # Counter for messages SKIPPED because they are !nopurge-protected.
        # Reported back to the user so they know protection worked.
        protected_skipped = 0

        # Helper that fetches a batch of messages, logs them to the msg log,
        # then bulk-deletes them. Returns the list of deleted messages.
        # We do the fetch+log+delete ourselves (instead of ctx.channel.purge)
        # so we can log each message before it's gone — purge()'s bulk delete
        # never fires on_message_delete, which is why purged msgs were missing
        # from the msg log.
        #
        # NO-PURGE EXCLUSION: messages marked with !nopurge (persisted in the
        # no_purge_messages table) are NEVER deleted here — they're filtered out
        # of the batch before logging/deletion. This is the SAME exclusion set the
        # auto-purge system and !purgeall use, so protection is consistent across
        # all three purge paths.
        async def _fetch_log_delete(channel: discord.TextChannel, limit: int, before) -> List[discord.Message]:
            nonlocal protected_skipped
            batch: List[discord.Message] = []
            async for m in channel.history(limit=limit, before=before, oldest_first=False):
                # Skip the command message itself AND any !nopurge-protected msg.
                if not _skip_command(m):
                    continue
                if data_manager.is_no_purge(m.id):
                    protected_skipped += 1
                    continue
                batch.append(m)
            if not batch:
                return []
            # Log to the msg-log channel BEFORE deleting.
            try:
                await MessageLogSystem.log_bulk_delete(
                    batch, ctx.author, channel,
                    reason=f"purge {limit}" if amount else "purge all",
                )
            except Exception as e:
                logging.warning(f"[Purge] msglog sync failed (non-fatal): {e}")
            # Bulk-delete. delete_messages accepts up to 100 messages at once.
            try:
                await channel.delete_messages(batch)
            except discord.HTTPException:
                # Fallback: delete one at a time (some are too old for bulk).
                for m in batch:
                    try:
                        await m.delete()
                    except (discord.HTTPException, discord.NotFound):
                        pass
            return batch

        if amount is None:
            # --- PURGE ALL LOGIC ---
            deleted_messages: List[discord.Message] = []
            while True:
                try:
                    batch = await _fetch_log_delete(ctx.channel, 100, before_obj)
                    deleted_messages.extend(batch)
                    if len(batch) < 100:
                        break
                    await asyncio.sleep(1)
                except discord.HTTPException as e:
                    if e.status == 429:
                        retry_after = int(e.response.headers.get('Retry-After', 1)) if hasattr(e.response, 'headers') else 1
                        await asyncio.sleep(retry_after)
                    else:
                        break

            deleted_count = len(deleted_messages)
            logging.info(f'{ctx.author} purged all ({deleted_count}) messages in {ctx.channel}')

            if not delete_command:
                summary = f'Deleted {deleted_count} messages.'
                if protected_skipped:
                    summary += f' ({protected_skipped} protected by !nopurge)'
                message = await ctx.send(summary)
                await asyncio.sleep(2)
                try:
                    await message.delete()
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    pass
            else:
                # -del: delete the command message itself. The cleanup prompt for
                # chained commands is also skipped (see process_potential_multi_command).
                if cmd_id is not None:
                    try:
                        await ctx.message.delete()
                    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                        pass

        else:
            # --- PURGE SPECIFIC AMOUNT LOGIC ---
            if amount <= 0:
                await ctx.send("Amount must be a positive number.", ephemeral=True)
                return

            if before_obj is not None:
                deleted_messages = await _fetch_log_delete(ctx.channel, amount, before_obj)
            else:
                # Fallback: fetch one extra to compensate for the skipped
                # command message, then filter it out before logging/deleting.
                # Also filters out !nopurge-protected messages (same exclusion set
                # as purge-all and the auto-purge system).
                batch: List[discord.Message] = []
                async for m in ctx.channel.history(limit=amount + 1, oldest_first=False):
                    if not _skip_command(m):
                        continue
                    if data_manager.is_no_purge(m.id):
                        protected_skipped += 1
                        continue
                    batch.append(m)
                if batch:
                    try:
                        await MessageLogSystem.log_bulk_delete(
                            batch, ctx.author, ctx.channel,
                            reason=f"purge {amount}",
                        )
                    except Exception as e:
                        logging.warning(f"[Purge] msglog sync failed (non-fatal): {e}")
                    try:
                        await ctx.channel.delete_messages(batch)
                    except discord.HTTPException:
                        for m in batch:
                            try:
                                await m.delete()
                            except (discord.HTTPException, discord.NotFound):
                                pass
                deleted_messages = batch

            deleted_count = len(deleted_messages)
            logging.info(f'{ctx.author} purged {deleted_count} messages in {ctx.channel}')

            if not delete_command:
                summary = f'Deleted {deleted_count} messages.'
                if protected_skipped:
                    summary += f' ({protected_skipped} protected by !nopurge)'
                message = await ctx.send(summary)
                await asyncio.sleep(2)
                try:
                    await message.delete()
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    pass
            else:
                # -del: also delete the original command message.
                if cmd_id is not None:
                    try:
                        await ctx.message.delete()
                    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                        pass
