# -*- coding: utf-8 -*-
"""Moderation — kicks/bans/mutes, warnings, blacklist, reports,
temp-mute + prune tasks."""

# stdlib + discord.py
import asyncio
import discord
import logging
import random
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import commands, tasks
from typing import Any, Dict, List, Optional, Tuple

from core import state  # shared mutable runtime state
from core.state import config, data_manager, warnings_data
from core.helpers import brand_text, log_event
from core.ows import ows_get
from utils.ui.embeds import EmbedBuilder



REPORT_TEMPLATES: List[str] = [
    "Ayyy what we all getttin' into?", "Yall tryin' to get on?", "Y'all know it's [GANG NAME] business. **[GANG NAME] ON TOP!**",
    "Ay, y'all wanna slide on the opps?",
]

REPORT_CATEGORIES: Dict[str, str] = {
    "1️⃣": "Rule Violation",
    "2️⃣": "Harassment",
    "3️⃣": "Scamming",
    "4️⃣": "Cheating",
    "5️⃣": "Other"
}


# --- DATA PERSISTENCE FUNCTIONS ---
def load_blacklist_data() -> None:
    try:
        state.blacklisted_keywords = data_manager.load_blacklist()
        logging.info(f"[Blacklist] Loaded {len(state.blacklisted_keywords)} blacklisted keywords from SQLite")
    except Exception as e:
        logging.error(f"[Blacklist] Error loading data: {e}")
        state.blacklisted_keywords = set()


def save_blacklist_data() -> None:
    try:
        data_manager.save_blacklist(state.blacklisted_keywords)
    except Exception as e:
        logging.error(f"[Blacklist] Error saving data: {e}")


# --- BLACKLIST HELPERS ---
def check_text_for_keywords(text: str) -> Tuple[bool, Optional[str]]:
    if not text:
        return (False, None)
    text_lower = text.lower()
    for keyword in state.blacklisted_keywords:
        if keyword.lower() in text_lower:
            return (True, keyword)
    return (False, None)


async def check_user_profile_for_blacklist(member: discord.Member) -> Tuple[bool, Optional[str], Optional[str]]:
    if not state.blacklisted_keywords:
        return (False, None, None)
    
    if isinstance(member, discord.Member) and member.activities:
        for activity in member.activities:
            if activity.type == discord.ActivityType.custom:
                parts = []
                if hasattr(activity, 'state') and activity.state:
                    parts.append(activity.state)
                if hasattr(activity, 'name') and activity.name:
                    parts.append(activity.name)
                if hasattr(activity, 'emoji') and activity.emoji and activity.emoji.name:
                    parts.append(activity.emoji.name)
                status_text = ' '.join(parts).strip()
                if status_text:
                    found, keyword = check_text_for_keywords(status_text)
                    if found:
                        return (True, keyword, "Custom Status")
    
    found, keyword = check_text_for_keywords(member.display_name)
    if found:
        return (True, keyword, "Display Name")
    
    found, keyword = check_text_for_keywords(member.name)
    if found:
        return (True, keyword, "Username")
    
    return (False, None, None)


async def auto_ban_if_blacklisted(member: discord.Member, source: str = "unknown") -> bool:
    if not ows_get("auto_ban_profile"):
        return False

    is_blacklisted, keyword, location = await check_user_profile_for_blacklist(member)
    if not is_blacklisted:
        return False

    if ows_get("blacklist_alert_only"):
        logging.info(f"[Blacklist] ALERT ONLY: {member} matched '{keyword}' in {location} (source: {source})")
        log_channel = state.bot.get_channel(config.channels.log)
        if log_channel:
            embed = discord.Embed(title="⚠️ Blacklist Alert (Review Mode)", color=discord.Color.orange())
            embed.add_field(name="User", value=f"{member.mention} ({member.name})", inline=True)
            embed.add_field(name="Matched Keyword", value=f"**{keyword}**", inline=True)
            embed.add_field(name="Location", value=location, inline=True)
            embed.add_field(name="Triggered By", value=source, inline=True)
            embed.timestamp = datetime.now(timezone.utc)
            await log_channel.send(embed=embed)
        return False

    try:
        await member.ban(reason=f"Auto-banned: Blacklisted keyword '{keyword}' found in {location}")
        logging.info(f"[Blacklist] Auto-banned {member} (ID: {member.id}) via {source} - Keyword '{keyword}' in {location}")
        
        log_channel = state.bot.get_channel(config.channels.log)
        if log_channel:
            embed = discord.Embed(title="Auto-Ban: Blacklisted Keyword Detected", color=discord.Color.red())
            embed.add_field(name="User", value=f"{member.mention} ({member.name})", inline=True)
            embed.add_field(name="User ID", value=str(member.id), inline=True)
            embed.add_field(name="Matched Keyword", value=f"**{keyword}**", inline=True)
            embed.add_field(name="Location", value=location, inline=True)
            embed.add_field(name="Triggered By", value=source, inline=True)
            embed.timestamp = datetime.now(timezone.utc)
            await log_channel.send(embed=embed)
        return True
    except discord.Forbidden:
        logging.warning(f"[Blacklist] Failed to ban {member} - No permission")
    except discord.HTTPException as e:
        logging.error(f"[Blacklist] Failed to ban {member} - HTTP Error: {e}")
    return False


