# -*- coding: utf-8 -*-
"""invites.py — full invite tracking / attribution system for FactionBot.

Tracks every invite code per guild, attributes new members to the invite they
used (diffing live invite use-counts against a cached snapshot, including
vanity URLs), detects fake invites (member leaving within 10 minutes), keeps
per-inviter statistics and offers a leaderboard, manual bonus adjustments,
configuration and a guided setup flow.

All persistence goes through :mod:`utils.botkit` (same SQLite file as the core
bot, WAL mode). This module never imports from src/bot.py.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import botkit as bk  # relocated into utils/ by the SaaS restructure

log = logging.getLogger(__name__)

__all__ = ("InviteTrackingCog", "setup")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
#: join labels that are not real invite codes
CODE_BOT = "bot"
CODE_VANITY = "vanity"
CODE_UNKNOWN = "unknown"

#: a member leaving this fast after joining counts as a fake invite
FAKE_WINDOW_SECONDS = 600

#: message used when no custom join_message is configured
DEFAULT_JOIN_MESSAGE = "Welcome {user}! You were invited by {inviter} 🎉"

_TABLES: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS invite_codes(
        guild_id      INTEGER NOT NULL,
        code          TEXT    NOT NULL,
        inviter_id    INTEGER NOT NULL DEFAULT 0,
        uses_snapshot INTEGER NOT NULL DEFAULT 0,
        max_uses      INTEGER NOT NULL DEFAULT 0,
        temporary     INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT,
        deleted       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invite_joins(
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        code       TEXT,
        inviter_id INTEGER NOT NULL DEFAULT 0,
        joined_at  TEXT,
        left_at    TEXT,
        fake       INTEGER NOT NULL DEFAULT 0,
        rejoin     INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invite_stats(
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        real     INTEGER NOT NULL DEFAULT 0,
        fake     INTEGER NOT NULL DEFAULT 0,
        bonus    INTEGER NOT NULL DEFAULT 0,
        "left"   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invite_config(
        guild_id       INTEGER PRIMARY KEY,
        log_channel_id INTEGER NOT NULL DEFAULT 0,
        announce_joins INTEGER NOT NULL DEFAULT 1,
        join_message   TEXT,
        track_bots     INTEGER NOT NULL DEFAULT 0
    )
    """,
]

_CONFIG_DEFAULTS: Dict[str, Any] = {
    "log_channel_id": 0,
    "announce_joins": 1,
    "join_message": None,
    "track_bots": 0,
}


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
def _parse_bool(raw: str) -> Optional[bool]:
    """Parse on/off style words → bool (None when unrecognised)."""
    r = raw.strip().lower()
    if r in ("on", "true", "1", "yes", "y", "enable", "enabled"):
        return True
    if r in ("off", "false", "0", "no", "n", "disable", "disabled"):
        return False
    return None


