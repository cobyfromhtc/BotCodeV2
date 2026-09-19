# -*- coding: utf-8 -*-
"""Polls — native multi-option polls with live result bars."""

# stdlib + discord.py
import asyncio
import discord
import logging
import time
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands
from typing import Dict, List, Tuple

from core.state import config
from utils.ui.embeds import EmbedBuilder



# Replace your corrupted emoji list with this:
POLL_EMOJIS = [
    "1️⃣",  # 1️⃣
    "2️⃣",  # 2️⃣
    "3️⃣",  # 3️⃣
    "4️⃣",  # 4️⃣
    "5️⃣",  # 5️⃣
    "6️⃣",  # 6️⃣
    "7️⃣",  # 7️⃣
    "8️⃣",  # 8️⃣
    "9️⃣",  # 9️⃣
]


# --- POLL COMMAND ---

# Width-15 progress bar with partial blocks for smoother granularity:
# full=█, then descending partials ▓▒░ (a 1/3, 2/3, empty approximation).
_POLL_BAR_WIDTH = 15
_POLL_BAR_CHARS = "░▒▓█"


def _poll_bar(pct: float) -> str:
    """Render a 15-character progress bar from a percentage (0–100)."""
    pct = max(0.0, min(100.0, pct))
    total_units = pct / 100.0 * _POLL_BAR_WIDTH
    full = int(total_units)
    remainder = total_units - full
    # Pick the closest partial block for the fractional part.
    if remainder >= 0.667:
        bar = "█" * full + "▓"
    elif remainder >= 0.333:
        bar = "█" * full + "▒"
    elif remainder > 0:
        bar = "█" * full + "░"
    else:
        bar = "█" * full
    bar += "░" * (_POLL_BAR_WIDTH - len(bar))
    return bar


async def _run_poll(poll_msg: discord.Message, author: discord.abc.User, question: str, options: Tuple[str, ...], end_time: float) -> None:
    """
    Background task that live-updates a poll until it ends, then shows results.

    Runs detached from the command so multiple concurrent polls don't pin a
    command task each. Exits cleanly if the poll message is deleted or becomes
    inaccessible.
    """
    results: Dict[int, int] = {}
    total_votes = 0
    try:
        while time.time() < end_time:
            await asyncio.sleep(15)
            try:
                poll_msg, results, total_votes = await _update_poll(poll_msg, author, question, options, end_time)
            except discord.NotFound:
                logging.info(f"[Poll] Poll message {poll_msg.id} was deleted; aborting poll loop.")
                return
            except discord.Forbidden:
                logging.warning(f"[Poll] Lost permission to edit poll message {poll_msg.id}; aborting.")
                return
        await _show_poll_results(poll_msg, author, question, options, results, total_votes)
    except asyncio.CancelledError:
        # Bot is shutting down; let the cancellation propagate.
        raise
    except Exception as e:
        logging.error(f"[Poll] Poll loop error for message {poll_msg.id}: {e}")


async def _update_poll(poll_msg: discord.Message, author: discord.abc.User, question: str, options: Tuple[str, ...], end_time: float) -> Tuple[discord.Message, Dict[int, int], int]:
    poll_msg = await poll_msg.channel.fetch_message(poll_msg.id)
    results: Dict[int, int] = {}
    total_votes = 0
    
    for idx, emoji in enumerate(POLL_EMOJIS[:len(options)]):
        for reaction in poll_msg.reactions:
            if str(reaction.emoji) == emoji:
                votes = reaction.count - 1  # subtract the bot's own reaction
                results[idx] = votes
                total_votes += votes
    
    embed = discord.Embed(
        title=f"📊 {question}",
        description="React with the matching emoji below to cast your vote!",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=f"{author.display_name} started a poll", icon_url=(author.avatar.url if author.avatar else author.default_avatar.url))
    
    for idx, option in enumerate(options):
        votes = results.get(idx, 0)
        pct = (votes / total_votes * 100) if total_votes > 0 else 0
        bar = _poll_bar(pct)
        embed.add_field(
            name=f"{POLL_EMOJIS[idx]} {option}",
            value=f"`{bar}` **{votes}** vote{'s' if votes != 1 else ''} ({pct:.1f}%)",
            inline=False,
        )
    
    remaining = max(0, int(end_time - time.time()))
    embed.set_footer(text=f"⏱️ {remaining}s remaining • {total_votes} total vote{'s' if total_votes != 1 else ''} • {len(options)} options")
    await poll_msg.edit(embed=embed)
    
    return poll_msg, results, total_votes