async def scan_and_ban_blacklisted_members(guild: discord.Guild) -> Tuple[int, int, List[Dict[str, Any]]]:
    banned_count = 0
    failed_count = 0
    matches: List[Dict[str, Any]] = []
    
    for member in guild.members:
        if member.bot:
            continue
        is_blacklisted, keyword, location = await check_user_profile_for_blacklist(member)
        if is_blacklisted:
            matches.append({'user': member, 'keyword': keyword, 'location': location})
            try:
                await member.ban(reason=f"Auto-banned: Blacklisted keyword '{keyword}' found in {location}")
                banned_count += 1
                logging.info(f"[Blacklist] Banned {member} (ID: {member.id}) - Keyword '{keyword}' in {location}")
            except discord.Forbidden:
                failed_count += 1
                logging.warning(f"[Blacklist] Failed to ban {member} - No permission")
            except discord.HTTPException as e:
                failed_count += 1
                logging.error(f"[Blacklist] Failed to ban {member} - HTTP Error: {e}")
    
    return (banned_count, failed_count, matches)


@tasks.loop(minutes=config.timing.report_message_interval_minutes)
async def send_report_message() -> None:
    if not state.messages_enabled:
        return
    channel = state.bot.get_channel(config.channels.log)
    if channel:
        await channel.send(brand_text(random.choice(REPORT_TEMPLATES)))


@tasks.loop(hours=config.timing.auto_scan_interval_hours)
async def auto_blacklist_scan() -> None:
    if not state.blacklisted_keywords:
        return
    for guild in state.bot.guilds:
        log_channel = state.bot.get_channel(config.channels.auto_scan)
        status_msg = None
        if log_channel:
            status_msg = await log_channel.send("Running scheduled blacklist scan...")
        banned_count, failed_count, matches = await scan_and_ban_blacklisted_members(guild)
        if status_msg:
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass
        if banned_count > 0 or failed_count > 0:
            if log_channel:
                embed = discord.Embed(title="Scheduled Blacklist Scan Complete", color=discord.Color.red() if banned_count > 0 else discord.Color.orange())
                embed.add_field(name="Members Banned", value=f"**{banned_count}**", inline=True)
                embed.add_field(name="Failed to Ban", value=f"**{failed_count}**", inline=True)
                if matches:
                    match_text = ""
                    for match in matches[:5]:
                        match_text += f"- {match['user'].name} - `{match['keyword']}` in {match['location']}\n"
                    if len(matches) > 5:
                        match_text += f"... and {len(matches) - 5} more"
                    embed.add_field(name="Matches", value=match_text, inline=False)
                embed.set_footer(text=f"Next scan in {config.timing.auto_scan_interval_hours} hours")
                embed.timestamp = datetime.now(timezone.utc)
                result_msg = await log_channel.send(embed=embed)
                await asyncio.sleep(30)
                try:
                    await result_msg.delete()
                except discord.HTTPException:
                    pass


@auto_blacklist_scan.before_loop
async def before_auto_scan() -> None:
    await state.bot.wait_until_ready()