def _require_manage_guild():
    """commands.check gate for Manage Server (prefix commands)."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not ctx.author.guild_permissions.manage_guild:
            raise commands.MissingPermissions(["manage_guild"])
        return True

    return commands.check(predicate)


def _unwrap_error(error: commands.CommandError) -> commands.CommandError:
    """Peel HybridCommandError / CommandInvokeError wrappers."""
    err: Any = error
    for _ in range(4):
        if isinstance(err, (commands.HybridCommandError, commands.CommandInvokeError)):
            inner = getattr(err, "original", None)
            if inner is None:
                break
            err = inner
        else:
            break
    return err


# ---------------------------------------------------------------------------
# UI views
# ---------------------------------------------------------------------------
class _ConfirmView(discord.ui.View):
    """Danger-confirm prompt with a 30 second timeout (used by reset)."""

    def __init__(self, user_id: int, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.result: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who ran the command can answer this.", ephemeral=True
            )
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, result: bool) -> None:
        self.result = result
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, False)


class _SetupView(discord.ui.View):
    """Guided `invites setup` flow — pick a log channel + toggles, then finish."""

    def __init__(self, cog: "InviteTrackingCog", ctx: commands.Context, timeout: float = 120.0) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.ctx = ctx
        self.user_id = ctx.author.id
        self.channel_id: Optional[int] = None
        self.announce = True
        self.track_bots = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who ran the setup can use these controls.", ephemeral=True
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Select the invite log channel…",
        min_values=1,
        max_values=1,
    )
    async def pick_channel(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        channel = select.values[0]
        self.channel_id = channel.id
        await interaction.response.send_message(
            f"Log channel set to {channel.mention}. Press **Finish setup** when ready.",
            ephemeral=True,
        )

    @discord.ui.button(label="Announcements: ON", style=discord.ButtonStyle.success)
    async def toggle_announce(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.announce = not self.announce
        button.label = f"Announcements: {'ON' if self.announce else 'OFF'}"
        button.style = discord.ButtonStyle.success if self.announce else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Track bots: OFF", style=discord.ButtonStyle.secondary)
    async def toggle_bots(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.track_bots = not self.track_bots
        button.label = f"Track bots: {'ON' if self.track_bots else 'OFF'}"
        button.style = discord.ButtonStyle.success if self.track_bots else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Finish setup", style=discord.ButtonStyle.primary)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = self.ctx.guild
        if guild is None:  # pragma: no cover — cog is guild-only
            await interaction.response.edit_message(view=None)
            self.stop()
            return
        self.cog._set_config(
            guild.id,
            log_channel_id=self.channel_id or 0,
            announce_joins=1 if self.announce else 0,
            track_bots=1 if self.track_bots else 0,
        )
        synced = await self.cog.refresh_cache(guild)
        codes = len(self.cog._cache.get(guild.id, {}))
        channel_line = f"<#{self.channel_id}>" if self.channel_id else "*(not set)*"
        desc = (
            f"Log channel: {channel_line}\n"
            f"Join announcements: **{'on' if self.announce else 'off'}**\n"
            f"Bot tracking: **{'on' if self.track_bots else 'off'}**\n"
            f"Invite cache: **{codes}** code(s) tracked"
        )
        if not synced:
            desc += "\n⚠️ I could not read this server's invites — I need the **Manage Server** permission for attribution."
        embed = bk.success("Invite tracking is configured", desc)
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=bk.neutral("Setup cancelled — nothing was changed."), view=None)
        self.stop()


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class InviteTrackingCog(commands.Cog, name="InviteTracking"):
    """Invite tracking, attribution, leaderboards and configuration."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # guild_id → {code: uses}
        self._cache: Dict[int, Dict[str, int]] = {}
        # guild_id → {code: Invite} (full objects so we can read inviter etc.)
        self._invite_objs: Dict[int, Dict[str, discord.Invite]] = {}
        # guild_id → unix timestamp of the last successful snapshot
        self._last_refresh: Dict[int, float] = {}
        # on_ready once-guard: only prime the cache on the first ready event
        self._did_initial_sync = False
        bk.create_tables(_TABLES)
        log.info("InviteTrackingCog loaded")

    # ------------------------------------------------------------------
    # Guild configuration helpers
    # ------------------------------------------------------------------
    def _get_config(self, guild_id: int) -> Dict[str, Any]:
        cfg = dict(_CONFIG_DEFAULTS)
        row = bk.fetchone(
            "SELECT log_channel_id, announce_joins, join_message, track_bots "
            "FROM invite_config WHERE guild_id=?",
            (guild_id,),
        )
        if row is not None:
            for key in cfg:
                if row[key] is not None:
                    cfg[key] = row[key]
        return cfg

    def _set_config(self, guild_id: int, **values: Any) -> None:
        keys = [k for k in values if k in _CONFIG_DEFAULTS]
        if not keys:
            return
        row = bk.fetchone("SELECT guild_id FROM invite_config WHERE guild_id=?", (guild_id,))
        if row is None:
            base = dict(_CONFIG_DEFAULTS)
            base.update({k: values[k] for k in keys})
            bk.run(
                "INSERT INTO invite_config(guild_id, log_channel_id, announce_joins, "
                "join_message, track_bots) VALUES(?,?,?,?,?)",
                (guild_id, base["log_channel_id"], base["announce_joins"],
                 base["join_message"], base["track_bots"]),
            )
        else:
            sets = ", ".join(f"{k}=?" for k in keys)
            args: List[Any] = [values[k] for k in keys]
            args.append(guild_id)
            bk.run(f"UPDATE invite_config SET {sets} WHERE guild_id=?", tuple(args))

    def _log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        channel_id = int(self._get_config(guild.id).get("log_channel_id") or 0)
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    # ------------------------------------------------------------------
    # Stats helpers
    # ------------------------------------------------------------------
    def _stats_row(self, guild_id: int, user_id: int) -> Dict[str, int]:
        row = bk.fetchone(
            'SELECT real, fake, bonus, "left" FROM invite_stats WHERE guild_id=? AND user_id=?',
            (guild_id, user_id),
        )
        if row is None:
            return {"real": 0, "fake": 0, "bonus": 0, "left": 0}
        return {k: int(row[k] or 0) for k in ("real", "fake", "bonus", "left")}

    def _total_invites(self, guild_id: int, user_id: int) -> int:
        s = self._stats_row(guild_id, user_id)
        return max(0, s["real"] + s["bonus"] - s["fake"] - s["left"])

    def _bump_stats(
        self, guild_id: int, user_id: int, *,
        real: int = 0, fake: int = 0, bonus: int = 0, left: int = 0,
    ) -> None:
        cur = self._stats_row(guild_id, user_id)
        new = {
            "real": max(0, cur["real"] + real),
            "fake": max(0, cur["fake"] + fake),
            "bonus": max(0, cur["bonus"] + bonus),
            "left": max(0, cur["left"] + left),
        }
        bk.run(
            'INSERT OR REPLACE INTO invite_stats(guild_id, user_id, real, fake, bonus, "left") '
            "VALUES(?,?,?,?,?,?)",
            (guild_id, user_id, new["real"], new["fake"], new["bonus"], new["left"]),
        )

    def _store_code(
        self, guild_id: int, code: str, inviter_id: int, uses: int,
        max_uses: int = 0, temporary: int = 0, created_at: Optional[str] = None,
    ) -> None:
        bk.run(
            "INSERT OR REPLACE INTO invite_codes(guild_id, code, inviter_id, uses_snapshot, "
            "max_uses, temporary, created_at, deleted) VALUES(?,?,?,?,?,?,?,0)",
            (guild_id, code, inviter_id, uses, max_uses, temporary, created_at),
        )

    def _inviter_id_for(
        self, guild_id: int, code: str, invite_obj: Optional[discord.Invite]
    ) -> int:
        if invite_obj is not None and invite_obj.inviter is not None:
            return invite_obj.inviter.id
        row = bk.fetchone(
            "SELECT inviter_id FROM invite_codes WHERE guild_id=? AND code=?", (guild_id, code)
        )
        if row is not None and row["inviter_id"]:
            return int(row["inviter_id"])
        return 0

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------
    async def refresh_cache(self, guild: discord.Guild) -> bool:
        """Snapshot every live invite's use count for a guild.

        Returns False when the invite list could not be fetched (typically a
        missing *Manage Server* permission).
        """
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not fetch invites for guild %s: %s", guild.id, exc)
            return False
        counts: Dict[str, int] = {}
        objs: Dict[str, discord.Invite] = {}
        for invite in invites:
            counts[invite.code] = invite.uses or 0
            objs[invite.code] = invite
        self._cache[guild.id] = counts
        self._invite_objs[guild.id] = objs
        self._last_refresh[guild.id] = bk.now_ts()
        return True

    # ------------------------------------------------------------------
    # Public API for other systems
    # ------------------------------------------------------------------
    async def get_inviter(self, guild_id: int, user_id: int) -> Optional[int]:
        """Inviter ID of the latest recorded join for a member (None if unknown)."""
        row = bk.fetchone(
            "SELECT inviter_id FROM invite_joins WHERE guild_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT 1",
            (guild_id, user_id),
        )
        if row is None:
            return None
        inviter = row["inviter_id"]
        return int(inviter) if inviter else None

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._did_initial_sync:
            return
        self._did_initial_sync = True
        refreshed = 0
        for guild in self.bot.guilds:
            if await self.refresh_cache(guild):
                refreshed += 1
        log.info("Invite cache primed for %d/%d guild(s)", refreshed, len(self.bot.guilds))

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if guild is None:
            return
        inviter_id = invite.inviter.id if invite.inviter is not None else 0
        created_at = invite.created_at.isoformat() if invite.created_at else None
        self._store_code(
            guild.id, invite.code, inviter_id, invite.uses or 0,
            invite.max_uses or 0, 1 if invite.temporary else 0, created_at,
        )
        self._cache.setdefault(guild.id, {})[invite.code] = invite.uses or 0
        self._invite_objs.setdefault(guild.id, {})[invite.code] = invite

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if guild is None:
            return
        bk.run(
            "UPDATE invite_codes SET deleted=1 WHERE guild_id=? AND code=?",
            (guild.id, invite.code),
        )
        self._cache.get(guild.id, {}).pop(invite.code, None)
        self._invite_objs.get(guild.id, {}).pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        cfg = self._get_config(guild.id)

        # 1. bots are only tracked when explicitly enabled
        if member.bot and not cfg.get("track_bots"):
            bk.run(
                "INSERT INTO invite_joins(guild_id, user_id, code, inviter_id, joined_at) "
                "VALUES(?,?,?,?,?)",
                (guild.id, member.id, CODE_BOT, 0, bk.now_iso()),
            )
            return

        # 2. diff live invite use counts against the cached snapshot
        try:
            fresh_list: Optional[List[discord.Invite]] = await guild.invites()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not fetch invites during join in guild %s: %s", guild.id, exc)
            fresh_list = None
            # The cached snapshot can no longer be diffed against reality, so
            # drop it. The NEXT join then runs through the "unprimed" path
            # (correctly reported as `unknown`) rather than being silently
            # mis-attributed against two-joins-ago state.
            self._cache.pop(guild.id, None)
            self._invite_objs.pop(guild.id, None)
            self._last_refresh.pop(guild.id, None)

        attributed_code = CODE_UNKNOWN
        inviter_id = 0
        fresh_counts: Dict[str, int] = {}
        fresh_objs: Dict[str, discord.Invite] = {}

        if fresh_list is not None:
            fresh_counts = {i.code: (i.uses or 0) for i in fresh_list}
            fresh_objs = {i.code: i for i in fresh_list}
            before = self._cache.get(guild.id, {})
            primed = guild.id in self._cache

            best_code: Optional[str] = None
            best_delta = 0
            # (A) an invite whose use count grew since our snapshot
            for code, before_uses in before.items():
                fresh_uses = fresh_counts.get(code)
                if fresh_uses is None:
                    continue
                delta = fresh_uses - before_uses
                if delta > best_delta:
                    best_code, best_delta = code, delta
            if best_code is None and primed:
                # (B) invite created (and used) between our snapshot and now —
                # only trusted when we actually had a baseline snapshot.
                new_uses = -1
                for code, invite_obj in fresh_objs.items():
                    if code in before:
                        continue
                    if (invite_obj.uses or 0) > new_uses:
                        best_code, new_uses = code, invite_obj.uses or 0
                if new_uses < 1:
                    best_code = None

            if best_code is not None:
                attributed_code = best_code
                inviter_id = self._inviter_id_for(guild.id, best_code, fresh_objs.get(best_code))

        # 4. vanity URL attribution (only when no regular invite matched)
        if attributed_code == CODE_UNKNOWN:
            vanity_code = getattr(guild, "vanity_url_code", None)
            if vanity_code:
                current_uses: Optional[int] = None
                try:
                    vanity = await guild.vanity_invite()
                except (LookupError, discord.Forbidden, discord.HTTPException):
                    vanity = None
                if vanity is not None and vanity.uses is not None:
                    current_uses = vanity.uses
                if current_uses is not None:
                    row = bk.fetchone(
                        "SELECT uses_snapshot FROM invite_codes WHERE guild_id=? AND code=?",
                        (guild.id, vanity_code),
                    )
                    stored = int(row["uses_snapshot"] or 0) if row is not None else None
                    # keep the vanity snapshot fresh either way
                    self._store_code(guild.id, vanity_code, 0, current_uses)
                    if stored is not None and current_uses > stored:
                        attributed_code = CODE_VANITY
                        inviter_id = 0

        # 6. record the join
        joined_at = bk.now_iso()
        prev = bk.fetchone(
            "SELECT id, left_at FROM invite_joins WHERE guild_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT 1",
            (guild.id, member.id),
        )
        rejoin = 0
        if prev is not None:
            rejoin = 1
            if prev["left_at"] is None:
                # the member came back without the bot seeing the leave —
                # close the stale row so remove handling stays consistent
                bk.run("UPDATE invite_joins SET left_at=? WHERE id=?", (joined_at, prev["id"]))
        bk.run(
            "INSERT INTO invite_joins(guild_id, user_id, code, inviter_id, joined_at, rejoin) "
            "VALUES(?,?,?,?,?,?)",
            (guild.id, member.id, attributed_code, inviter_id, joined_at, rejoin),
        )

        if inviter_id:
            self._bump_stats(guild.id, inviter_id, real=1)

        # keep the invite_codes snapshot in sync for the used code
        if attributed_code not in (CODE_UNKNOWN, CODE_VANITY, CODE_BOT):
            invite_obj = fresh_objs.get(attributed_code)
            if invite_obj is not None:
                self._store_code(
                    guild.id, attributed_code, inviter_id, fresh_counts.get(attributed_code, 0),
                    invite_obj.max_uses or 0, 1 if invite_obj.temporary else 0,
                    invite_obj.created_at.isoformat() if invite_obj.created_at else None,
                )
            else:
                bk.run(
                    "UPDATE invite_codes SET uses_snapshot=uses_snapshot+1 "
                    "WHERE guild_id=? AND code=?",
                    (guild.id, attributed_code),
                )

        # refresh the cache with what we just saw
        if fresh_list is not None:
            self._cache[guild.id] = fresh_counts
            self._invite_objs[guild.id] = fresh_objs
            self._last_refresh[guild.id] = bk.now_ts()

        inviter_total = self._total_invites(guild.id, inviter_id) if inviter_id else 0
        try:
            await self._announce_and_log_join(member, attributed_code, inviter_id, inviter_total)
        except Exception:  # noqa: BLE001 — never break the join event
            log.exception("Failed to announce invite join for member %s", member.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        row = bk.fetchone(
            "SELECT id, code, inviter_id, joined_at FROM invite_joins "
            "WHERE guild_id=? AND user_id=? AND left_at IS NULL ORDER BY id DESC LIMIT 1",
            (guild.id, member.id),
        )
        if row is None:
            return  # join happened while the bot was offline — nothing to close
        now = bk.now_iso()
        bk.run("UPDATE invite_joins SET left_at=? WHERE id=?", (now, row["id"]))
        joined = bk.parse_iso(row["joined_at"])
        duration = max(0.0, bk.now_ts() - joined.timestamp()) if joined is not None else 0.0
        inviter_id = int(row["inviter_id"] or 0)

        fake = duration < FAKE_WINDOW_SECONDS
        if fake:
            bk.run("UPDATE invite_joins SET fake=1 WHERE id=?", (row["id"],))
            if inviter_id:
                self._bump_stats(guild.id, inviter_id, real=-1, fake=1)
        else:
            if inviter_id:
                self._bump_stats(guild.id, inviter_id, left=1)

        # leave log
        log_channel = self._log_channel(guild)
        if log_channel is not None:
            code = row["code"] or CODE_UNKNOWN
            code_display = "Vanity URL" if code == CODE_VANITY else code
            embed = bk.warning(
                "Member left",
                f"{member.mention} left after **{bk.fmt_duration(duration)}**.",
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Invite code", value=f"`{code_display}`", inline=True)
            if inviter_id:
                embed.add_field(name="Invited by", value=f"<@{inviter_id}>", inline=True)
            embed.add_field(
                name="Fake invite",
                value="Yes — inviter stats adjusted" if fake else "No",
                inline=True,
            )
            try:
                await log_channel.send(embed=embed)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                log.warning("Could not send leave log in guild %s: %s", guild.id, exc)

    # ------------------------------------------------------------------
    # Join announcement + log
    # ------------------------------------------------------------------
    async def _announce_and_log_join(
        self, member: discord.Member, code: str, inviter_id: int, inviter_total: int
    ) -> None:
        guild = member.guild
        cfg = self._get_config(guild.id)
        log_channel = self._log_channel(guild)
        code_display = "Vanity URL" if code == CODE_VANITY else (code or CODE_UNKNOWN)
        inviter_display = f"<@{inviter_id}>" if inviter_id else (
            "Vanity URL" if code == CODE_VANITY else "Unknown"
        )

        if log_channel is not None:
            embed = bk.info("Member joined", f"{member.mention} ({member}) just joined.")
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Invited by", value=inviter_display, inline=True)
            embed.add_field(name="Invite code", value=f"`{code_display}`", inline=True)
            if inviter_id:
                embed.add_field(name="Inviter total", value=str(inviter_total), inline=True)
            try:
                await log_channel.send(embed=embed)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                log.warning("Could not send join log in guild %s: %s", guild.id, exc)

        if not cfg.get("announce_joins"):
            return
        template = cfg.get("join_message") or DEFAULT_JOIN_MESSAGE
        placeholders = {
            "user": member.mention,
            "inviter": inviter_display,
            "inviter_total": inviter_total,
            "code": code_display,
            "server": guild.name,
        }
        try:
            text = template.format(**placeholders)
        except (KeyError, IndexError, ValueError):
            text = DEFAULT_JOIN_MESSAGE.format(**placeholders)
        destination = log_channel or guild.system_channel
        if destination is None:
            return
        try:
            await destination.send(
                content=text[:2000],
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            log.warning("Could not send join announcement in guild %s: %s", guild.id, exc)

    # ------------------------------------------------------------------
    # Command helpers
    # ------------------------------------------------------------------
    async def _send_invite_card(self, ctx: commands.Context, target: discord.abc.User) -> None:
        stats = self._stats_row(ctx.guild.id, target.id)
        total = max(0, stats["real"] + stats["bonus"] - stats["fake"] - stats["left"])
        embed = bk.info(
            "Invite Card",
            f"{target.mention} has **{total}** invite{'s' if total != 1 else ''}.",
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="🟢 Real", value=str(stats["real"]), inline=True)
        embed.add_field(name="🎁 Bonus", value=str(stats["bonus"]), inline=True)
        embed.add_field(name="❌ Fake", value=str(stats["fake"]), inline=True)
        embed.add_field(name="👋 Left", value=str(stats["left"]), inline=True)
        await ctx.send(embed=embed)

    async def _run_setup(self, ctx: commands.Context) -> None:
        view = _SetupView(self, ctx)
        embed = bk.info(
            "Invite Tracking Setup",
            "Configure invite tracking in a few clicks:\n"
            "1. **Select the channel** for join/leave logs.\n"
            "2. Toggle join **announcements** and **bot tracking**.\n"
            "3. Press **Finish setup** — the invite cache is built automatically.\n"
            f"(You can also change everything later with `{ctx.clean_prefix}invites config`.)",
        )
        await ctx.send(embed=embed, view=view)

    # ------------------------------------------------------------------
    # Commands — `invites` group
    # ------------------------------------------------------------------
    @commands.group(name="invites", aliases=["invite"], invoke_without_command=True)
    @commands.guild_only()
    @app_commands.describe(member="Member whose invite card to show (defaults to you)")
    async def invites_command(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Show invite counts for a member (total = real + bonus - fake - left)."""
        target = member if member is not None else ctx.author
        await self._send_invite_card(ctx, target)

    @invites_command.command(name="leaderboard")
    @commands.guild_only()
    async def leaderboard_cmd(self, ctx: commands.Context) -> None:
        """Show the top 10 inviters of this server."""
        rows = bk.fetchall(
            'SELECT user_id, real, fake, bonus, "left" FROM invite_stats WHERE guild_id=? '
            'ORDER BY (real + bonus - fake - "left") DESC, real DESC LIMIT 10',
            (ctx.guild.id,),
        )
        if not rows:
            await ctx.send(embed=bk.info("Invite Leaderboard", "No invites have been tracked yet."))
            return
        medals = ("🥇", "🥈", "🥉")
        lines: List[str] = []
        for i, row in enumerate(rows, start=1):
            total = max(0, row["real"] + row["bonus"] - row["fake"] - row["left"])
            tag = medals[i - 1] if i <= 3 else f"**{i}.**"
            lines.append(
                f"{tag} <@{row['user_id']}> — **{total}** invites "
                f"(`{row['real']}` real · `{row['bonus']}` bonus · "
                f"`{row['fake']}` fake · `{row['left']}` left)"
            )
        embed = bk.info("Invite Leaderboard 🏆", "\n".join(lines))
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @invites_command.command(name="info")
    @commands.guild_only()
    @app_commands.describe(member="Member whose invite details to show")
    async def info_cmd(self, ctx: commands.Context, member: discord.Member) -> None:
        """Detailed invite information for a member."""
        stats = self._stats_row(ctx.guild.id, member.id)
        total = max(0, stats["real"] + stats["bonus"] - stats["fake"] - stats["left"])
        joins_row = bk.fetchone(
            "SELECT COUNT(*) AS n FROM invite_joins WHERE guild_id=? AND inviter_id=?",
            (ctx.guild.id, member.id),
        )
        joins = int(joins_row["n"]) if joins_row is not None else 0
        codes = bk.fetchall(
            "SELECT code, uses_snapshot, max_uses, temporary, created_at, deleted "
            "FROM invite_codes WHERE guild_id=? AND inviter_id=? "
            "ORDER BY created_at DESC, code LIMIT 15",
            (ctx.guild.id, member.id),
        )

        embed = bk.info(f"{member.display_name} — Invite Details", f"{member.mention}")
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Total invites", value=str(total), inline=True)
        embed.add_field(name="Joins attributed", value=str(joins), inline=True)
        embed.add_field(name="Real / Bonus", value=f"{stats['real']} / {stats['bonus']}", inline=True)
        embed.add_field(name="Fake / Left", value=f"{stats['fake']} / {stats['left']}", inline=True)
        if codes:
            code_lines: List[str] = []
            for c in codes:
                max_uses = c["max_uses"] or 0
                uses = f"{c['uses_snapshot']}/{max_uses if max_uses else '∞'}"
                flags = []
                if c["temporary"]:
                    flags.append("temporary")
                if c["deleted"]:
                    flags.append("deleted")
                suffix = f" · {', '.join(flags)}" if flags else ""
                code_lines.append(f"`{c['code']}` — {uses} uses{suffix}")
            embed.add_field(
                name=f"Invite codes ({len(codes)} shown)",
                value="\n".join(code_lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(name="Invite codes", value="No invites created.", inline=False)
        await ctx.send(embed=embed)

    @invites_command.command(name="add")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        member="Member to give bonus invites to",
        amount="Bonus invites to add (1-10000)",
    )
    async def add_cmd(
        self, ctx: commands.Context, member: discord.Member,
        amount: app_commands.Range[int, 1, 10000],
    ) -> None:
        """Add bonus invites to a member (Manage Server)."""
        self._bump_stats(ctx.guild.id, member.id, bonus=int(amount))
        total = self._total_invites(ctx.guild.id, member.id)
        await ctx.send(
            embed=bk.success(
                "Bonus invites added",
                f"Added **{amount}** bonus invite(s) to {member.mention} — "
                f"they now have **{total}** total.",
            )
        )

    @invites_command.command(name="remove")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        member="Member to remove bonus invites from",
        amount="Bonus invites to remove (1-10000)",
    )
    async def remove_cmd(
        self, ctx: commands.Context, member: discord.Member,
        amount: app_commands.Range[int, 1, 10000],
    ) -> None:
        """Remove bonus invites from a member (Manage Server)."""
        self._bump_stats(ctx.guild.id, member.id, bonus=-int(amount))
        total = self._total_invites(ctx.guild.id, member.id)
        await ctx.send(
            embed=bk.success(
                "Bonus invites removed",
                f"Removed **{amount}** bonus invite(s) from {member.mention} — "
                f"they now have **{total}** total.",
            )
        )

    @invites_command.command(name="reset")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(target="A member (ID / mention / name) or 'all' to reset the server")
    async def reset_cmd(self, ctx: commands.Context, target: str) -> None:
        """Reset invite stats for a member or the whole server (Manage Server)."""
        raw = target.strip()
        member: Optional[discord.Member] = None
        if raw.lower() in ("all", "*", "everyone", "server"):
            scope_desc = (
                "This will **delete every invite stat row for this server**.\n"
                "Join history (`invite_joins`) is kept — only the counters are reset."
            )
        else:
            try:
                member = await commands.MemberConverter().convert(ctx, raw)
            except commands.MemberNotFound:
                await ctx.send(
                    embed=bk.error(
                        "Member not found",
                        f"I couldn't find `{raw[:100]}` in this server. "
                        "Pass a mention, ID, or the word `all`.",
                    )
                )
                return
            scope_desc = f"This will reset **{member.mention}**'s invite counters to zero."

        view = _ConfirmView(ctx.author.id, timeout=30.0)
        await ctx.send(embed=bk.warning("Confirm invite reset", scope_desc), view=view)
        await view.wait()

        if view.result is not True:
            await ctx.send(embed=bk.neutral("Reset cancelled — nothing was changed."))
            return
        if member is not None:
            bk.run(
                "DELETE FROM invite_stats WHERE guild_id=? AND user_id=?",
                (ctx.guild.id, member.id),
            )
            await ctx.send(
                embed=bk.success("Invite stats reset", f"{member.mention}'s counters were reset.")
            )
        else:
            bk.run("DELETE FROM invite_stats WHERE guild_id=?", (ctx.guild.id,))
            await ctx.send(
                embed=bk.success(
                    "Invite stats reset", "All invite counters for this server were reset."
                )
            )

    @invites_command.command(name="config")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        key="Setting to view/change: log_channel, announce_joins, join_message, track_bots",
        value="New value for the setting (quote multi-word messages)",
    )
    async def config_cmd(
        self, ctx: commands.Context, key: Optional[str] = None, value: Optional[str] = None
    ) -> None:
        """View or change invite tracking settings (Manage Server)."""
        cfg = self._get_config(ctx.guild.id)

        def _config_embed() -> discord.Embed:
            channel = (
                f"<#{cfg['log_channel_id']}>" if cfg.get("log_channel_id") else "*(not set)*"
            )
            message = cfg.get("join_message") or f"{DEFAULT_JOIN_MESSAGE} *(default)*"
            embed = bk.info("Invite Tracking Configuration")
            embed.add_field(name="Log channel", value=channel, inline=False)
            embed.add_field(
                name="Announce joins",
                value="on" if cfg.get("announce_joins") else "off", inline=True,
            )
            embed.add_field(
                name="Track bots",
                value="on" if cfg.get("track_bots") else "off", inline=True,
            )
            embed.add_field(name="Join message", value=message[:1024], inline=False)
            embed.set_footer(text=(
                f"Change with {ctx.clean_prefix}invites config <key> <value> — keys: "
                "log_channel, announce_joins, join_message, track_bots"
            ))
            return embed

        if key is None:
            await ctx.send(embed=_config_embed())
            return
        k = key.strip().lower()

        if value is None:
            if k in ("log_channel", "log_channel_id", "logs", "log"):
                current = (
                    f"<#{cfg['log_channel_id']}>" if cfg.get("log_channel_id") else "not set"
                )
                desc = f"`log_channel` is currently {current}."
            elif k in ("announce_joins", "announce", "announcements"):
                desc = f"`announce_joins` is currently **{'on' if cfg.get('announce_joins') else 'off'}**."
            elif k in ("join_message", "join_msg", "message"):
                desc = f"`join_message` is currently:\n{cfg.get('join_message') or DEFAULT_JOIN_MESSAGE}"
            elif k in ("track_bots", "bots"):
                desc = f"`track_bots` is currently **{'on' if cfg.get('track_bots') else 'off'}**."
            else:
                await ctx.send(
                    embed=bk.error(
                        "Unknown setting",
                        "Valid keys: `log_channel`, `announce_joins`, `join_message`, `track_bots`.",
                    )
                )
                return
            await ctx.send(embed=bk.info("Invite config", desc[:2000]))
            return

        if k in ("log_channel", "log_channel_id", "logs", "log"):
            raw = value.strip()
            if raw.lower() in ("off", "none", "disable", "reset", "clear", "0"):
                self._set_config(ctx.guild.id, log_channel_id=0)
                await ctx.send(embed=bk.success("Log channel cleared", "Join/leave logs are now off."))
                return
            channel: Optional[discord.TextChannel] = None
            try:
                channel = await commands.TextChannelConverter().convert(ctx, raw)
            except commands.ChannelNotFound:
                if raw.strip("<#>").isdigit():
                    channel = ctx.guild.get_channel(int(raw.strip("<#>")))
            if channel is None:
                await ctx.send(
                    embed=bk.error("Channel not found", f"I couldn't find a text channel from `{raw[:100]}`.")
                )
                return
            self._set_config(ctx.guild.id, log_channel_id=channel.id)
            await ctx.send(
                embed=bk.success("Log channel set", f"Join/leave logs will go to {channel.mention}.")
            )
        elif k in ("announce_joins", "announce", "announcements"):
            flag = _parse_bool(value)
            if flag is None:
                await ctx.send(embed=bk.error("Invalid value", "Use `on` or `off`."))
                return
            self._set_config(ctx.guild.id, announce_joins=1 if flag else 0)
            await ctx.send(
                embed=bk.success("Join announcements", f"Join announcements are now **{'on' if flag else 'off'}**.")
            )
        elif k in ("join_message", "join_msg", "message"):
            raw = value.strip()
            if raw.lower() in ("off", "default", "reset", "none"):
                self._set_config(ctx.guild.id, join_message=None)
                await ctx.send(
                    embed=bk.success(
                        "Join message reset",
                        f"Using the default message:\n{DEFAULT_JOIN_MESSAGE}",
                    )
                )
                return
            if len(raw) > 1000:
                await ctx.send(embed=bk.error("Message too long", "Keep the join message under 1000 characters."))
                return
            try:
                preview = raw.format(
                    user=ctx.author.mention, inviter="the inviter", inviter_total=12,
                    code="AbC123", server=ctx.guild.name,
                )
            except (KeyError, IndexError, ValueError):
                await ctx.send(
                    embed=bk.error(
                        "Invalid placeholders",
                        "The join message may only use `{user}`, `{inviter}`, "
                        "`{inviter_total}`, `{code}` and `{server}` — it was **not** saved.",
                    )
                )
                return
            self._set_config(ctx.guild.id, join_message=raw)
            await ctx.send(
                embed=bk.success("Join message set", "Preview:\n" + preview[:1500])
            )
        elif k in ("track_bots", "bots"):
            flag = _parse_bool(value)
            if flag is None:
                await ctx.send(embed=bk.error("Invalid value", "Use `on` or `off`."))
                return
            self._set_config(ctx.guild.id, track_bots=1 if flag else 0)
            await ctx.send(
                embed=bk.success("Bot tracking", f"Bot join tracking is now **{'on' if flag else 'off'}**.")
            )
        else:
            await ctx.send(
                embed=bk.error(
                    "Unknown setting",
                    "Valid keys: `log_channel`, `announce_joins`, `join_message`, `track_bots`.",
                )
            )

    @invites_command.command(name="cache")
    @commands.guild_only()
    async def cache_cmd(self, ctx: commands.Context) -> None:
        """Show the in-memory invite cache status."""
        guild_count = len(self._cache)
        code_count = sum(len(c) for c in self._cache.values())
        guild_codes = self._cache.get(ctx.guild.id, {})
        last = self._last_refresh.get(ctx.guild.id)
        if last is not None:
            age = f"{bk.fmt_duration(bk.now_ts() - last)} ago"
        else:
            age = "never"
        embed = bk.info(
            "Invite Cache Status",
            "\n".join(
                (
                    f"Guilds cached: **{guild_count}**",
                    f"Total codes cached: **{code_count}**",
                    f"This server: **{len(guild_codes)}** code(s)",
                    f"Last refresh (this server): **{age}**",
                )
            ),
        )
        await ctx.send(embed=embed)

    @invites_command.command(name="resync")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    async def resync_cmd(self, ctx: commands.Context) -> None:
        """Rebuild the invite cache and reconcile stored invite codes (Manage Server)."""
        ok = await self.refresh_cache(ctx.guild)
        if not ok:
            await ctx.send(
                embed=bk.error(
                    "Could not fetch invites",
                    "I need the **Manage Server** permission to read this server's invites.",
                )
            )
            return
        objs = self._invite_objs.get(ctx.guild.id, {})
        vanity_code = getattr(ctx.guild, "vanity_url_code", None)
        live = set(objs)

        marked_stale = 0
        for row in bk.fetchall(
            "SELECT code, deleted FROM invite_codes WHERE guild_id=?", (ctx.guild.id,)
        ):
            if row["code"] not in live and not row["deleted"] and row["code"] != vanity_code:
                bk.run(
                    "UPDATE invite_codes SET deleted=1 WHERE guild_id=? AND code=?",
                    (ctx.guild.id, row["code"]),
                )
                marked_stale += 1

        upserted = 0
        for code, invite_obj in objs.items():
            self._store_code(
                ctx.guild.id,
                code,
                invite_obj.inviter.id if invite_obj.inviter is not None else 0,
                invite_obj.uses or 0,
                invite_obj.max_uses or 0,
                1 if invite_obj.temporary else 0,
                invite_obj.created_at.isoformat() if invite_obj.created_at else None,
            )
            upserted += 1

        await ctx.send(
            embed=bk.success(
                "Invite cache resynced",
                f"Codes found: **{len(objs)}** · stored rows upserted: **{upserted}** · "
                f"stale codes marked deleted: **{marked_stale}**",
            )
        )

    @invites_command.command(name="setup")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    async def setup_cmd(self, ctx: commands.Context) -> None:
        """Guided invite tracking setup (Manage Server)."""
        await self._run_setup(ctx)

    @commands.command(name="isetup")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    async def isetup_command(self, ctx: commands.Context) -> None:
        """Quick guided invite-tracking setup (same as `invites setup`)."""
        await self._run_setup(ctx)

    # ------------------------------------------------------------------
    # Checks & error handling
    # ------------------------------------------------------------------
    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        err = _unwrap_error(error)
        ephemeral = ctx.interaction is not None
        if isinstance(err, commands.NoPrivateMessage):
            await ctx.send(
                embed=bk.error("Guild only", "Invite commands only work inside a server."),
                ephemeral=ephemeral,
            )
        elif isinstance(err, commands.MissingPermissions):
            missing = ", ".join(err.missing_permissions) if err.missing_permissions else "permissions"
            await ctx.send(
                embed=bk.error("Missing permissions", f"You need **{missing}** to use this command."),
                ephemeral=ephemeral,
            )
        elif isinstance(err, commands.MemberNotFound):
            await ctx.send(
                embed=bk.error("Member not found", f"Couldn't find member `{str(err)[:200]}`."),
                ephemeral=ephemeral,
            )
        elif isinstance(err, commands.BadArgument):
            await ctx.send(
                embed=bk.error("Bad argument", str(err)[:1500] or "One of the arguments was invalid."),
                ephemeral=ephemeral,
            )
        elif isinstance(err, commands.CheckFailure):
            await ctx.send(
                embed=bk.error("Not allowed", "You can't use this command here."),
                ephemeral=ephemeral,
            )
        else:
            log.exception("Unhandled error in invites command %s", ctx.command, exc_info=error)
            await ctx.send(
                embed=bk.error(
                    "Command error",
                    "Something went wrong running that command — it has been logged.",
                ),
                ephemeral=ephemeral,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InviteTrackingCog(bot))
