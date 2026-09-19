# -*- coding: utf-8 -*-
"""Giveaways — creation command, entry view, draw task + persistence."""

# stdlib + discord.py
import asyncio
import discord
import logging
import random
import threading
from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View
from typing import Dict, Optional

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from utils.ui.embeds import EmbedBuilder




import uuid





# --- GIVEAWAY SYSTEM (V2 Enhancement) ---
# Per-giveaway locks serialize entry handling so two users clicking at the
# same instant can't both be appended before the entry count / button label
# updates (the race condition that previously left the count briefly wrong).
_giveaway_entry_locks: Dict[str, asyncio.Lock] = {}
_giveaway_locks_guard = threading.Lock()


def _get_giveaway_lock(giveaway_id: str) -> asyncio.Lock:
    """Return (creating if needed) the asyncio.Lock for a given giveaway.

    Locks are created lazily so we don't construct asyncio primitives at import
    time (which can warn on some Python versions if no loop is running yet).
    """
    with _giveaway_locks_guard:
        lock = _giveaway_entry_locks.get(giveaway_id)
        if lock is None:
            lock = asyncio.Lock()
            _giveaway_entry_locks[giveaway_id] = lock
        return lock


def _drop_giveaway_lock(giveaway_id: str) -> None:
    """Free the lock for a giveaway once it has ended (keeps the dict small)."""
    with _giveaway_locks_guard:
        _giveaway_entry_locks.pop(giveaway_id, None)


class GiveawayView(View):
    def __init__(self, giveaway_id: str):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="🎉 Enter Giveaway", style=discord.ButtonStyle.success)
    async def enter_button(self, interaction: discord.Interaction, button: Button) -> None:
        giveaway_id = self.giveaway_id
        if giveaway_id not in state.giveaways_data:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Giveaway Ended", "This giveaway no longer exists."),
                ephemeral=True,
            )
            return

        # Serialize entries for this giveaway. The check-then-append + label
        # update now happens atomically under the lock, eliminating the race
        # where two near-simultaneous clicks both passed the "already entered"
        # check and both got appended.
        lock = _get_giveaway_lock(giveaway_id)
        async with lock:
            if giveaway_id not in state.giveaways_data:
                await interaction.response.send_message(
                    embed=EmbedBuilder.error("Giveaway Ended", "This giveaway no longer exists."),
                    ephemeral=True,
                )
                return

            giveaway = state.giveaways_data[giveaway_id]

            if giveaway.get('status') != 'active':
                await interaction.response.send_message(
                    embed=EmbedBuilder.warning("Giveaway Closed", "This giveaway has already ended."),
                    ephemeral=True,
                )
                return

            entries = giveaway.setdefault('entries', [])
            if interaction.user.id in entries:
                await interaction.response.send_message(
                    embed=EmbedBuilder.info("Already Entered", "You've already entered this giveaway! Sit tight for the draw."),
                    ephemeral=True,
                )
                return

            entries.append(interaction.user.id)
            entry_count = len(entries)

            # Save ONLY this specific giveaway to SQLite (fast, low disk I/O)
            save_giveaways_data(giveaway_id)

            # Update the button label AND the message view in a single
            # interaction response (atomic from the user's perspective).
            button.label = f"🎉 Entered ({entry_count})"
            try:
                await interaction.response.edit_message(view=self)
            except discord.InteractionResponded:
                # Fallback: response was already made somehow; edit the message.
                try:
                    await interaction.message.edit(view=self)
                except (discord.HTTPException, discord.Forbidden):
                    pass
            except (discord.HTTPException, discord.Forbidden):
                pass

        # Confirmation is sent as a followup so it doesn't conflict with the
        # edit_message response above.
        try:
            await interaction.followup.send(
                embed=EmbedBuilder.success("✅ You're In!", f"You've been entered — good luck! 🍀\n\nTotal entrants so far: **{entry_count}**"),
                ephemeral=True,
            )
        except (discord.HTTPException, discord.Forbidden):
            pass


def save_giveaways_data(giveaway_id: Optional[str] = None) -> None:
    """Save giveaways data to SQLite. If giveaway_id is provided, only saves that one."""
    try:
        if giveaway_id:
            g = state.giveaways_data.get(giveaway_id)
            if g:
                data_manager.save_giveaway(g)
        else:
            for g in state.giveaways_data.values():
                data_manager.save_giveaway(g)
    except Exception as e:
        logging.error(f"[Giveaways] Error saving data: {e}")

def load_giveaways_data() -> None:
    """Load giveaways data from SQLite so active giveaways survive a restart."""
    try:
        state.giveaways_data = data_manager.load_all_giveaways()
        logging.info(f"[Giveaways] Loaded {len(state.giveaways_data)} giveaway(s) from SQLite")
    except Exception as e:
        logging.error(f"[Giveaways] Error loading data, starting fresh: {e}")
        state.giveaways_data = {}

def reset_giveaways_data() -> None:
    """Clear giveaways data (used on shutdown if desired)."""
    state.giveaways_data = {}


