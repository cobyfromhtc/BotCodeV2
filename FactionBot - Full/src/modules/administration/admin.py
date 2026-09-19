# -*- coding: utf-8 -*-
"""Admin — dbcleanup, shutdown, botstatus, auditlog."""

# stdlib + discord.py
import discord
import logging
from datetime import datetime, timezone
from discord.ext import commands
from discord.ui import Button, View
from typing import List

from core.state import data_manager
from core.helpers import get_uptime
from core.process_manager import process_manager
from core.lifecycle import reset_temporary_data, save_all_data




class DBCleanupConfirmView(View):
    def __init__(
        self,
        user_id: int,
        valid_ticket_ids: set,
        valid_panel_ids: set,
        orphaned_open_ticket_ids: set,
        expired_blacklist_ids: set,
        closed_ticket_ids: set,
        report_lines: List[str],
        guild_id: int,
        clear_invite_history: bool = False,
    ):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.valid_ticket_ids = valid_ticket_ids
        self.valid_panel_ids = valid_panel_ids
        self.orphaned_open_ticket_ids = orphaned_open_ticket_ids
        self.expired_blacklist_ids = expired_blacklist_ids
        self.closed_ticket_ids = closed_ticket_ids
        self.report_lines = report_lines
        self.guild_id = guild_id
        self.clear_invite_history = clear_invite_history

    @discord.ui.button(label="✅ Confirm Cleanup", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the person who ran this command can confirm.", ephemeral=True)
            return

        await interaction.response.defer()

        counts = data_manager.purge_stale_data(
            valid_ticket_ids=self.valid_ticket_ids,
            valid_panel_ids=self.valid_panel_ids,
            orphaned_open_ticket_ids=self.orphaned_open_ticket_ids,
            expired_blacklist_ids=self.expired_blacklist_ids,
            closed_ticket_ids=self.closed_ticket_ids,
        )

        # Clear invite history if the flag was set
        if self.clear_invite_history:
            cleared_invites = data_manager.clear_invite_history(guild_id=self.guild_id)
            counts['invite_history'] = cleared_invites
        else:
            counts['invite_history'] = 0

        total_removed = sum(counts.values())

        result_lines = self.report_lines + [
            "",
            "**Rows removed:**",
            f"  • Closed tickets deleted: {counts.get('closed_tickets', 0)}",
            f"  • Answers deleted: {counts.get('closed_answers', 0) + counts.get('orphan_answers', 0)}",
            f"  • Notes deleted: {counts.get('closed_notes', 0) + counts.get('orphan_notes', 0)}",
            f"  • Cached messages deleted: {counts.get('closed_messages', 0) + counts.get('orphan_messages', 0)}",
            f"  • Transcripts deleted: 0 (PRESERVED — kept for historical reference)",
            f"  • Questions deleted: {counts.get('orphan_questions', 0)}",
            f"  • Open tickets closed (orphaned): {counts.get('orphaned_tickets_closed', 0)}",
            f"  • Blacklist entries removed: {counts.get('ticket_blacklist', 0)}",
            f"  • Inactive warnings removed: {counts.get('warnings', 0)}",
            f"  • Archived invite records deleted: {counts.get('invite_history', 0)}",
            "",
            f"**Total rows cleaned: {total_removed}**",
        ]

        for child in self.children:
            child.disabled = True

        await interaction.edit_original_response(
            embed=discord.Embed(
                title="✅ Database Cleanup — Complete",
                description="\n".join(result_lines),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            ),
            view=self
        )
        logging.info(f"[DBCleanup] Cleanup run by {interaction.user}: {counts}")
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the person who ran this command can cancel.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Database Cleanup — Cancelled",
                description="No changes were made.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            ),
            view=self
        )
        self.stop()

