# -*- coding: utf-8 -*-
"""Invites — InviteManager, tracking views, invite check task + commands."""

# stdlib + discord.py
import asyncio
import discord
import logging
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View
from typing import Any, Dict, List, Optional

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from utils.ui.embeds import EmbedBuilder



INVITE_CONFIGS: List[Dict[str, Any]] = [
    {"name": "10 Uses", "max_uses": 10, "max_age": 0},
    {"name": "25 Uses", "max_uses": 25, "max_age": 0},
    {"name": "50 Uses", "max_uses": 50, "max_age": 0},
]


# --- INVITE TRACKING VIEW (persistent, attached to the tracking message) ---
class InviteTrackingView(View):
    """Persistent view attached to the invite-tracking message.

    Carries a single 'View Previous Invites' button that opens a paginated
    history modal-ish view. Persisted via `bot.add_view()` on startup and
    re-attached on every `update_invite_message` call so it survives restarts.
    """

    def __init__(self, guild_id: Optional[int]):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="View Previous Invites", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="invite_view_history")
    async def view_history_button(self, interaction: discord.Interaction, button: Button) -> None:
        # Anyone with manage_guild can browse the history. Keeps it staff-only
        # so random members can't scrape old invite codes.
        try:
            can_view = interaction.user.guild_permissions.manage_guild
        except Exception:
            can_view = False
        if not can_view:
            await interaction.response.send_message("You need the Manage Server permission to view invite history.", ephemeral=True)
            return

        groups = data_manager.load_invite_history_grouped(guild_id=self.guild_id, limit=500)
        if not groups:
            await interaction.response.send_message("No previous invite batches have been archived yet.", ephemeral=True)
            return

        view = InviteHistoryView(groups, self.guild_id)
        embed = view.build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# --- INVITE HISTORY VIEW (paginated, ephemeral) ---
class InviteHistoryView(View):
    """Paginated view of archived invite batches.

    One page = one archived batch (all invites that were saved at the same
    archived_at timestamp). Previous/Next walk the batch list; Close deletes
    the ephemeral message. Bounded to ~25 batches max for safety.
    """

    MAX_PAGES = 25

    def __init__(self, groups: List[Dict[str, Any]], guild_id: Optional[int]):
        super().__init__(timeout=120)
        # Cap to MAX_PAGES so an attacker can't OOM the bot by spamming
        # regenerateinvites thousands of times.
        self.groups = groups[: self.MAX_PAGES]
        self.guild_id = guild_id
        self.page = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= len(self.groups) - 1

    @property
    def current_group(self) -> Dict[str, Any]:
        if not self.groups:
            return {'archived_at': 'unknown', 'archived_by': None, 'invites': []}
        return self.groups[self.page]

    def build_embed(self) -> discord.Embed:
        group = self.current_group
        invites: List[Dict[str, Any]] = group.get('invites', [])

        # Parse the archived_at timestamp into something readable.
        archived_str = group.get('archived_at') or 'unknown'
        try:
            dt = datetime.fromisoformat(archived_str.replace('Z', '+00:00'))
            archived_display = dt.strftime('%Y-%m-%d %H:%M:%S UTC')
        except Exception:
            archived_display = archived_str

        archived_by = group.get('archived_by')
        by_line = f"<@{archived_by}>" if archived_by else "Unknown"

        embed = discord.Embed(
            title=" Invite History — Previous Batch",
            description=(
                f"**Archived:** {archived_display}\n"
                f"**Archived by:** {by_line}\n"
                f"**Invites in batch:** {len(invites)}"
            ),
            color=discord.Color.dark_theme(),
            timestamp=datetime.now(timezone.utc),
        )

        total_uses = 0
        for r in invites:
            code = r.get('invite_code', '?')
            name = r.get('invite_name') or 'Invite'
            max_uses = int(r.get('max_uses', 0) or 0)
            final_uses = int(r.get('final_uses', 0) or 0)
            total_uses += final_uses
            status = r.get('status', 'expired')
            icon = "❌" if status == 'expired' else "✅"
            if max_uses > 0:
                line = f"```diff\n- discord.gg/{code}\n  {name} • {final_uses}/{max_uses} used • {status}\n```"
            else:
                line = f"```diff\n- discord.gg/{code}\n  {name} • {final_uses} used • {status}\n```"
            embed.add_field(name=f"{icon} `{code}` — {name}", value=line, inline=False)

        embed.add_field(name="Batch Summary", value=f"```Total uses: {total_uses}```", inline=False)
        embed.set_footer(text=f"Page {self.page + 1}/{max(1, len(self.groups))} • Use the buttons below to navigate")
        return embed

    @discord.ui.button(label="⬅ Previous", style=discord.ButtonStyle.secondary, custom_id="invite_history_prev")
    async def prev_button(self, interaction: discord.Interaction, button: Button) -> None:
        if self.page > 0:
            self.page -= 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary, custom_id="invite_history_next")
    async def next_button(self, interaction: discord.Interaction, button: Button) -> None:
        if self.page < len(self.groups) - 1:
            self.page += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.danger, custom_id="invite_history_close")
    async def close_button(self, interaction: discord.Interaction, button: Button) -> None:
        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            try:
                await interaction.response.edit_message(view=None)
            except Exception:
                pass
        self.stop()

    async def on_timeout(self) -> None:
        # Auto-clear the ephemeral message on timeout so it doesn't linger.
        # (Discord auto-hides ephemeral messages after a while anyway, but
        # this keeps the buttons from looking stale if the user reopens.)
        pass