async def _schedule_unmute(mute_id: str, delay: int) -> None:
    """Wait `delay` seconds then unmute. Dies gracefully if the bot restarts
    (the persistent check_temp_mutes_task will pick it up)."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    await _unmute_user(mute_id, source="scheduled")


async def _unmute_user(mute_id: str, source: str = "scheduled") -> None:
    """Remove the mute role for a given mute_id and deactivate the record.
    Idempotent: safe to call multiple times (e.g. once by the scheduled task
    and again by the background loop)."""
    mute = data_manager.get_temp_mute(mute_id)
    if not mute or not mute.get('is_active'):
        return

    # Deactivate first so a concurrent call is a no-op.
    data_manager.deactivate_temp_mute(mute_id)

    guild = state.bot.get_guild(mute['guild_id'])
    if not guild:
        logging.warning(f"[TempMute] Guild {mute['guild_id']} not found for unmute (mute_id={mute_id})")
        return

    role = guild.get_role(mute['role_id'])
    member = guild.get_member(mute['user_id'])

    if member and role:
        try:
            await member.remove_roles(role, reason=f"Temp-mute expired (mute_id={mute_id})")
        except discord.Forbidden:
            logging.warning(f"[TempMute] No permission to remove mute role from {member}")
        except discord.HTTPException as e:
            logging.error(f"[TempMute] Failed to remove mute role from {member}: {e}")
    elif member is None:
        # Member left the guild; the role can't be removed now, but the record
        # is deactivated so they won't be re-muted on rejoin handling. If they
        # rejoin, they simply won't have the role.
        logging.info(f"[TempMute] Member {mute['user_id']} not in guild; role removal skipped (mute_id={mute_id})")

    logging.info(f"[TempMute] Unmuted user {mute['user_id']} via {source} (mute_id={mute_id})")

    log_channel = state.bot.get_channel(config.channels.log)
    if log_channel:
        try:
            embed = discord.Embed(
                title="🔇 Temp-Mute Expired",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="User", value=f"<@{mute['user_id']}> (`{mute['user_id']}`)", inline=True)
            embed.add_field(name="Mute ID", value=f"`{mute_id}`", inline=True)
            embed.add_field(name="Triggered By", value=source, inline=True)
            embed.add_field(name="Original Reason", value=mute.get('reason') or "No reason provided", inline=False)
            await log_channel.send(embed=embed)
        except Exception as e:
            logging.warning(f"[TempMute] Could not send unmute log: {e}")


async def restore_temp_mutes() -> None:
    """Called on startup. Re-schedules prompt unmutes for any active mutes
    and immediately unmutes any that already expired while the bot was down."""
    active = data_manager.load_active_temp_mutes()
    if not active:
        return
    now = datetime.now(timezone.utc)
    scheduled = 0
    expired_now = 0
    for mute in active:
        try:
            unmute_at = datetime.fromisoformat(mute['unmute_at'].replace('Z', '+00:00'))
        except Exception:
            logging.warning(f"[TempMute] Could not parse unmute_at for mute_id={mute.get('mute_id')}; deactivating.")
            data_manager.deactivate_temp_mute(mute.get('mute_id'))
            continue

        remaining = (unmute_at - now).total_seconds()
        if remaining <= 0:
            await _unmute_user(mute['mute_id'], source="startup-expired")
            expired_now += 1
        else:
            asyncio.create_task(_schedule_unmute(mute['mute_id'], int(remaining) + 1))
            scheduled += 1

    logging.info(f"[TempMute] Restored {scheduled} active mute(s); unmuted {expired_now} expired mute(s) on startup.")


@tasks.loop(minutes=1)
async def check_temp_mutes_task() -> None:
    """Persistent safety net: unmute any active temp-mute whose time is up.
    Catches mutes whose in-memory scheduled task was lost (e.g. after a restart)
    or that were created before the scheduling helper existed."""
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        expired = data_manager.load_expired_temp_mutes(now_iso)
        for mute in expired:
            await _unmute_user(mute['mute_id'], source="background-loop")
    except Exception as e:
        logging.error(f"[TempMute] Error in check_temp_mutes_task: {e}")


@check_temp_mutes_task.before_loop
async def before_check_temp_mutes() -> None:
    await state.bot.wait_until_ready()


# --- Message Log cache pruning (keeps the SQLite cache bounded) ---
@tasks.loop(minutes=30)
async def prune_message_cache_task() -> None:
    """Periodically trim the message-log cache so it never grows unbounded."""
    try:
        if data_manager is None or data_manager._connection is None:
            return
        removed = data_manager.prune_message_cache(keep_recent=5000)
        if removed:
            logging.info(f"[MsgLog] Pruned {removed} stale cached message(s)")
    except Exception as exc:
        logging.debug(f"[MsgLog] prune task error: {exc}")


@prune_message_cache_task.before_loop
async def before_prune_message_cache() -> None:
    await state.bot.wait_until_ready()

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    # --- MODERATION COMMANDS ---
    @bot.command()
    @commands.has_permissions(kick_members=True)
    async def kick(ctx: commands.Context, members: commands.Greedy[discord.Member], *, reason: str = "No reason provided") -> None:
        """Kicks one or multiple members. Usage: !kick @user1 @user2 [reason]"""
        if not members:
            await ctx.send("Usage: `!kick @user1 @user2 [reason]` (or `!kick @user1, @user2`)")
            return

        kicked_list = []
        failed_list = []

        for member in members:
            if member.top_role >= ctx.guild.me.top_role or member.id == ctx.guild.owner_id:
                failed_list.append(f"{member.mention} (Hierarchy/Owner)")
                continue
            try:
                await member.kick(reason=f"Kicked by {ctx.author}: {reason}")
                kicked_list.append(member.mention)
            except discord.Forbidden:
                failed_list.append(f"{member.mention} (Missing Perms)")
            except discord.HTTPException:
                failed_list.append(f"{member.mention} (API Error)")

        embed = EmbedBuilder.success("Mass Kick Complete", "")
        if kicked_list:
            embed.add_field(name="✅ Successfully Kicked", value="\n".join(kicked_list), inline=False)
        if failed_list:
            embed.add_field(name="❌ Failed to Kick", value="\n".join(failed_list), inline=False)
    
        embed.description = f"**Reason:** {reason}"
        await ctx.send(embed=embed)
        logging.info(f'User(s) {kicked_list} were kicked by {ctx.author} for: {reason}')


    @bot.command()
    @commands.has_permissions(ban_members=True)
    async def ban(ctx: commands.Context, members: commands.Greedy[discord.Member], *, reason: str = "No reason provided") -> None:
        """Bans one or multiple members. Usage: !ban @user1 @user2 [reason]"""
        if not members:
            await ctx.send("Usage: `!ban @user1 @user2 [reason]` (or `!ban @user1, @user2`)")
            return

        banned_list = []
        failed_list = []

        for member in members:
            if member.top_role >= ctx.guild.me.top_role or member.id == ctx.guild.owner_id:
                failed_list.append(f"{member.mention} (Hierarchy/Owner)")
                continue
            try:
                await member.ban(reason=f"Banned by {ctx.author}: {reason}", delete_message_days=0)
                banned_list.append(member.mention)
            except discord.Forbidden:
                failed_list.append(f"{member.mention} (Missing Perms)")
            except discord.HTTPException:
                failed_list.append(f"{member.mention} (API Error)")

        embed = EmbedBuilder.success("Mass Ban Complete", "")
        if banned_list:
            embed.add_field(name="✅ Successfully Banned", value="\n".join(banned_list), inline=False)
        if failed_list:
            embed.add_field(name="❌ Failed to Ban", value="\n".join(failed_list), inline=False)
    
        embed.description = f"**Reason:** {reason}"
        await ctx.send(embed=embed)
        logging.info(f'User(s) {banned_list} were banned by {ctx.author} for: {reason}')

    @bot.command()
    @commands.has_permissions(ban_members=True)
    async def banid(ctx: commands.Context, user_id: int, *, reason: str = "No reason provided") -> None:
        try:
            user = await bot.fetch_user(user_id)
            await ctx.guild.ban(user, reason=reason)
            await ctx.send(embed=EmbedBuilder.success("User Banned", f"**{user.name}** (ID: {user_id}) has been banned.\n**Reason:** {reason}"))
            logging.info(f'User with ID {user_id} was banned by {ctx.author} for: {reason}')
        except discord.NotFound:
            await ctx.send(f'User with ID {user_id} not found.')
        except discord.Forbidden:
            await ctx.send("I don't have permission to ban that user.")
        except discord.HTTPException:
            await ctx.send("Failed to ban the user. Please try again.")


    # --- BLACKLIST COMMANDS ---
    @bot.command(name="blacklist", description="Add a keyword to the blacklist")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(keyword="The keyword to blacklist")
    async def blacklist_cmd(ctx: commands.Context, *, keyword: str) -> None:
    
        keyword = keyword.strip()
        if not keyword:
            await ctx.send("Please provide a keyword to blacklist.")
            return
    
        if len(keyword) < config.limits.min_blacklist_keyword_length:
            await ctx.send(f"Keyword must be at least {config.limits.min_blacklist_keyword_length} characters long.")
            return
    
        for existing in state.blacklisted_keywords:
            if existing.lower() == keyword.lower():
                await ctx.send(f"Keyword `{keyword}` is already blacklisted.")
                return
    
        state.blacklisted_keywords.add(keyword)
        save_blacklist_data()
    
        embed = EmbedBuilder.warning(
            "Keyword Blacklisted",
            f"Added `{keyword}` to blacklist.\nTotal keywords: {len(state.blacklisted_keywords)}\n\n"
            f"Any user with this keyword in their profile will be auto-banned."
        )
    
        await ctx.send(embed=embed)
        logging.info(f"[Blacklist] Keyword '{keyword}' added by {ctx.author}")


    @bot.command(name="unblacklist", description="Remove a keyword from the blacklist")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(keyword="The keyword to remove from the blacklist")
    async def unblacklist_cmd(ctx: commands.Context, *, keyword: str) -> None:
    
        keyword = keyword.strip()
        found_keyword = next((k for k in state.blacklisted_keywords if k.lower() == keyword.lower()), None)
    
        if not found_keyword:
            await ctx.send(f"Keyword `{keyword}` is not in the blacklist.")
            return
    
        state.blacklisted_keywords.discard(found_keyword)
        save_blacklist_data()
    
        await ctx.send(embed=EmbedBuilder.success("Keyword Removed", f"Removed `{found_keyword}` from blacklist."))
        logging.info(f"[Blacklist] Keyword '{found_keyword}' removed by {ctx.author}")


    @bot.command(name="blacklistscan", description="Scan all members for blacklisted keywords")
    @commands.has_permissions(administrator=True)
    async def blacklistscan_cmd(ctx: commands.Context) -> None:
        if not state.blacklisted_keywords:
            await ctx.send("No keywords are currently blacklisted. Use `!blacklist <keyword>` to add some.")
            return
    
        status_msg = await ctx.send(f"Scanning **{ctx.guild.member_count}** members for **{len(state.blacklisted_keywords)}** blacklisted keyword(s)...")
    
        banned_count, failed_count, matches = await scan_and_ban_blacklisted_members(ctx.guild)
        await status_msg.delete()
    
        embed = EmbedBuilder.warning(
            "Blacklist Scan Complete",
            f"**Members Scanned:** {ctx.guild.member_count}\n"
            f"**Members Banned:** {banned_count}\n"
            f"**Failed to Ban:** {failed_count}"
        )
    
        if matches:
            match_text = ""
            for match in matches[:5]:
                match_text += f"- {match['user'].name} - \"{match['keyword']}\" in {match['location']}\n"
            if len(matches) > 5:
                match_text += f"... and {len(matches) - 5} more"
            embed.add_field(name="Matches Found", value=match_text, inline=False)
    
        await ctx.send(embed=embed)
        logging.info(f"[Blacklist] Scan complete by {ctx.author}. Banned: {banned_count}, Failed: {failed_count}")


    @bot.command(name="blacklistlist", description="Display all blacklisted keywords")
    @commands.has_permissions(administrator=True)
    async def blacklistlist_cmd(ctx: commands.Context) -> None:
        if not state.blacklisted_keywords:
            await ctx.send("**No keywords are currently blacklisted.**")
            return
    
        keywords_list = list(state.blacklisted_keywords)
        embed = discord.Embed(title="Blacklisted Keywords", color=discord.Color.red())
    
        chunks = [keywords_list[i:i+20] for i in range(0, len(keywords_list), 20)]
        for i, chunk in enumerate(chunks[:5]):
            field_name = "Keywords" if i == 0 else "Keywords (continued)"
            embed.add_field(name=field_name, value="\n".join(f"- `{kw}`" for kw in chunk), inline=False)
    
        if len(chunks) > 5:
            embed.add_field(name="...", value=f"And {len(keywords_list) - 100} more keywords", inline=False)
    
        embed.set_footer(text=f"Total: {len(state.blacklisted_keywords)} keyword(s)")
        await ctx.send(embed=embed)


    @bot.command(name="checkprofile", description="Check a user's profile for blacklisted keywords")
    @app_commands.describe(member="The member to check")
    async def checkprofile_cmd(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        if member is None:
            member = ctx.author
    
        is_blacklisted, keyword, location = await check_user_profile_for_blacklist(member)
    
        embed = discord.Embed(title=f"Profile Check: {member.display_name}", color=discord.Color.red() if is_blacklisted else discord.Color.green())
        embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
    
        if is_blacklisted:
            embed.add_field(name="Status", value="**BLACKLISTED**", inline=True)
            embed.add_field(name="Matched Keyword", value=f"**{keyword}**", inline=True)
            embed.add_field(name="Location", value=location, inline=True)
        else:
            embed.add_field(name="Status", value="**Clean**", inline=True)
            embed.add_field(name="Keywords Checked", value=str(len(state.blacklisted_keywords)), inline=True)
    
        await ctx.send(embed=embed)


    @bot.command()
    @commands.has_permissions(manage_roles=True)
    async def mute(ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        if config.roles.staff in [role.id for role in member.roles]:
            await ctx.send(f'{member.mention} cannot be muted because they have the Staff role.')
            return
    
        mute_role = discord.utils.get(ctx.guild.roles, name='Muted')
        if not mute_role:
            message = await ctx.send('Mute role not found. Please create a role named "Muted".')
            await asyncio.sleep(2)
            await message.delete()
            return
    
        try:
            await member.add_roles(mute_role)
            response = f'Muted {member.mention} for: {reason}' if reason else f'Muted {member.mention} without a specified reason.'
            await ctx.send(embed=EmbedBuilder.warning("Member Muted", response))
            logging.info(f'User {member} was muted by {ctx.author} for: {reason}')
        except discord.Forbidden:
            await ctx.send("I do not have permission to mute that member.")
        except discord.HTTPException:
            await ctx.send("Failed to mute the member. Please try again.")


    # NOTE: The `unmute` command is defined further below alongside the temp-mute
    # system. It removes the Muted role AND deactivates any active temp-mute
    # records in the database (so a manual unmute cancels a pending auto-unmute).


    @bot.command()
    @commands.has_permissions(manage_channels=True)
    async def lock(ctx: commands.Context) -> None:
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
        await ctx.send(embed=EmbedBuilder.warning("Channel Locked", "This channel is now locked."))
        logging.info(f'Channel {ctx.channel} was locked by {ctx.author}')


    @bot.command()
    @commands.has_permissions(manage_channels=True)
    async def unlock(ctx: commands.Context) -> None:
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
        await ctx.send(embed=EmbedBuilder.success("Channel Unlocked", "This channel is now unlocked."))
        logging.info(f'Channel {ctx.channel} was unlocked by {ctx.author}')


    @bot.command()
    @commands.has_permissions(manage_channels=True)
    async def slowmode(ctx: commands.Context, seconds: int) -> None:
        await ctx.channel.edit(slowmode_delay=seconds)
        await ctx.send(embed=EmbedBuilder.info("Slowmode Set", f"Slowmode set to {seconds} seconds."))
        logging.info(f'Slowmode in {ctx.channel} was set to {seconds}s by {ctx.author}')


    @bot.command()
    @commands.has_permissions(manage_roles=True)
    async def addrole(ctx: commands.Context, member: discord.Member, *, role_name: str) -> None:
        role = discord.utils.get(ctx.guild.roles, name=role_name)
        if role is None:
            await ctx.send(f'Role "{role_name}" not found.')
            return
        if role in member.roles:
            await ctx.send(f"{member.mention} already has the {role_name} role.")
            return
        await member.add_roles(role)
        await ctx.send(embed=EmbedBuilder.success("Role Added", f"Added **{role_name}** to {member.mention}."))
        log_event("Role Added", ctx.author, f"Added {role_name} to {member}")


    @bot.command()
    @commands.has_permissions(manage_roles=True)
    async def roleall(ctx: commands.Context, role: discord.Role) -> None:
        if role is None:
            await ctx.send("Please mention a valid role.")
            return
    
        members_assigned = 0
        failed_members = 0
    
        for member in ctx.guild.members:
            if member.bot:
                continue
            if role not in member.roles:
                try:
                    await member.add_roles(role)
                    members_assigned += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed_members += 1
    
        await ctx.send(embed=EmbedBuilder.success("Role Assigned", f"Assigned {role.mention} to **{members_assigned}** members.{f' Failed: {failed_members}' if failed_members else ''}"))
        log_event("Role All Assigned", ctx.author, f"Assigned {role.name} to {members_assigned} members")


    @bot.command()
    @commands.has_permissions(manage_roles=True)
    async def removerole(ctx: commands.Context, member: discord.Member, role: discord.Role) -> None:
        try:
            await member.remove_roles(role)
            await ctx.send(embed=EmbedBuilder.success("Role Removed", f"Removed **{role.name}** from {member.mention}."))
            logging.info(f'Role {role.name} was removed from {member} by {ctx.author}')
        except discord.Forbidden:
            await ctx.send("I do not have permission to remove that role.")
        except discord.HTTPException:
            await ctx.send("Failed to remove the role. Please try again.")


    @bot.command()
    @commands.has_permissions(ban_members=True)
    async def softban(ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        await member.ban(reason=reason)
        await ctx.guild.unban(member)
        await ctx.send(embed=EmbedBuilder.warning("Member Softbanned", f"{member.mention} has been softbanned.\n**Reason:** {reason or 'No reason provided'}"))
        logging.info(f'User {member} was softbanned by {ctx.author} for: {reason}')


    @bot.command()
    @commands.has_permissions(manage_roles=True)
    async def tempmute(ctx: commands.Context, member: discord.Member, duration: int, *, reason: Optional[str] = None) -> None:
        """
    Temporarily mute a member for `duration` seconds.

    The mute is persisted in the database (temp_mutes table) and a background
    task performs the unmute, so the mute survives a bot restart instead of
    relying on a blocking asyncio.sleep() that dies when the process dies.
    """
        if duration <= 0:
            await ctx.send("Duration must be a positive number of seconds.")
            return

        mute_role = discord.utils.get(ctx.guild.roles, name='Muted')
        if not mute_role:
            await ctx.send("Mute role not found. Please create a role named 'Muted'.")
            return

        now = datetime.now(timezone.utc)
        unmute_at = now + timedelta(seconds=duration)
        mute_id = str(_uuid.uuid4())[:8]
        mute_record = {
            'mute_id': mute_id,
            'guild_id': ctx.guild.id,
            'user_id': member.id,
            'role_id': mute_role.id,
            'moderator_id': ctx.author.id,
            'reason': reason,
            'muted_at': now.isoformat(),
            'unmute_at': unmute_at.isoformat(),
            'is_active': True,
        }

        try:
            await member.add_roles(mute_role, reason=reason or "Temp-mute")
        except discord.Forbidden:
            await ctx.send("I don't have permission to manage roles for that member.")
            return
        except discord.HTTPException:
            await ctx.send("Failed to apply the mute role. Please try again.")
            return

        # Persist AFTER the role is applied so we only track mutes that actually took effect.
        data_manager.save_temp_mute(mute_record)
        logging.info(f'[TempMute] {member} (ID: {member.id}) muted by {ctx.author} for {duration}s (mute_id={mute_id})')

        await ctx.send(embed=EmbedBuilder.warning(
            "Member Temp-Muted",
            f"{member.mention} muted for **{duration}** seconds.\n"
            f"**Reason:** {reason or 'No reason provided'}\n"
            f"**Unmute at:** {unmute_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"**Mute ID:** `{mute_id}`"
        ))

        # Schedule a prompt unmute in-memory. The DB-backed background task
        # (check_temp_mutes_task) is the persistent safety net that also handles
        # mutes that were in progress when the bot restarted.
        asyncio.create_task(_schedule_unmute(mute_id, duration))


    @bot.command()
    @commands.has_permissions(manage_roles=True)
    async def unmute(ctx: commands.Context, member: discord.Member) -> None:
        """Manually unmute a member early, deactivating any active temp-mute."""
        mute_role = discord.utils.get(ctx.guild.roles, name='Muted')
        if not mute_role:
            await ctx.send("Mute role not found.")
            return

        # Deactivate any active DB records for this user in this guild.
        removed = 0
        for mute in data_manager.load_active_temp_mutes():
            if mute.get('guild_id') == ctx.guild.id and mute.get('user_id') == member.id:
                data_manager.deactivate_temp_mute(mute['mute_id'])
                removed += 1

        try:
            if mute_role in member.roles:
                await member.remove_roles(mute_role, reason=f"Manually unmuted by {ctx.author}")
            await ctx.send(embed=EmbedBuilder.success(
                "Member Unmuted",
                f"{member.mention} has been unmuted.\nDeactivated {removed} active temp-mute record(s)."
            ))
            logging.info(f"[TempMute] {member} manually unmuted by {ctx.author}; {removed} record(s) deactivated.")
        except discord.Forbidden:
            await ctx.send("I don't have permission to manage roles for that member.")
        except discord.HTTPException:
            await ctx.send("Failed to remove the mute role. Please try again.")


    # --- WARNING COMMANDS (V2 Enhancement) ---
    @bot.command(name="warn", description="Warn a member")
    @commands.has_permissions(manage_roles=True)
    @app_commands.describe(member="Member to warn", reason="Reason for warning")
    async def warn_cmd(ctx: commands.Context, member: discord.Member, *, reason: str) -> None:
        if not config.enable_warnings:
            await ctx.send("Warning system is disabled.")
            return
    
        import uuid
        warning_id = str(uuid.uuid4())[:8]
    
        warning = {
            'warning_id': warning_id,
            'user_id': member.id,
            'guild_id': ctx.guild.id,
            'moderator_id': ctx.author.id,
            'reason': reason,
            'points': 1,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'is_active': True
        }
    
        if ctx.guild.id not in warnings_data:
            warnings_data[ctx.guild.id] = []
        warnings_data[ctx.guild.id].append(warning)
    
        total_points = sum(1 for w in warnings_data[ctx.guild.id] if w['user_id'] == member.id and w['is_active'])
    
        embed = EmbedBuilder.warning(
            "Member Warned",
            f"{member.mention} has been warned.\n"
            f"**Reason:** {reason}\n"
            f"**Warning ID:** {warning_id}\n"
            f"**Total Points:** {total_points}/{config.limits.max_warnings_before_ban}"
        )
    
        await ctx.send(embed=embed)
    
        if total_points >= config.limits.max_warnings_before_ban and ows_get("warnings_auto_ban"):
            try:
                await member.ban(reason=f"Exceeded warning limit ({total_points} points)")
                await ctx.send(embed=EmbedBuilder.error("Auto-Ban", f"{member.mention} has been auto-banned for exceeding warning limit."))
            except discord.Forbidden:
                logging.warning(f"[Warnings] No permission to auto-ban {member}")
            except discord.HTTPException as exc:
                logging.error(f"[Warnings] HTTP error auto-banning {member}: {exc}")
    
        logging.info(f"[Warnings] {member} warned by {ctx.author}: {reason}")


    @bot.command(name="warnings", description="View warnings for a member")
    @app_commands.describe(member="Member to check")
    async def warnings_cmd(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        member = member or ctx.author
    
        guild_warnings = warnings_data.get(ctx.guild.id, [])
        user_warnings = [w for w in guild_warnings if w['user_id'] == member.id and w['is_active']]
    
        if not user_warnings:
            await ctx.send(embed=EmbedBuilder.info("Warnings", f"{member.mention} has no active warnings."))
            return
    
        embed = EmbedBuilder.warning(f"Warnings for {member.display_name}", f"Total Active: {len(user_warnings)}")
    
        for w in user_warnings[:5]:
            created = datetime.fromisoformat(w['created_at']).strftime('%Y-%m-%d')
            embed.add_field(
                name=f"Warning {w['warning_id']}",
                value=f"**Reason:** {w['reason']}\n**By:** <@{w['moderator_id']}>\n**Date:** {created}",
                inline=False
            )
    
        await ctx.send(embed=embed)


    @bot.command(name="clearwarnings", description="Clear warnings for a member")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(member="Member to clear warnings for")
    async def clearwarnings_cmd(ctx: commands.Context, member: discord.Member) -> None:
        global warnings_data
    
        if ctx.guild.id not in warnings_data:
            await ctx.send(f"{member.mention} has no warnings.")
            return
    
        count = 0
        for w in warnings_data[ctx.guild.id]:
            if w['user_id'] == member.id and w['is_active']:
                w['is_active'] = False
                count += 1
    
        await ctx.send(embed=EmbedBuilder.success("Warnings Cleared", f"Cleared {count} warning(s) for {member.mention}."))
        logging.info(f"[Warnings] {ctx.author} cleared {count} warnings for {member}")


    # --- REPORT SYSTEM ---
    @bot.command()
    @commands.cooldown(1, 300, commands.BucketType.user)
    async def report(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        if not member:
            await ctx.send("Please mention the user you're reporting.")
            return
    
        if member == ctx.author:
            await ctx.send("You can't report yourself.")
            return
    
        if member.bot:
            await ctx.send("You can't report bots.")
            return
    
        embed = discord.Embed(title="Report System", description="React with the category number:", color=discord.Color.red())
    
        for emoji, cat in REPORT_CATEGORIES.items():
            embed.add_field(name=emoji, value=cat, inline=False)
    
        msg = await ctx.author.send(embed=embed)
        for emoji in REPORT_CATEGORIES.keys():
            await msg.add_reaction(emoji)
    
        try:
            reaction, _ = await bot.wait_for('reaction_add', check=lambda r, u: u == ctx.author and r.message.id == msg.id and str(r.emoji) in REPORT_CATEGORIES, timeout=60)
            category = REPORT_CATEGORIES[str(reaction.emoji)]
        except asyncio.TimeoutError:
            await ctx.author.send("Report timed out.")
            return
    
        await ctx.author.send(f"Please describe the {category} issue:")
    
        try:
            reason_msg = await bot.wait_for('message', check=lambda m: m.author == ctx.author and m.channel.type == discord.ChannelType.private, timeout=config.timing.report_timeout_seconds)
        
            if reason_msg.content.lower() == 'cancel':
                await ctx.author.send("Report cancelled.")
                return
        
            reason = reason_msg.content
        except asyncio.TimeoutError:
            await ctx.author.send("Report timed out.")
            return
    
        report_embed = discord.Embed(title=f"New {category} Report", color=discord.Color.red())
        report_embed.add_field(name="Reported User", value=f"{member.mention} (ID: {member.id})", inline=False)
        report_embed.add_field(name="Reporter", value=f"{ctx.author.mention} (ID: {ctx.author.id})", inline=False)
        report_embed.add_field(name="Reason", value=reason, inline=False)
        report_embed.timestamp = datetime.now(timezone.utc)
    
        reports_channel = bot.get_channel(config.channels.reports)
        if reports_channel:
            await reports_channel.send(embed=report_embed)
    
        await ctx.author.send("Your report has been submitted.")
        logging.info(f"New {category} report: {member} reported by {ctx.author}")


    # --- MESSAGE TOGGLE ---
    @bot.command()
    async def messageson(ctx: commands.Context) -> None:
    
        if state.messages_enabled:
            await ctx.send("Message reporting is already enabled.")
            return
    
        state.messages_enabled = True
        if not send_report_message.is_running():
            send_report_message.start()
        await ctx.send(embed=EmbedBuilder.success("Messages Enabled", "Periodic report reminder messages have been enabled."))


    @bot.command()
    async def messagesoff(ctx: commands.Context) -> None:
    
        if not state.messages_enabled:
            await ctx.send("Message reporting is already disabled.")
            return
    
        state.messages_enabled = False
        send_report_message.stop()
        await ctx.send(embed=EmbedBuilder.info("Messages Disabled", "Periodic report reminder messages have been disabled."))