def register(bot: commands.Bot) -> None:
    """Register commands on the bot instance."""


    @bot.command(name="dbcleanup", description="Scan the database and remove stale, invalid, or unused data")
    @commands.has_permissions(administrator=True)
    async def dbcleanup_cmd(ctx: commands.Context) -> None:
        """
    Scans every ticket-related table and cross-checks against live Discord state.
    Removes / closes anything that no longer has a valid corresponding Discord object.

    What it checks:
      - Panels      : is the Discord channel and message still there?
      - Open tickets : does the ticket channel still exist in Discord?
      - Closed tickets: are their answers, notes, messages, transcripts still linked to a real ticket?
      - Blacklist    : are entries expired or already inactive?
      - Warnings     : are there deactivated warning rows taking up space?
      - Invite History: are there archived invite links taking up space?
    """
        await ctx.defer()

        status_msg = await ctx.send(
            embed=discord.Embed(
                title="🔍 Database Cleanup — Scanning...",
                description="Checking every table against live Discord state. Please wait.",
                color=discord.Color.yellow()
            )
        )

        report_lines: List[str] = []

        # ─── 1. PANELS ──────────────────────────────────────────────────────────────
        all_panels = data_manager.load_all_ticket_panels()
        valid_panel_ids: set = set()
        panels_deactivated = 0

        for panel in all_panels:
            if not panel.get('is_active'):
                continue  # Already inactive, skip
            guild = bot.get_guild(panel['guild_id'])
            if not guild:
                # Bot no longer in that guild — deactivate
                panel['is_active'] = 0
                data_manager.save_ticket_panel(panel)
                panels_deactivated += 1
                continue
            channel = guild.get_channel(panel.get('channel_id', 0))
            if not channel:
                panel['is_active'] = 0
                data_manager.save_ticket_panel(panel)
                panels_deactivated += 1
                continue
            # Try to verify the panel message still exists
            msg_id = panel.get('message_id')
            if msg_id:
                try:
                    await channel.fetch_message(msg_id)
                except (discord.NotFound, discord.Forbidden):
                    panel['is_active'] = 0
                    data_manager.save_ticket_panel(panel)
                    panels_deactivated += 1
                    continue
            valid_panel_ids.add(panel['panel_id'])

        if panels_deactivated:
            report_lines.append(f"🗂️ **Panels** — deactivated **{panels_deactivated}** (channel/message gone)")
        else:
            report_lines.append("🗂️ **Panels** — ✅ all active panels valid")

        # ─── 2. TICKETS ──────────────────────────────────────────────────────────
        all_tickets = data_manager.load_all_tickets()
        valid_ticket_ids: set = set()
        closed_ticket_ids: set = set()
        orphaned_open_ticket_ids: set = set()

        for ticket in all_tickets:
            if ticket.get('status') == 'closed':
                # All closed tickets get purged — they're done, no longer needed
                closed_ticket_ids.add(ticket['ticket_id'])
            else:
                # Open ticket — check if its Discord channel still exists
                valid_ticket_ids.add(ticket['ticket_id'])
                guild = bot.get_guild(ticket['guild_id'])
                if not guild:
                    orphaned_open_ticket_ids.add(ticket['ticket_id'])
                    continue
                channel = guild.get_channel(ticket.get('channel_id', 0))
                if not channel:
                    orphaned_open_ticket_ids.add(ticket['ticket_id'])

        if closed_ticket_ids:
            report_lines.append(
                f"🎫 **Closed Tickets** — permanently deleting **{len(closed_ticket_ids)}** "
                f"closed ticket(s) and their linked cache data (answers, notes, messages). "
                f"Transcripts are PRESERVED for historical reference."
            )
        else:
            report_lines.append("🎫 **Closed Tickets** — ✅ none to remove")

        if orphaned_open_ticket_ids:
            report_lines.append(
                f"⚠️ **Orphaned Open Tickets** — marking **{len(orphaned_open_ticket_ids)}** "
                f"as closed (Discord channel no longer exists)"
            )

        # ─── 3. BLACKLIST ────────────────────────────────────────────────────────
        all_blacklist = data_manager.load_all_ticket_blacklist()
        expired_blacklist_ids: set = set()
        now_utc = datetime.now(timezone.utc)

        for entry in all_blacklist:
            if not entry.get('is_active'):
                expired_blacklist_ids.add(entry['blacklist_id'])
                continue
            expires_at = entry.get('expires_at')
            if expires_at:
                try:
                    exp_dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                    if now_utc > exp_dt:
                        expired_blacklist_ids.add(entry['blacklist_id'])
                except Exception:
                    pass

        if expired_blacklist_ids:
            report_lines.append(
                f"🚫 **Ticket Blacklist** — removing **{len(expired_blacklist_ids)}** "
                f"expired/inactive entries"
            )
        else:
            report_lines.append("🚫 **Ticket Blacklist** — ✅ no expired entries")

        # ─── 4. COUNT ORPHANED CHILD ROWS (preview before deleting) ─────────────
        cursor = data_manager._connection.cursor()

        def count_orphans(table: str, fk_col: str, valid_ids: set) -> int:
            if not valid_ids:
                cursor.execute(f'SELECT COUNT(*) FROM {table}')
            else:
                ph = ','.join('?' * len(valid_ids))
                cursor.execute(
                    f'SELECT COUNT(*) FROM {table} WHERE {fk_col} NOT IN ({ph})',
                    list(valid_ids)
                )
            return cursor.fetchone()[0]

        orphan_answers    = count_orphans('ticket_answers',     'ticket_id', valid_ticket_ids)
        orphan_notes      = count_orphans('ticket_notes',       'ticket_id', valid_ticket_ids)
        orphan_messages   = count_orphans('ticket_messages',    'ticket_id', valid_ticket_ids)
        orphan_transcripts= count_orphans('ticket_transcripts', 'ticket_id', valid_ticket_ids)
        orphan_questions  = count_orphans('ticket_questions',   'panel_id',  valid_panel_ids)

        cursor.execute('SELECT COUNT(*) FROM warnings WHERE is_active = 0')
        inactive_warnings = cursor.fetchone()[0]

        child_total = orphan_answers + orphan_notes + orphan_messages + orphan_questions

        if child_total:
            report_lines.append(
                f"🗑️ **Orphaned rows** found (will be deleted):\n"
                f"  • Answers: {orphan_answers}\n"
                f"  • Notes: {orphan_notes}\n"
                f"  • Cached messages: {orphan_messages}\n"
                f"  • Questions: {orphan_questions}\n"
                f"  • Transcripts: {orphan_transcripts} (PRESERVED — not deleted)"
            )
        else:
            report_lines.append(
                f"🗑️ **Orphaned child rows** — ✅ none found to delete "
                f"({orphan_transcripts} transcript(s) preserved)"
            )

        if inactive_warnings:
            report_lines.append(f"⚠️ **Warnings** — removing **{inactive_warnings}** deactivated rows")
        else:
            report_lines.append("⚠️ **Warnings** — ✅ no inactive rows")

        # ─── 5. INVITE HISTORY ────────────────────────────────────────────────────
        cursor.execute('SELECT COUNT(*) FROM invite_history WHERE guild_id = ?', (ctx.guild.id,))
        archived_invites_count = cursor.fetchone()[0]
        clear_invite_history_flag = archived_invites_count > 0

        if clear_invite_history_flag:
            report_lines.append(
                f"📜 **Invite History** — permanently deleting **{archived_invites_count}** "
                f"archived invite record(s) to free up space."
            )
        else:
            report_lines.append("📜 **Invite History** — ✅ none to remove")

        # ─── 6. NOTHING TO DO? ──────────────────────────────────────────────────
        nothing_to_do = (
            panels_deactivated == 0
            and len(closed_ticket_ids) == 0
            and len(orphaned_open_ticket_ids) == 0
            and len(expired_blacklist_ids) == 0
            and child_total == 0
            and inactive_warnings == 0
            and not clear_invite_history_flag
        )

        if nothing_to_do:
            await status_msg.edit(embed=discord.Embed(
                title="✅ Database Cleanup — Nothing to clean",
                description="Every table was checked. All rows are valid and in use.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            ))
            return

        # ─── 7. CONFIRM VIEW ────────────────────────────────────────────────────
        preview_embed = discord.Embed(
            title="🔍 Database Cleanup — Review",
            description="\n".join(report_lines),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        preview_embed.set_footer(text="Press Confirm to permanently apply these changes, or Cancel to abort.")

        confirm_view = DBCleanupConfirmView(
            ctx.author.id,
            valid_ticket_ids=valid_ticket_ids,
            valid_panel_ids=valid_panel_ids,
            orphaned_open_ticket_ids=orphaned_open_ticket_ids,
            expired_blacklist_ids=expired_blacklist_ids,
            closed_ticket_ids=closed_ticket_ids,
            report_lines=report_lines,
            guild_id=ctx.guild.id,
            clear_invite_history=clear_invite_history_flag,
        )
        await status_msg.edit(embed=preview_embed, view=confirm_view)


    # --- SHUTDOWN & STATUS COMMANDS ---
    @bot.command()
    @commands.has_permissions(administrator=True)
    async def shutdown(ctx: commands.Context) -> None:
        if process_manager.is_busy():
            await ctx.send("Cannot shutdown: Users are currently in verification process!")
            return

        await ctx.send("Shutting down bot...")
        logging.info(f"Bot shutdown initiated by {ctx.author}")
        save_all_data()
        reset_temporary_data()
        data_manager.close()
        process_manager.clear_lock_file()
        await bot.close()


    @bot.command()
    @commands.has_permissions(administrator=True)
    async def botstatus(ctx: commands.Context) -> None:
        embed = discord.Embed(title="Bot Status", color=discord.Color.blue())
        embed.add_field(name="Uptime", value=get_uptime(), inline=True)
        embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="Active Verifications", value=str(len(process_manager._active_verifications)), inline=True)
    
        if process_manager.is_busy():
            embed.color = discord.Color.orange()
            embed.add_field(name="Status", value="⚠️ Bot is busy! Avoid restarting.", inline=False)
        else:
            embed.color = discord.Color.green()
            embed.add_field(name="Status", value="✅ Bot is idle. Safe to restart.", inline=False)
    
        await ctx.send(embed=embed)


    # --- AUDIT LOG ---
    @bot.command()
    @commands.has_permissions(view_audit_log=True)
    async def auditlog(ctx: commands.Context, limit: int = 10) -> None:
        try:
            entries: List[str] = []
        
            async for entry in ctx.guild.audit_logs(limit=limit):
                entries.append(f"**{entry.created_at.strftime('%Y-%m-%d %H:%M:%S')}** | {str(entry.action).split('.')[-1]} | User: {entry.user} | Target: {entry.target} | Reason: {entry.reason or 'None'}")
        
            if not entries:
                await ctx.send("No audit log entries found.")
                return
        
            for chunk in [entries[i:i+10] for i in range(0, len(entries), 10)]:
                await ctx.send("\n".join(chunk))
            
        except discord.Forbidden:
            await ctx.send("I don't have permission to view audit logs.")