# --- INVITE MANAGER CLASS ---
class InviteManager:
    def __init__(self, bot_instance: commands.Bot):
        self.bot = bot_instance
        self.invite_message_id: Optional[int] = None
        self.invite_channel_id: Optional[int] = None
        self.tracked_invites: Dict[str, Dict[str, Any]] = {}
        self.guild_invites: Dict[str, discord.Invite] = {}
    
    def load_data(self) -> None:
        try:
            self.invite_message_id, self.invite_channel_id, self.tracked_invites = data_manager.load_invites()
            logging.info(f"[InviteManager] Loaded {len(self.tracked_invites)} tracked invites from SQLite")
        except Exception as e:
            logging.error(f"[InviteManager] Error loading data: {e}")

    def save_data(self) -> None:
        try:
            data_manager.save_invites(self.invite_message_id, self.invite_channel_id, self.tracked_invites)
        except Exception as e:
            logging.error(f"[InviteManager] Error saving data: {e}")
    
    async def create_invite(self, guild: discord.Guild, max_uses: int, max_age: int = 0) -> Optional[discord.Invite]:
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).create_instant_invite:
                invite = await channel.create_invite(max_uses=max_uses, max_age=max_age, unique=True, reason="Auto-generated by GNG Invite Manager")
                logging.info(f"[InviteManager] Created invite {invite.code} with {max_uses} max uses")
                return invite
        return None
    
    async def check_invite_status(self, guild: discord.Guild, invite_code: str) -> Dict[str, Any]:
        try:
            invites = await guild.invites()
            for invite in invites:
                if invite.code == invite_code:
                    return {'valid': True, 'uses': invite.uses, 'max_uses': invite.max_uses, 'expired': invite.uses >= invite.max_uses if invite.max_uses else False}
            return {'valid': False, 'uses': self.tracked_invites.get(invite_code, {}).get('max_uses', 0), 'max_uses': self.tracked_invites.get(invite_code, {}).get('max_uses', 0), 'expired': True}
        except Exception as e:
            logging.error(f"[InviteManager] Error checking invite {invite_code}: {e}")
            return {'valid': False, 'expired': True, 'uses': 0, 'max_uses': 0}
    
    async def generate_all_invites(self, guild: discord.Guild) -> Dict[str, Dict[str, Any]]:
        new_invites: Dict[str, Dict[str, Any]] = {}
        for cfg in INVITE_CONFIGS:
            invite = await self.create_invite(guild, cfg['max_uses'], cfg['max_age'])
            if invite:
                new_invites[invite.code] = {'name': cfg['name'], 'max_uses': cfg['max_uses'], 'uses': 0, 'status': 'active', 'created_at': datetime.now(timezone.utc).isoformat()}
        return new_invites
    
    async def update_invite_message(self, channel: discord.TextChannel) -> bool:
        if not self.invite_message_id:
            return False
        try:
            message = await channel.fetch_message(self.invite_message_id)
        except discord.NotFound:
            self.invite_message_id = None
            return False
        
        try:
            live_invites = await channel.guild.invites()
            live_invite_data = {inv.code: inv for inv in live_invites}
        except Exception:
            live_invite_data = {}
        
        # Build a richer per-invite data list so the embed builder can show
        # progress bars, status icons, and usage counts without re-parsing.
        invite_rows: List[Dict[str, Any]] = []
        all_expired = True

        for code, data in self.tracked_invites.items():
            max_uses = data.get('max_uses', 0)
            current_uses = live_invite_data[code].uses if code in live_invite_data else data.get('uses', 0)
            is_marked_expired = data['status'] == 'expired' or data.get('expired', False)
            remaining = max_uses - current_uses if max_uses > 0 else None

            if is_marked_expired:
                expired = True
            elif max_uses > 0 and remaining <= 0:
                expired = True
                # Sync the tracked record so the next save reflects reality.
                self.tracked_invites[code]['status'] = 'expired'
                self.tracked_invites[code]['expired'] = True
            else:
                expired = False
                all_expired = False

            invite_rows.append({
                'code': code,
                'name': data.get('name', 'Invite'),
                'max_uses': max_uses,
                'uses': current_uses,
                'remaining': remaining,
                'expired': expired,
                'created_at': data.get('created_at'),
            })

        embed = self._build_invite_embed(invite_rows, all_expired)
        embed.set_footer(text=f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} • {len(invite_rows)} invite(s) tracked")

        # Attach the persistent "View Previous Invites" view so the button on
        # the tracking message keeps working after a bot restart.
        view = InviteTrackingView(channel.guild.id)
        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException:
            # If the view edit fails (e.g. discord rejected the components),
            # fall back to just editing the embed so the message still updates.
            await message.edit(embed=embed)
        self.save_data()
        return True
    
    def _build_invite_embed(self, invite_rows: List[Dict[str, Any]], all_expired: bool) -> discord.Embed:
        """Build the main invite-tracking embed.

        Visual improvements over the old version:
          - Per-invite fields (instead of one big code block) so each invite
            gets its own titled slot with usage details.
          - A simple ASCII usage bar gives a quick visual of how used-up an
            invite is (10 segments). Kept intentionally plain so it doesn't
            look over-polished / AI-generated.
          - Status icons (✅ / ⚠️ / ❌) and color shifts make active vs
            expired invites immediately scannable.
          - A compact summary footer line.
        """
        active_rows = [r for r in invite_rows if not r['expired']]
        expired_rows = [r for r in invite_rows if r['expired']]

        if all_expired and self.tracked_invites:
            title = " Invite Links — All Expired"
            color = discord.Color.red()
            description = "All tracked invites have expired. Run `!regenerateinvites` to mint a fresh batch."
        elif not invite_rows:
            title = " Invite Links"
            color = discord.Color.gold()
            description = "No invites are being tracked yet. Run `!setupinvites` to create the first batch."
        else:
            title = " Invite Links"
            color = discord.Color.dark_theme()
            description = f"**{len(active_rows)}** active • **{len(expired_rows)}** expired"

        embed = discord.Embed(title=title, description=description, color=color,
                               timestamp=datetime.now(timezone.utc))

        # Active invites first, then expired — keeps the useful ones on top.
        for r in active_rows:
            embed.add_field(
                name=f"✅ `{r['code']}` — {r['name']}",
                value=self._format_invite_line(r),
                inline=False,
            )
        for r in expired_rows:
            embed.add_field(
                name=f"❌ `{r['code']}` — {r['name']}",
                value=self._format_invite_line(r),
                inline=False,
            )

        # Compact summary footer line.
        total_uses = sum((r['uses'] or 0) for r in invite_rows)
        embed.add_field(
            name="Summary",
            value=f"```Total uses across all links: {total_uses}```",
            inline=False,
        )
        return embed

    @staticmethod
    def _format_invite_line(r: Dict[str, Any]) -> str:
        """Format one invite's stats as a compact, scannable block.

        The invite URL lives inside its own code block so Discord's "Copy Code"
        button (hover icon) copies ONLY the URL. The usage stats are placed as
        plain text immediately after the code block — same visual position
        (right below the URL) but no longer included in the copy.
        """
        code = r['code']
        max_uses = r.get('max_uses', 0) or 0
        uses = r.get('uses', 0) or 0
        if r.get('expired'):
            if max_uses > 0:
                return f"```diff\n- discord.gg/{code}\n```\n{uses}/{max_uses} used • EXPIRED"
            return f"```diff\n- discord.gg/{code}\n```\nEXPIRED"
        if max_uses > 0:
            remaining = max(0, max_uses - uses)
            return (
                f"```\n"
                f"discord.gg/{code}\n"
                f"```\n"
                f"{uses}/{max_uses} used • {remaining} left"
            )
        # Unlimited invites.
        return f"```\ndiscord.gg/{code}\n```\nUnlimited • {uses} used"

    
    async def post_initial_message(self, channel: discord.TextChannel) -> discord.Message:
        embed = discord.Embed(description="***Invite Links:***\nSetting up invite links...", color=discord.Color.gold())
        message = await channel.send(embed=embed)
        self.invite_message_id = message.id
        self.invite_channel_id = channel.id
        self.save_data()
        return message
    
    async def check_and_update_all(self, guild: discord.Guild, channel: discord.TextChannel) -> bool:
        any_changed = False
        for code in list(self.tracked_invites.keys()):
            status = await self.check_invite_status(guild, code)
            if status['expired'] and self.tracked_invites[code]['status'] != 'expired':
                self.tracked_invites[code]['status'] = 'expired'
                self.tracked_invites[code]['expired'] = True
                self.tracked_invites[code]['uses'] = status['max_uses']
                any_changed = True
                logging.info(f"[InviteManager] Invite {code} has expired")
            elif status['valid']:
                self.tracked_invites[code]['uses'] = status['uses']
                if self.tracked_invites[code]['status'] != 'active':
                    self.tracked_invites[code]['status'] = 'active'
                    any_changed = True
        if any_changed:
            self.save_data()
            await self.update_invite_message(channel)
        return any_changed
    
    async def regenerate_expired(self, guild: discord.Guild, channel: discord.TextChannel, archived_by: Optional[int] = None) -> bool:
        if not self.tracked_invites:
            return False
        all_expired = all(data.get('expired', False) or data['status'] == 'expired' for data in self.tracked_invites.values())
        if all_expired:
            logging.info("[InviteManager] All invites expired, regenerating...")
            # Archive the outgoing batch so the "View Previous Invites" button
            # can show it later. Non-fatal if this fails.
            try:
                data_manager.archive_invite_batch(self.tracked_invites, guild.id, archived_by)
            except Exception as e:
                logging.warning(f"[InviteManager] Could not archive expired batch: {e}")
            new_invites = await self.generate_all_invites(guild)
            if not new_invites:
                return False
            for code in self.tracked_invites:
                self.tracked_invites[code]['expired'] = True
                self.tracked_invites[code]['status'] = 'expired'
            for code, data in new_invites.items():
                self.tracked_invites[code] = data
            self.save_data()
            await self.update_invite_message(channel)
            return True
        return False

    def archive_current_batch(self, guild_id: Optional[int], archived_by: Optional[int]) -> int:
        """Public helper so the !regenerateinvites command can archive the
        current batch BEFORE minting new ones (covers the manual-regenerate
        case where invites may not all be expired yet)."""
        return data_manager.archive_invite_batch(self.tracked_invites, guild_id, archived_by)


