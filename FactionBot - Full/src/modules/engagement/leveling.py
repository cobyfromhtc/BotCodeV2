# -*- coding: utf-8 -*-
"""Leveling — XP processing, level/leaderboard commands + persistence."""

# stdlib + discord.py
import discord
import logging
import random
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands
from typing import Optional

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from core.helpers import compute_level_from_xp
from utils.ui.embeds import EmbedBuilder



def save_levels_data() -> None:
    try:
        data_manager.save_all_levels(state.levels_data)
    except Exception as e:
        logging.error(f"[Levels] Error saving data: {e}")


def load_levels_data() -> None:
    try:
        state.levels_data = data_manager.load_all_levels()
        logging.info(f"[Levels] Loaded {len(state.levels_data)} user levels from SQLite")
    except Exception as e:
        logging.error(f"[Levels] Error loading data: {e}")


# --- LEVELING SYSTEM (V2 Enhancement) ---
async def process_leveling(message: discord.Message) -> None:
    if not config.enable_leveling or not message.guild:
        return
    
    user_id = message.author.id
    guild_id = message.guild.id
    key = (user_id, guild_id)
    
    # Check cooldown
    level_data = state.levels_data.get(key, {'xp': 0, 'level': 0, 'total_messages': 0, 'last_xp_gain': None})
    last_gain = level_data.get('last_xp_gain')
    if last_gain:
        try:
            last_time = datetime.fromisoformat(last_gain)
            if (datetime.now(timezone.utc) - last_time).total_seconds() < 60:
                return
        except:
            pass
    
    # Add XP
    xp_gain = random.randint(5, 15)
    level_data['xp'] = level_data.get('xp', 0) + xp_gain
    level_data['total_messages'] = level_data.get('total_messages', 0) + 1
    level_data['last_xp_gain'] = datetime.now(timezone.utc).isoformat()
    
    # Check for level up using the centralized curve helper.
    old_level = level_data.get('level', 0)
    new_level = compute_level_from_xp(level_data['xp'])

    level_data['level'] = new_level
    state.levels_data[key] = level_data
    
    if new_level > old_level:
        embed = EmbedBuilder.success(
            "🎉 Level Up!",
            f"{message.author.mention} has reached **Level {new_level}**!"
        )
        await message.channel.send(embed=embed, delete_after=10)
    
    # OPTIMIZATION: Save ONLY this user to the database every 10 messages.
    # This prevents the bot from lagging by writing thousands of users at once.
    if level_data['total_messages'] % 10 == 0:
        data_manager.save_level(
            user_id, 
            guild_id, 
            level_data['xp'], 
            level_data['level'], 
            level_data['total_messages'], 
            datetime.fromisoformat(level_data['last_xp_gain']) if level_data.get('last_xp_gain') else None
        )

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    # --- LEVEL COMMANDS (V2 Enhancement) ---
    @bot.command(name="level", description="View your level")
    @app_commands.describe(member="Member to check")
    async def level_cmd(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        if not config.enable_leveling:
            await ctx.send("Leveling system is disabled.")
            return
    
        member = member or ctx.author
        key = (member.id, ctx.guild.id)
        level_data = state.levels_data.get(key, {'xp': 0, 'level': 0, 'total_messages': 0})
    
        embed = EmbedBuilder.level(member, level_data)
        await ctx.send(embed=embed)


    @bot.command(name="leaderboard", description="View the server leaderboard")
    async def leaderboard_cmd(ctx: commands.Context) -> None:
        if not config.enable_leveling:
            await ctx.send("Leveling system is disabled.")
            return
    
        # Get top 10 users in this guild
        guild_levels = [(uid, data) for (uid, gid), data in state.levels_data.items() if gid == ctx.guild.id]
        guild_levels.sort(key=lambda x: x[1].get('xp', 0), reverse=True)
    
        if not guild_levels:
            await ctx.send("No data yet. Start chatting to earn XP!")
            return
    
        embed = EmbedBuilder.info("🏆 Leaderboard", "")
    
        medals = ["🥇", "🥈", "🥉"]
    
        for idx, (user_id, data) in enumerate(guild_levels[:10]):
            medal = medals[idx] if idx < 3 else f"#{idx + 1}"
            user = ctx.guild.get_member(user_id)
            name = user.display_name if user else f"User {user_id}"
        
            embed.add_field(
                name=f"{medal} {name}",
                value=f"Level {data.get('level', 0)} • {data.get('xp', 0):,} XP",
                inline=False
            )
    
        await ctx.send(embed=embed)