@tasks.loop(minutes=1)
async def check_giveaways_task() -> None:
    """Check for ended giveaways."""
    if not config.enable_giveaways:
        return
    
    now = datetime.now(timezone.utc)
    to_end = []
    
    for giveaway_id, giveaway in state.giveaways_data.items():
        if giveaway.get('status') != 'active':
            continue
        try:
            ends_at = datetime.fromisoformat(giveaway['ends_at'])
            if ends_at <= now:
                to_end.append(giveaway_id)
        except:
            pass
    
    for giveaway_id in to_end:
        await end_giveaway(giveaway_id)


async def end_giveaway(giveaway_id: str) -> None:
    """End a giveaway and pick winners.

    Acquires the SAME per-giveaway asyncio.Lock that the entry button uses,
    so a late entry can't be appended to `entries` while we're mid-selection.
    Previously, end_giveaway read the entry list and chose winners without
    the lock, so an in-flight Enter click could append a user AFTER the
    winner list was frozen — that user would be silently excluded.
    """
    if giveaway_id not in state.giveaways_data:
        return

    # Hold the entry lock for the whole end operation: read entries, pick
    # winners, flip status to 'ended', and persist — all atomically with
    # respect to concurrent Enter clicks.
    lock = _get_giveaway_lock(giveaway_id)
    async with lock:
        if giveaway_id not in state.giveaways_data:
            return
        giveaway = state.giveaways_data[giveaway_id]

        # If another caller already ended it, don't double-end (avoids a
        # second winner pick + a duplicate "Giveaway Ended" broadcast).
        if giveaway.get('status') != 'active':
            return

        entries = giveaway.get('entries', [])
        winner_count = giveaway.get('winner_count', 1)

        if entries:
            winners = random.sample(entries, min(winner_count, len(entries)))
            giveaway['winners'] = winners
        else:
            giveaway['winners'] = []

        giveaway['status'] = 'ended'
        giveaway['ended_at'] = datetime.now(timezone.utc).isoformat()
        save_giveaways_data(giveaway_id)  # Save only the updated giveaway to SQLite

        channel = state.bot.get_channel(giveaway.get('channel_id'))
        if channel:
            winners = giveaway.get('winners', [])
            if winners:
                winners_mention = " ".join(f"<@{wid}>" for wid in winners)
                embed = EmbedBuilder.success(
                    "🎉 Giveaway Ended!",
                    f"**Prize:** {giveaway.get('prize', 'Unknown')}\n"
                    f"**Winners:** {winners_mention}\n"
                    f"**Total Entries:** {len(entries)}"
                )
            else:
                embed = EmbedBuilder.warning(
                    "🎉 Giveaway Ended",
                    f"**Prize:** {giveaway.get('prize', 'Unknown')}\n"
                    f"No valid entries received."
                )

            try:
                message = await channel.fetch_message(giveaway.get('message_id', 0))
                await message.edit(embed=embed, view=None)
            except Exception:
                pass

            await channel.send(embed=embed)

    logging.info(f"[Giveaway] Ended giveaway {giveaway_id}")
    # Release the per-giveaway entry lock now that the giveaway is over.
    _drop_giveaway_lock(giveaway_id)


@check_giveaways_task.before_loop
async def before_check_giveaways() -> None:
    await state.bot.wait_until_ready()

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""

    # GIVEAWAY COMMANDS (V2 Enhancement)
    # =============================================================================
    @bot.command(name="giveaway", description="Create a giveaway")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(duration="Duration in hours", winners="Number of winners", prize="The prize")
    async def giveaway_cmd(ctx: commands.Context, duration: int, winners: int, *, prize: str) -> None:
        if not config.enable_giveaways:
            await ctx.send("Giveaway system is disabled.")
            return
    
        if winners > config.limits.max_giveaway_winners:
            await ctx.send(f"Maximum {config.limits.max_giveaway_winners} winners allowed.")
            return
    
        import uuid
        giveaway_id = str(uuid.uuid4())[:8]
    
        giveaway = {
            'giveaway_id': giveaway_id,
            'message_id': 0,
            'channel_id': ctx.channel.id,
            'guild_id': ctx.guild.id,
            'host_id': ctx.author.id,
            'prize': prize,
            'winner_count': winners,
            'entries': [],
            'status': 'active',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'ends_at': (datetime.now(timezone.utc) + timedelta(hours=duration)).isoformat()
        }
    
        embed = EmbedBuilder.giveaway(
            prize,
            f"**Hosted by:** {ctx.author.mention}\n"
            f"**Winners:** {winners}\n"
            f"**Ends:** <t:{int(datetime.fromisoformat(giveaway['ends_at']).timestamp())}:R>\n"
            f"**Entries:** 0\n\n"
            f"Click the button below to enter!"
        )
    
        view = GiveawayView(giveaway_id)
        message = await ctx.send(embed=embed, view=view)
    
        giveaway['message_id'] = message.id
        state.giveaways_data[giveaway_id] = giveaway
        save_giveaways_data()
    
        logging.info(f"[Giveaway] Created by {ctx.author}: {prize}")


    @bot.command(name="endgiveaway", description="End a giveaway early")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(giveaway_id="The giveaway ID to end")
    async def endgiveaway_cmd(ctx: commands.Context, giveaway_id: str) -> None:
        if giveaway_id not in state.giveaways_data:
            await ctx.send("Giveaway not found.")
            return
    
        await end_giveaway(giveaway_id)
        await ctx.send(f"Giveaway `{giveaway_id}` has been ended.")