async def _show_poll_results(poll_msg: discord.Message, author: discord.abc.User, question: str, options: Tuple[str, ...], results: Dict[int, int], total_votes: int) -> None:
    """Render the final poll results with a winner highlight and tie detection."""
    # Determine the winner(s) — may be a tie.
    max_votes = max(results.values()) if results else 0
    if max_votes == 0:
        winner_idxs: List[int] = []
    else:
        winner_idxs = [idx for idx, v in results.items() if v == max_votes]
    is_tie = len(winner_idxs) > 1
    has_winner = len(winner_idxs) == 1

    # Gold for a single winner, orange for a tie, blurple if no votes at all.
    if has_winner:
        result_color = discord.Color.gold()
    elif is_tie:
        result_color = discord.Color.orange()
    else:
        result_color = discord.Color.blurple()

    if has_winner:
        title = f"🏆 Poll Results: {question}"
        desc = f"The votes are in! The winner is **{options[winner_idxs[0]]}** with **{max_votes}** vote{'s' if max_votes != 1 else ''}."
    elif is_tie:
        tied_names = ", ".join(f"**{options[i]}**" for i in winner_idxs)
        title = f"🤝 Poll Results: {question}"
        desc = f"It's a tie! {tied_names} each received **{max_votes}** vote{'s' if max_votes != 1 else ''}."
    elif total_votes == 0:
        title = f"📊 Poll Results: {question}"
        desc = "The poll ended with no votes cast. Better luck next time!"
    else:
        title = f"📊 Poll Results: {question}"
        desc = "The votes are in! Here are the final results."

    result_embed = discord.Embed(
        title=title,
        description=desc,
        color=result_color,
        timestamp=datetime.now(timezone.utc),
    )
    result_embed.set_author(name=f"{author.display_name}'s poll has ended", icon_url=(author.avatar.url if author.avatar else author.default_avatar.url))
    
    for idx, option in enumerate(options):
        votes = results.get(idx, 0)
        pct = (votes / total_votes * 100) if total_votes > 0 else 0
        bar = _poll_bar(pct)
        # Crown the winning option(s) and mark ties.
        if idx in winner_idxs and has_winner:
            prefix = "🏆 "
        elif idx in winner_idxs and is_tie:
            prefix = "🤝 "
        else:
            prefix = ""
        result_embed.add_field(
            name=f"{prefix}{POLL_EMOJIS[idx]} {option}",
            value=f"`{bar}` **{votes}** vote{'s' if votes != 1 else ''} ({pct:.1f}%)",
            inline=False,
        )
    result_embed.set_footer(text=f"✅ Poll ended • {total_votes} total vote{'s' if total_votes != 1 else ''} • {len(options)} options")
    
    await poll_msg.edit(embed=result_embed)
    await poll_msg.clear_reactions()

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    @bot.command(name="poll", description="Create a poll with multiple options")
    @commands.has_permissions(manage_messages=True)
    @app_commands.describe(
        duration="Poll duration in seconds (10-600)",
        question="The poll question",
        options="Poll options separated by spaces (e.g. 'Red Blue Green') or pipes (e.g. 'Red|Blue|Green')",
    )
    async def poll(ctx: commands.Context, duration: int, question: str, *, options: str) -> None:
        """Create a live-updating poll.

    The old `*options: str` variadic positional arg is replaced with a
    single keyword-only `options: str` that gets split inside the function.
    Accepts either space-separated or pipe-separated options.
    """
        # Parse options: split on | if present, otherwise split on spaces.
        if "|" in options:
            option_list = [o.strip() for o in options.split("|") if o.strip()]
        else:
            option_list = [o.strip() for o in options.split() if o.strip()]
        options_tuple: Tuple[str, ...] = tuple(option_list)

        if len(options_tuple) < config.limits.min_poll_options or len(options_tuple) > config.limits.max_poll_options:
            await ctx.send(
                embed=EmbedBuilder.error(
                    "Invalid Poll Options",
                    f"Please provide **{config.limits.min_poll_options}-{config.limits.max_poll_options}** options.\n"
                    f"You provided: **{len(options_tuple)}**.\n\n"
                    f"**Usage:** `!poll <duration> <question> <opt1> <opt2> ...`\n"
                    f"*(use `|` to separate options with spaces in them, e.g. `!poll 60 Best color Red|Blue Green`)*",
                )
            )
            return

        if duration < config.limits.min_poll_duration or duration > config.limits.max_poll_duration:
            await ctx.send(
                embed=EmbedBuilder.error(
                    "Invalid Duration",
                    f"Duration must be between **{config.limits.min_poll_duration}** and **{config.limits.max_poll_duration}** seconds.\n"
                    f"You provided: **{duration}s**.",
                )
            )
            return

        embed = discord.Embed(
            title=f"📊 {question}",
            description="React with the matching emoji below to cast your vote!",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=f"{ctx.author.display_name} started a poll", icon_url=(ctx.author.avatar.url if ctx.author.avatar else ctx.author.default_avatar.url))

        for idx, option in enumerate(options_tuple):
            embed.add_field(name=f"{POLL_EMOJIS[idx]} {option}", value="`░░░░░░░░░░░░░░░` 0 votes (0.0%)", inline=False)
        embed.set_footer(text=f"⏱️ Ends in {duration}s • {len(options_tuple)} options")

        poll_msg = await ctx.send(embed=embed)

        for idx in range(len(options_tuple)):
            await poll_msg.add_reaction(POLL_EMOJIS[idx])

        end_time = time.time() + duration

        # Run the live-update loop as a detached background task so this command
        # returns immediately. Previously the command blocked the invoking task
        # for the entire poll duration, which scaled badly with multiple polls.
        asyncio.create_task(_run_poll(poll_msg, ctx.author, question, options_tuple, end_time))