# --- BACKGROUND TASKS ---
@tasks.loop(minutes=config.timing.invite_check_interval_minutes)
async def check_invites_task() -> None:
    if not state.invite_manager or not state.invite_manager.invite_channel_id:
        return
    channel = state.invite_manager.bot.get_channel(state.invite_manager.invite_channel_id)
    if not channel:
        return
    guild = channel.guild
    await state.invite_manager.check_and_update_all(guild, channel)
    await state.invite_manager.regenerate_expired(guild, channel)


@check_invites_task.before_loop
async def before_check_invites() -> None:
    await state.bot.wait_until_ready()
    # Wait until invite_manager is actually initialized by on_ready
    while state.invite_manager is None:
        await asyncio.sleep(1)


def _build_invite_commands_embed() -> discord.Embed:
    """Embed listing ONLY the invite-related commands.

    Shown right after !setupinvites finishes so the user knows which commands
    they can use to manage the invite system. Only invite commands are listed
    (no generic moderation / ticket commands).
    """
    embed = discord.Embed(
        title="📨 Invite Commands",
        description=(
            "Here are the commands you can use to manage the invite system.\n"
            "Use them with the bot prefix (`!`)."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="🛠️ Setup & Regeneration",
        value=(
            "`!setupinvites` — Set up the invite tracking panel\n"
            "`!regenerateinvites` — Archive the current batch and mint new invites"
        ),
        inline=False,
    )

    embed.add_field(
        name="📊 Status & Info",
        value=(
            "`!checkinvites` — View the status of all tracked invites\n"
            "`!inviteinfo [code]` — View detailed info about a specific invite"
        ),
        inline=False,
    )

    embed.set_footer(text="Invite Commands • Invite tracking system")
    return embed

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    # --- INVITE MANAGEMENT COMMANDS ---
    @bot.command(name="setupinvites", description="Set up the invite tracking system")
    @commands.has_permissions(manage_guild=True)
    async def setupinvites_cmd(ctx: commands.Context) -> None:
    
        if state.invite_manager.invite_message_id:
            try:
                old_channel = bot.get_channel(state.invite_manager.invite_channel_id)
                if old_channel:
                    old_message = await old_channel.fetch_message(state.invite_manager.invite_message_id)
                    await old_message.delete()
            except Exception:
                pass
    
        message = await state.invite_manager.post_initial_message(ctx.channel)
        new_invites = await state.invite_manager.generate_all_invites(ctx.guild)
    
        if not new_invites:
            await ctx.send("Failed to create invite links. Make sure I have permission to create invites.")
            return
    
        state.invite_manager.tracked_invites = new_invites
        state.invite_manager.save_data()
        await state.invite_manager.update_invite_message(ctx.channel)
    
        # Send the success message and delete it after 3 seconds
        await ctx.send(embed=EmbedBuilder.success("Invite System Setup", f"Created **{len(new_invites)}** invite links."), delete_after=3.0)
        # Show the available invite commands so the user immediately knows what
        # they (and staff) can run to manage the invite system.
        await ctx.send(embed=_build_invite_commands_embed())
        logging.info(f"[InviteManager] Setup completed by {ctx.author} in channel {ctx.channel.name}")


    @bot.command(name="regenerateinvites", description="Regenerate all invite links")
    @commands.has_permissions(manage_guild=True)
    async def regenerateinvites_cmd(ctx: commands.Context) -> None:
    
        if not state.invite_manager.invite_channel_id:
            await ctx.send("Invite system not set up. Use `!setupinvites` first.")
            return
    
        channel = bot.get_channel(state.invite_manager.invite_channel_id)
        if not channel:
            await ctx.send("Invite channel not found.")
            return
    
        # Send the initial progress embed
        progress_embed = discord.Embed(
            title="🔄 Regenerating Invites",
            description="⏳ Deleting old invite links...",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        progress_msg = await ctx.send(embed=progress_embed)
    
        deleted_count = 0
        skipped_count = 0
        bot_user_id = bot.user.id if bot.user else None
        try:
            guild_invites = await ctx.guild.invites()

            # IMPORTANT: delete ANY invite THIS BOT created — not just the ones
            # still present in invite_manager.tracked_invites. After a restart (or
            # a crash that lost the in-memory tracking set, or a DB save that
            # didn't capture every invite), tracked_invites can be empty or stale
            # while the bot's old invites are still live in Discord. The previous
            # code only matched on tracked_invites and silently left those orphan
            # invites behind — which is why !regenerateinvites appeared to "not
            # delete" even though it still minted new ones. Matching on
            # invite.inviter.id == bot.user.id catches every invite this bot
            # account ever made in the guild; the tracked_invites check is kept
            # as a fallback for the rare case where inviter is None but the code
            # is known-tracked.
            for invite in guild_invites:
                is_bot_invite = (
                    bot_user_id is not None
                    and getattr(invite.inviter, "id", None) == bot_user_id
                )
                is_tracked = invite.code in state.invite_manager.tracked_invites
                if not (is_bot_invite or is_tracked):
                    skipped_count += 1
                    continue
                try:
                    await invite.delete(reason=f"Regenerating invite links (by {ctx.author})")
                    deleted_count += 1
                except discord.Forbidden:
                    logging.warning(f"[InviteManager] No permission to delete invite {invite.code}")
                    skipped_count += 1
                except discord.NotFound:
                    # Already gone — counts as deleted for user-facing purposes.
                    deleted_count += 1
                except Exception as e:
                    logging.warning(f"[InviteManager] Could not delete invite {invite.code}: {e}")
                    skipped_count += 1
        except discord.Forbidden:
            progress_embed.description = "❌ I don't have permission to manage invites."
            progress_embed.color = discord.Color.red()
            await progress_msg.edit(embed=progress_embed)
            return
        except Exception as e:
            progress_embed.description = f"❌ Error deleting old invites: {e}"
            progress_embed.color = discord.Color.red()
            await progress_msg.edit(embed=progress_embed)
            return

        # Update progress: Generating new invites
        progress_embed.description = "⏳ Generating new invite links..."
        await progress_msg.edit(embed=progress_embed)

        # Archive the outgoing batch FIRST so the "View Previous Invites" button
        # can show what was just replaced. Non-fatal if it fails.
        if state.invite_manager.tracked_invites:
            try:
                archived = state.invite_manager.archive_current_batch(ctx.guild.id, ctx.author.id)
                logging.info(f"[InviteManager] Archived {archived} old invite(s) to history before regen")
            except Exception as e:
                logging.warning(f"[InviteManager] Could not archive batch before regen: {e}")

        new_invites = await state.invite_manager.generate_all_invites(ctx.guild)
    
        if not new_invites:
            progress_embed.description = "❌ Failed to create new invites. Make sure I have the `Create Invite` permission."
            progress_embed.color = discord.Color.red()
            await progress_msg.edit(embed=progress_embed)
            return

        # Update progress: Updating tracking message
        progress_embed.description = "⏳ Updating tracking message..."
        await progress_msg.edit(embed=progress_embed)
    
        state.invite_manager.tracked_invites = new_invites
        state.invite_manager.save_data()
        await state.invite_manager.update_invite_message(channel)
    
        # Final update: Success!
        summary = (
            f"Deleted **{deleted_count}** old invite(s) and generated **{len(new_invites)}** new one(s)!"
        )
        if skipped_count:
            summary += f"\n*Skipped {skipped_count} invite(s) not created by this bot or undeletable.*"
        final_embed = EmbedBuilder.success("✅ Invites Regenerated", summary)
        await progress_msg.edit(embed=final_embed)
        logging.info(f"[InviteManager] Invites regenerated by {ctx.author} (deleted={deleted_count}, skipped={skipped_count}, new={len(new_invites)})")


    @bot.command(name="checkinvites", description="Display the status of all tracked invites")
    async def checkinvites_cmd(ctx: commands.Context) -> None:
    
        if not state.invite_manager.tracked_invites:
            await ctx.send("No invites are being tracked. Use `!setupinvites` to set up.")
            return
    
        try:
            live_invites = await ctx.guild.invites()
            live_invite_data = {inv.code: inv for inv in live_invites}
        except Exception:
            live_invite_data = {}
    
        # Reuse the same richer embed builder the tracking message uses, so the
        # /checkinvites output looks consistent with the persistent panel.
        invite_rows: List[Dict[str, Any]] = []
        all_expired = True
        for code, data in state.invite_manager.tracked_invites.items():
            max_uses = data.get('max_uses', 0)
            current_uses = live_invite_data[code].uses if code in live_invite_data else data.get('uses', 0)
            remaining = max_uses - current_uses if max_uses > 0 else None
            is_marked_expired = data['status'] == 'expired' or data.get('expired', False)
            if is_marked_expired or (max_uses > 0 and remaining is not None and remaining <= 0):
                expired = True
            else:
                expired = False
                all_expired = False
            invite_rows.append({
                'code': code,
                'name': data.get('name', 'Invite'),
                'max_uses': max_uses,
                'uses': current_uses,
                'remaining': remaining,
                'expired': expired,
                'created_at': data.get('created_at'),
            })

        embed = state.invite_manager._build_invite_embed(invite_rows, all_expired)
        embed.set_footer(text=f"Requested by {ctx.author.display_name} • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        # Attach the same view so /checkinvites also exposes the history button.
        view = InviteTrackingView(ctx.guild.id)
        await ctx.send(embed=embed, view=view)


    @bot.command(name="inviteinfo", description="Display detailed information about a specific invite")
    @app_commands.describe(invite_code="The invite code to check")
    async def inviteinfo_cmd(ctx: commands.Context, invite_code: Optional[str] = None) -> None:
    
        if not state.invite_manager.tracked_invites:
            await ctx.send("No invites are being tracked.")
            return
    
        if not invite_code:
            await checkinvites_cmd(ctx)
            return
    
        invite_code = invite_code.replace("discord.gg/", "").replace("gg/", "").strip()
    
        if invite_code not in state.invite_manager.tracked_invites:
            await ctx.send(f"Invite `{invite_code}` is not being tracked.")
            return
    
        data = state.invite_manager.tracked_invites[invite_code]
        max_uses = data.get('max_uses', 0)
    
        try:
            live_invites = await ctx.guild.invites()
            current_uses = next((inv.uses for inv in live_invites if inv.code == invite_code), data.get('uses', 0))
        except Exception:
            current_uses = data.get('uses', 0)
    
        remaining = max_uses - current_uses
        is_expired = data['status'] == 'expired' or data.get('expired') or (remaining <= 0 and max_uses > 0)
    
        embed = discord.Embed(title=f"Invite: discord.gg/{invite_code}", color=discord.Color.green() if not is_expired else discord.Color.red())
        embed.add_field(name="Name", value=data['name'], inline=True)
        embed.add_field(name="Status", value="EXPIRED" if is_expired else "ACTIVE", inline=True)
        embed.add_field(name="Uses", value=f"{current_uses}/{max_uses} Used", inline=True)
        embed.add_field(name="Remaining", value=f"**{remaining}** uses left" if not is_expired else "None", inline=True)
    
        created_at = data.get('created_at', 'Unknown')
        embed.add_field(name="Created At", value=created_at[:10] if created_at != 'Unknown' else 'Unknown', inline=True)
    
        await ctx.send(embed=embed)
