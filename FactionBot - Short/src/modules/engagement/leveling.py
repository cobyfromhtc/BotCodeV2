# -*- coding: utf-8 -*-
"""leveling.py — XP / leveling system for FactionBot.

Reuses the legacy ``levels`` table (column order ``user_id`` first for
compatibility with rows written by the old system) and adds a per-guild
``level_config`` table plus a ``level_roles`` reward table.

XP is earned from regular chat messages with per-user cooldowns and duplicate
content anti-farm protection. Level ups are announced (channel / custom
channel / DM / off) and can hand out role rewards.

Public API for other cogs (e.g. automod XP penalties):
    * ``await cog.add_xp(guild, member, amount) -> int`` (new level)
    * ``await cog.remove_xp(guild, member, amount) -> int``
    * ``cog.get_stats(guild_id, user_id) -> Optional[dict]``

All persistence goes through :mod:`utils.botkit`. This module never imports
from src/bot.py.
"""
from __future__ import annotations

import hashlib
import logging
import random
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils import botkit as bk  # relocated into utils/ by the SaaS restructure

log = logging.getLogger(__name__)

__all__ = ("LevelingCog", "setup", "xp_for_level", "compute_level")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_LEVEL = 10000

#: same-content messages inside this window give no XP (anti-farm)
DUPLICATE_WINDOW_SECONDS = 300

DEFAULT_LEVEL_UP_MESSAGE = "🎉 {user} reached level {level}!"

_ANNOUNCE_MODES = ("channel", "custom", "dm", "off")

_LEVEL_CONFIG_DEFAULTS: Dict[str, Any] = {
    "xp_min": 5,
    "xp_max": 15,
    "cooldown_seconds": 60,
    "min_length": 10,
    "announce_mode": "channel",
    "level_up_channel_id": 0,
    "level_up_message": DEFAULT_LEVEL_UP_MESSAGE,
    "no_xp_channels": "",
    "no_xp_roles": "",
}

_CONFIG_ALIASES: Dict[str, str] = {
    "xp_min": "xp_min",
    "min_xp": "xp_min",
    "xp_max": "xp_max",
    "max_xp": "xp_max",
    "cooldown": "cooldown_seconds",
    "cooldown_seconds": "cooldown_seconds",
    "min_length": "min_length",
    "announce": "announce_mode",
    "announce_mode": "announce_mode",
    "level_up_channel": "level_up_channel_id",
    "levelup_channel": "level_up_channel_id",
    "level_up_message": "level_up_message",
    "message": "level_up_message",
    "no_xp_channels": "no_xp_channels",
    "no_xp_roles": "no_xp_roles",
}

# The legacy `levels` table is reused as-is (user_id first for compat with
# legacy rows). The CREATE is idempotent insurance for fresh databases so this
# cog never depends on the old DataManager having run first.
_TABLES: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS levels(
        user_id        INTEGER NOT NULL,
        guild_id       INTEGER NOT NULL,
        xp             INTEGER NOT NULL DEFAULT 0,
        level          INTEGER NOT NULL DEFAULT 0,
        total_messages INTEGER NOT NULL DEFAULT 0,
        last_xp_gain   TEXT,
        PRIMARY KEY(user_id, guild_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS level_config(
        guild_id            INTEGER PRIMARY KEY,
        xp_min              INTEGER NOT NULL DEFAULT 5,
        xp_max              INTEGER NOT NULL DEFAULT 15,
        cooldown_seconds    INTEGER NOT NULL DEFAULT 60,
        min_length          INTEGER NOT NULL DEFAULT 10,
        announce_mode       TEXT    NOT NULL DEFAULT 'channel',
        level_up_channel_id INTEGER NOT NULL DEFAULT 0,
        level_up_message    TEXT    NOT NULL DEFAULT '🎉 {user} reached level {level}!',
        no_xp_channels      TEXT,
        no_xp_roles         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS level_roles(
        guild_id INTEGER NOT NULL,
        level    INTEGER NOT NULL,
        role_id  INTEGER NOT NULL,
        PRIMARY KEY(guild_id, level)
    )
    """,
]


# ---------------------------------------------------------------------------
# XP curve (module level so other systems can use them)
# ---------------------------------------------------------------------------
def xp_for_level(level: int) -> int:
    """Total XP required to reach ``level`` (MEE6-style curve)."""
    return int(5 / 6 * level * (2 * level * level + 27 * level + 91))


def compute_level(xp: int) -> int:
    """Level that ``xp`` corresponds to (capped at MAX_LEVEL)."""
    level = 0
    while level < MAX_LEVEL and xp >= xp_for_level(level + 1):
        level += 1
    return level


def _progress_bar(current: int, span: int, length: int = 20) -> str:
    if span <= 0:
        return "█" * length
    fraction = min(1.0, max(0.0, current / span))
    filled = int(round(fraction * length))
    return "█" * filled + "░" * (length - filled)


def _parse_bool(raw: str) -> Optional[bool]:
    r = raw.strip().lower()
    if r in ("on", "true", "1", "yes", "y", "enable", "enabled"):
        return True
    if r in ("off", "false", "0", "no", "n", "disable", "disabled"):
        return False
    return None


def _to_int(raw: str) -> Optional[int]:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
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
# Leaderboard pagination view
# ---------------------------------------------------------------------------
class _LeaderboardView(discord.ui.View):
    """⬅ / ➡ pagination for the XP leaderboard (120s timeout)."""

    def __init__(
        self, cog: "LevelingCog", ctx: commands.Context, page: int, total_pages: int,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = ctx.guild
        self.user_id = ctx.author.id
        self.page = page
        self.total_pages = total_pages
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.total_pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who ran the leaderboard can page through it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(emoji="⬅", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(1, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(emoji="➡", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.total_pages, self.page + 1)
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._update_buttons()
        embed = self.cog._leaderboard_embed(self.guild, self.page, self.total_pages)
        await interaction.response.edit_message(embed=embed, view=self)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class LevelingCog(commands.Cog, name="Leveling"):
    """XP gain from chat, level-up announcements, role rewards and leaderboards."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # (guild_id, user_id) → (timestamp of last gain, md5 of that message)
        self._last_gain: Dict[Tuple[int, int], Tuple[float, str]] = {}
        bk.create_tables(_TABLES)
        log.info("LevelingCog loaded")

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    def get_config(self, guild_id: int) -> Dict[str, Any]:
        """Per-guild leveling config merged over the defaults."""
        cfg = dict(_LEVEL_CONFIG_DEFAULTS)
        row = bk.fetchone(
            "SELECT xp_min, xp_max, cooldown_seconds, min_length, announce_mode, "
            "level_up_channel_id, level_up_message, no_xp_channels, no_xp_roles "
            "FROM level_config WHERE guild_id=?",
            (guild_id,),
        )
        if row is not None:
            for key in cfg:
                if row[key] is not None:
                    cfg[key] = row[key]
        return cfg

    def _set_config(self, guild_id: int, **values: Any) -> None:
        keys = [k for k in values if k in _LEVEL_CONFIG_DEFAULTS]
        if not keys:
            return
        row = bk.fetchone("SELECT guild_id FROM level_config WHERE guild_id=?", (guild_id,))
        if row is None:
            base = dict(_LEVEL_CONFIG_DEFAULTS)
            base.update({k: values[k] for k in keys})
            bk.run(
                "INSERT INTO level_config(guild_id, xp_min, xp_max, cooldown_seconds, "
                "min_length, announce_mode, level_up_channel_id, level_up_message, "
                "no_xp_channels, no_xp_roles) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    guild_id,
                    base["xp_min"], base["xp_max"], base["cooldown_seconds"],
                    base["min_length"], base["announce_mode"], base["level_up_channel_id"],
                    base["level_up_message"], base["no_xp_channels"], base["no_xp_roles"],
                ),
            )
        else:
            sets = ", ".join(f"{k}=?" for k in keys)
            args: List[Any] = [values[k] for k in keys]
            args.append(guild_id)
            bk.run(f"UPDATE level_config SET {sets} WHERE guild_id=?", tuple(args))

    @staticmethod
    def _no_xp_ids(cfg: Dict[str, Any], key: str) -> List[int]:
        data = bk.jload(cfg.get(key), [])
        if not isinstance(data, list):
            return []
        return [int(x) for x in data if isinstance(x, int)]

    # ------------------------------------------------------------------
    # Core XP engine
    # ------------------------------------------------------------------
    def _leveling_enabled(self) -> bool:
        """Core toggle — XP gain only; rank/leaderboard stay viewable."""
        fb_config = getattr(self.bot, "fb_config", None)
        if fb_config is None:
            return True
        return bool(getattr(fb_config, "enable_leveling", True))

    def _command_prefix(self) -> str:
        prefix = getattr(self.bot, "command_prefix", "!")
        if callable(prefix) or not isinstance(prefix, str) or not prefix:
            return "!"
        return prefix

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:  # DMs
            return
        author = message.author
        if author.bot or message.webhook_id is not None:
            return
        member = author if isinstance(author, discord.Member) else None
        if member is None:
            return

        # --- FACTIONACCESS AUTOMATION SCOPE ---
        # Leveling data is keyed (user, guild), so XP follows the per-guild
        # 'leveling' bundle: the home faction and explicitly granted allied
        # factions earn XP; everyone else is skipped. A missing service
        # (pre-setup_hook) keeps the legacy behavior.
        faction = getattr(self.bot, 'faction_access', None)
        if faction is not None and not faction.automation_allowed(message.guild.id, 'leveling'):
            return

        content = message.content or ""
        prefix = self._command_prefix()
        if content.startswith(prefix):  # commands earn no XP
            return
        if not self._leveling_enabled():
            return

        cfg = self.get_config(message.guild.id)
        channel = message.channel
        channel_id = channel.id
        parent_id = getattr(channel, "parent_id", None)
        no_xp_channels = self._no_xp_ids(cfg, "no_xp_channels")
        if channel_id in no_xp_channels or (parent_id and parent_id in no_xp_channels):
            return
        no_xp_roles = set(self._no_xp_ids(cfg, "no_xp_roles"))
        if no_xp_roles and (bk.role_ids_for(member) & no_xp_roles):
            return
        if len(content.strip()) < int(cfg["min_length"]):
            return

        # anti-farm: per-user cooldown + duplicate content inside 5 minutes
        key = (message.guild.id, member.id)
        content_hash = hashlib.md5(content.strip().lower().encode("utf-8")).hexdigest()
        now = bk.now_ts()
        last = self._last_gain.get(key)
        if last is not None:
            ts, previous_hash = last
            if now - ts < float(cfg["cooldown_seconds"]):
                return
            if previous_hash == content_hash and now - ts < DUPLICATE_WINDOW_SECONDS:
                return
        self._last_gain[key] = (now, content_hash)

        lo, hi = int(cfg["xp_min"]), int(cfg["xp_max"])
        if lo > hi:
            lo, hi = hi, lo
        lo = max(0, lo)
        hi = max(lo, hi)
        gained = random.randint(lo, hi)
        try:
            await self._apply_gain(message.guild, member, gained, source_message=message)
        except Exception:  # noqa: BLE001 — never break message handling
            log.exception("Failed to apply XP gain for member %s", member.id)

    async def _apply_gain(
        self,
        guild: discord.Guild,
        member: discord.Member,
        amount: int,
        source_message: Optional[discord.Message] = None,
    ) -> int:
        """Apply an XP delta and persist it; returns the new level.

        The legacy ``levels`` table has PRIMARY KEY(user_id, guild_id) in the
        live schema, so SELECT-then-INSERT OR REPLACE is safe; the column
        order (user_id first) and total_messages accumulation are preserved
        for compatibility with rows written by the old system.
        """
        row = bk.fetchone(
            "SELECT xp, level, total_messages FROM levels WHERE user_id=? AND guild_id=?",
            (member.id, guild.id),
        )
        old_xp = int(row["xp"] or 0) if row is not None else 0
        old_level = int(row["level"] or 0) if row is not None else 0
        old_messages = int(row["total_messages"] or 0) if row is not None else 0

        new_xp = max(0, old_xp + int(amount))
        new_level = compute_level(new_xp)
        new_messages = old_messages + (1 if source_message is not None else 0)

        bk.run(
            "INSERT OR REPLACE INTO levels(user_id, guild_id, xp, level, "
            "total_messages, last_xp_gain) VALUES(?,?,?,?,?,?)",
            (member.id, guild.id, new_xp, new_level, new_messages, bk.now_iso()),
        )

        if new_level > old_level and amount > 0:
            try:
                await self._handle_level_up(guild, member, new_level, source_message)
            except Exception:  # noqa: BLE001 — level-up side effects must not fail XP gain
                log.exception("Level-up handling failed for member %s", member.id)
        return new_level

    async def _handle_level_up(
        self,
        guild: discord.Guild,
        member: discord.Member,
        new_level: int,
        source_message: Optional[discord.Message] = None,
    ) -> None:
        cfg = self.get_config(guild.id)
        await self._assign_level_roles(guild, member, new_level)

        mode = str(cfg.get("announce_mode") or "channel").strip().lower()
        if mode == "off":
            return
        template = cfg.get("level_up_message") or DEFAULT_LEVEL_UP_MESSAGE
        try:
            text = template.format(user=member.mention, level=new_level, server=guild.name)
        except (KeyError, IndexError, ValueError):
            text = DEFAULT_LEVEL_UP_MESSAGE.format(user=member.mention, level=new_level, server=guild.name)
        embed = bk.success("Level Up", text)
        embed.set_thumbnail(url=member.display_avatar.url)

        if mode == "dm":
            try:
                await member.send(embed=embed)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                log.warning("Could not DM level-up to member %s: %s", member.id, exc)
            return

        channel: Optional[discord.abc.Messageable] = None
        if mode == "channel":
            channel = source_message.channel if source_message is not None else guild.system_channel
        elif mode == "custom":
            custom_id = int(cfg.get("level_up_channel_id") or 0)
            channel = guild.get_channel_or_thread(custom_id) if custom_id else None
            if channel is None:  # fall back so the level up is not silently lost
                channel = source_message.channel if source_message is not None else guild.system_channel
        if channel is None:
            return
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            log.warning("Could not send level-up message in guild %s: %s", guild.id, exc)

    async def _assign_level_roles(
        self, guild: discord.Guild, member: discord.Member, new_level: int
    ) -> None:
        """Grant every reward role whose level threshold is now met."""
        rows = bk.fetchall(
            "SELECT level, role_id FROM level_roles WHERE guild_id=? AND level<=? ORDER BY level ASC",
            (guild.id, new_level),
        )
        for row in rows:
            role = guild.get_role(int(row["role_id"] or 0))
            if role is None:
                continue
            if role in member.roles:
                continue
            if role.managed or not bk.bot_can_manage_role(guild, role):
                log.warning(
                    "Cannot assign level role %s (id %s) in guild %s — hierarchy/permissions",
                    role.name, role.id, guild.id,
                )
                continue
            try:
                await member.add_roles(
                    role, reason=f"FactionBot level reward (level {row['level']})"
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                log.warning("Could not assign level role %s to %s: %s", role.id, member.id, exc)

    # ------------------------------------------------------------------
    # Public API for other cogs (automod XP penalties, etc.)
    # ------------------------------------------------------------------
    async def add_xp(self, guild: discord.Guild, member: discord.Member, amount: int) -> int:
        """Add XP to a member; returns the new level (handles level-up effects)."""
        return await self._apply_gain(guild, member, int(amount))

    async def remove_xp(self, guild: discord.Guild, member: discord.Member, amount: int) -> int:
        """Remove XP from a member (floored at 0); returns the new level."""
        return await self._apply_gain(guild, member, -int(amount))

    def get_stats(self, guild_id: int, user_id: int) -> Optional[dict]:
        """Raw ``levels`` row for a member (or None when they have no XP)."""
        row = bk.fetchone(
            "SELECT user_id, guild_id, xp, level, total_messages, last_xp_gain "
            "FROM levels WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        )
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Rank card / leaderboard
    # ------------------------------------------------------------------
    async def _send_rank_card(self, ctx: commands.Context, target: discord.abc.User) -> None:
        row = bk.fetchone(
            "SELECT xp, level, total_messages FROM levels WHERE user_id=? AND guild_id=?",
            (target.id, ctx.guild.id),
        )
        if row is None:
            await ctx.send(
                embed=bk.info("No XP yet", f"{target.mention} hasn't earned any XP in this server yet.")
            )
            return
        xp = int(row["xp"] or 0)
        level = int(row["level"] or 0)
        messages = int(row["total_messages"] or 0)

        # self-heal legacy rows whose stored level disagrees with the new curve
        computed = compute_level(xp)
        if computed != level:
            level = computed
            bk.run(
                "UPDATE levels SET level=? WHERE user_id=? AND guild_id=?",
                (level, target.id, ctx.guild.id),
            )

        pos_row = bk.fetchone(
            "SELECT COUNT(*) AS n FROM levels WHERE guild_id=? AND xp > ?",
            (ctx.guild.id, xp),
        )
        position = (int(pos_row["n"]) + 1) if pos_row is not None else 1

        base = xp_for_level(level)
        next_total = xp_for_level(level + 1)
        span = max(1, next_total - base)
        into = max(0, xp - base)
        if level >= MAX_LEVEL:
            progress_desc = "```MAX LEVEL REACHED```"
        else:
            progress_desc = f"`{_progress_bar(into, span)}` {int(100 * into / span)}% — {into}/{span} XP to level {level + 1}"

        embed = bk.info(f"{target.display_name} — Level {level}", progress_desc)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="💬 Messages", value=str(messages), inline=True)
        embed.add_field(name="⚡ XP", value=str(xp), inline=True)
        embed.add_field(name="🏆 Server rank", value=f"#{position}", inline=True)
        await ctx.send(embed=embed)

    def _leaderboard_embed(
        self, guild: discord.Guild, page: int, total_pages: int
    ) -> discord.Embed:
        offset = (page - 1) * 10
        rows = bk.fetchall(
            "SELECT user_id, xp, level FROM levels WHERE guild_id=? "
            "ORDER BY xp DESC, user_id ASC LIMIT 10 OFFSET ?",
            (guild.id, offset),
        )
        medals = ("🥇", "🥈", "🥉")
        lines: List[str] = []
        for i, row in enumerate(rows):
            rank = offset + i + 1
            tag = medals[rank - 1] if rank <= 3 else f"**{rank}.**"
            lines.append(f"{tag} <@{row['user_id']}> — level **{row['level']}** · `{row['xp']}` XP")
        embed = bk.info(
            "XP Leaderboard",
            "\n".join(lines) if lines else "Nothing on this page.",
        )
        embed.set_footer(text=f"Page {page}/{total_pages} · top 10 per page")
        return embed

    # ------------------------------------------------------------------
    # Commands — rank / leaderboard (open to everyone)
    # ------------------------------------------------------------------
    @commands.command(name="rank", aliases=["lvl", "xprank"])
    @commands.guild_only()
    @app_commands.describe(member="Member to check (defaults to you)")
    async def rank_command(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Show a rank card with level, XP and progress."""
        target = member if member is not None else ctx.author
        await self._send_rank_card(ctx, target)

    @commands.command(name="leaderboard", aliases=["lb"])
    @commands.guild_only()
    @app_commands.describe(page="Page number (10 members per page, default 1)")
    async def leaderboard_command(
        self, ctx: commands.Context, page: app_commands.Range[int, 1, 1000] = 1
    ) -> None:
        """Show the server XP leaderboard (top 10 per page)."""
        total_row = bk.fetchone(
            "SELECT COUNT(*) AS n FROM levels WHERE guild_id=?", (ctx.guild.id,)
        )
        total = int(total_row["n"]) if total_row is not None else 0
        if total == 0:
            await ctx.send(embed=bk.info("XP Leaderboard", "Nobody has earned XP in this server yet."))
            return
        total_pages = max(1, (total + 9) // 10)
        current_page = min(int(page), total_pages)
        view = _LeaderboardView(self, ctx, current_page, total_pages)
        embed = self._leaderboard_embed(ctx.guild, current_page, total_pages)
        await ctx.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------
    # Commands — `level` group (admin)
    # ------------------------------------------------------------------
    @commands.group(name="level", invoke_without_command=True)
    @commands.guild_only()
    @app_commands.describe(member="Member whose rank card to show (defaults to you)")
    async def level_command(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Show a rank card — or manage the leveling system with the subcommands below."""
        target = member if member is not None else ctx.author
        await self._send_rank_card(ctx, target)

    @level_command.command(name="config")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        key="Setting to view/change: xp_min, xp_max, cooldown_seconds, min_length, "
            "announce_mode, level_up_channel, level_up_message, no_xp_channels, no_xp_roles",
        value="New value for the setting (quote multi-word values)",
    )
    async def config_cmd(
        self, ctx: commands.Context, key: Optional[str] = None, value: Optional[str] = None
    ) -> None:
        """View or change the leveling configuration (Manage Server)."""
        cfg = self.get_config(ctx.guild.id)

        def _config_embed() -> discord.Embed:
            custom = (
                f"<#{cfg['level_up_channel_id']}>" if cfg.get("level_up_channel_id") else "*(same channel)*"
            )
            channels = self._no_xp_ids(cfg, "no_xp_channels")
            roles = self._no_xp_ids(cfg, "no_xp_roles")
            channel_text = " ".join(f"<#{c}>" for c in channels) if channels else "*(none)*"
            role_text = " ".join(f"<@&{r}>" for r in roles) if roles else "*(none)*"
            embed = bk.info("Leveling Configuration")
            embed.add_field(
                name="XP per message",
                value=f"{cfg['xp_min']} – {cfg['xp_max']}", inline=True,
            )
            embed.add_field(
                name="Cooldown", value=f"{cfg['cooldown_seconds']}s", inline=True,
            )
            embed.add_field(name="Min length", value=str(cfg["min_length"]), inline=True)
            embed.add_field(name="Announce mode", value=str(cfg["announce_mode"]), inline=True)
            embed.add_field(name="Level-up channel", value=custom, inline=True)
            embed.add_field(name="Level-up message", value=str(cfg["level_up_message"])[:1024], inline=False)
            embed.add_field(name="No-XP channels", value=channel_text, inline=False)
            embed.add_field(name="No-XP roles", value=role_text, inline=False)
            embed.set_footer(text=(
                f"Change with {ctx.clean_prefix}level config <key> <value> — quote values containing spaces"
            ))
            return embed

        if key is None:
            await ctx.send(embed=_config_embed())
            return
        canonical = _CONFIG_ALIASES.get(key.strip().lower())
        if canonical is None:
            await ctx.send(
                embed=bk.error(
                    "Unknown setting",
                    "Valid keys: `xp_min`, `xp_max`, `cooldown_seconds`, `min_length`, "
                    "`announce_mode`, `level_up_channel`, `level_up_message`, "
                    "`no_xp_channels`, `no_xp_roles`.",
                )
            )
            return
        if value is None:
            await ctx.send(embed=bk.info("Leveling config", f"`{canonical}` is currently **{cfg.get(canonical)}**."))
            return

        raw = value.strip()
        if canonical in ("xp_min", "xp_max"):
            number = _to_int(raw)
            if number is None:
                await ctx.send(embed=bk.error("Invalid number", f"`{raw[:50]}` is not a whole number."))
                return
            other = int(cfg["xp_max"] if canonical == "xp_min" else cfg["xp_min"])
            if canonical == "xp_min" and not (1 <= number <= 100):
                await ctx.send(embed=bk.error("Out of range", "`xp_min` must be between 1 and 100."))
                return
            if canonical == "xp_max" and not (1 <= number <= 500):
                await ctx.send(embed=bk.error("Out of range", "`xp_max` must be between 1 and 500."))
                return
            if (canonical == "xp_min" and number > other) or (canonical == "xp_max" and number < other):
                await ctx.send(
                    embed=bk.error("Invalid range", "`xp_min` must be less than or equal to `xp_max`.")
                )
                return
            self._set_config(ctx.guild.id, **{canonical: number})
            await ctx.send(
                embed=bk.success("Setting saved", f"`{canonical}` is now **{number}**.")
            )
        elif canonical == "cooldown_seconds":
            number = _to_int(raw)
            if number is None or not (0 <= number <= 86400):
                await ctx.send(embed=bk.error("Out of range", "`cooldown_seconds` must be between 0 and 86400."))
                return
            self._set_config(ctx.guild.id, cooldown_seconds=number)
            await ctx.send(embed=bk.success("Setting saved", f"`cooldown_seconds` is now **{number}**."))
        elif canonical == "min_length":
            number = _to_int(raw)
            if number is None or not (0 <= number <= 2000):
                await ctx.send(embed=bk.error("Out of range", "`min_length` must be between 0 and 2000."))
                return
            self._set_config(ctx.guild.id, min_length=number)
            await ctx.send(embed=bk.success("Setting saved", f"`min_length` is now **{number}**."))
        elif canonical == "announce_mode":
            mode = raw.lower()
            if mode not in _ANNOUNCE_MODES:
                await ctx.send(
                    embed=bk.error(
                        "Invalid mode",
                        "Announce mode must be one of: `channel`, `custom`, `dm`, `off`.",
                    )
                )
                return
            self._set_config(ctx.guild.id, announce_mode=mode)
            await ctx.send(embed=bk.success("Setting saved", f"`announce_mode` is now **{mode}**."))
        elif canonical == "level_up_channel_id":
            if raw.lower() in ("off", "none", "same", "channel", "reset", "0"):
                self._set_config(ctx.guild.id, level_up_channel_id=0)
                await ctx.send(
                    embed=bk.success("Setting saved", "Level-ups will be announced in the channel they happen in.")
                )
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
            self._set_config(ctx.guild.id, level_up_channel_id=channel.id)
            await ctx.send(
                embed=bk.success("Setting saved", f"Level-ups will be announced in {channel.mention} "
                                                  "(with `announce_mode custom`).")
            )
        elif canonical == "level_up_message":
            if raw.lower() in ("default", "reset", "none"):
                self._set_config(ctx.guild.id, level_up_message=DEFAULT_LEVEL_UP_MESSAGE)
                await ctx.send(
                    embed=bk.success("Setting saved", f"Level-up message reset to:\n{DEFAULT_LEVEL_UP_MESSAGE}")
                )
                return
            if len(raw) > 500:
                await ctx.send(embed=bk.error("Message too long", "Keep the level-up message under 500 characters."))
                return
            self._set_config(ctx.guild.id, level_up_message=raw)
            preview = raw.replace("{user}", ctx.author.mention).replace(
                "{level}", "42").replace("{server}", ctx.guild.name)
            await ctx.send(embed=bk.success("Setting saved", f"Preview:\n{preview[:1500]}"))
        elif canonical == "no_xp_channels":
            ids, bad = await self._resolve_channel_ids(ctx, raw)
            if bad:
                await ctx.send(
                    embed=bk.error(
                        "Channel not found",
                        "I couldn't resolve: " + ", ".join(f"`{b[:50]}`" for b in bad),
                    )
                )
                return
            self._set_config(ctx.guild.id, no_xp_channels=bk.jdump(sorted(set(ids))))
            mention_line = " ".join(f"<#{i}>" for i in sorted(set(ids))) or "*(none)*"
            await ctx.send(embed=bk.success("Setting saved", f"No-XP channels: {mention_line}"))
        elif canonical == "no_xp_roles":
            ids, bad = await self._resolve_role_ids(ctx, raw)
            if bad:
                await ctx.send(
                    embed=bk.error(
                        "Role not found",
                        "I couldn't resolve: " + ", ".join(f"`{b[:50]}`" for b in bad),
                    )
                )
                return
            self._set_config(ctx.guild.id, no_xp_roles=bk.jdump(sorted(set(ids))))
            mention_line = " ".join(f"<@&{i}>" for i in sorted(set(ids))) or "*(none)*"
            await ctx.send(embed=bk.success("Setting saved", f"No-XP roles: {mention_line}"))

    async def _resolve_channel_ids(
        self, ctx: commands.Context, raw: str
    ) -> Tuple[List[int], List[str]]:
        ids: List[int] = []
        bad: List[str] = []
        for token in [t.strip() for t in raw.split(",") if t.strip()]:
            channel: Optional[discord.abc.GuildChannel] = None
            try:
                channel = await commands.TextChannelConverter().convert(ctx, token)
            except commands.ChannelNotFound:
                if token.strip("<#>").isdigit():
                    channel = ctx.guild.get_channel_or_thread(int(token.strip("<#>")))
            if channel is not None:
                ids.append(channel.id)
            else:
                bad.append(token)
        return ids, bad

    async def _resolve_role_ids(
        self, ctx: commands.Context, raw: str
    ) -> Tuple[List[int], List[str]]:
        ids: List[int] = []
        bad: List[str] = []
        for token in [t.strip() for t in raw.split(",") if t.strip()]:
            role: Optional[discord.Role] = None
            try:
                role = await commands.RoleConverter().convert(ctx, token)
            except commands.RoleNotFound:
                if token.strip("<@&>").isdigit():
                    role = ctx.guild.get_role(int(token.strip("<@&>")))
            if role is not None:
                ids.append(role.id)
            else:
                bad.append(token)
        return ids, bad

    @level_command.command(name="give")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="Member to give XP to", amount="Amount of XP (1-1000000)")
    async def give_cmd(
        self, ctx: commands.Context, member: discord.Member,
        amount: app_commands.Range[int, 1, 1000000],
    ) -> None:
        """Give XP to a member (Manage Server)."""
        new_level = await self.add_xp(ctx.guild, member, int(amount))
        await ctx.send(
            embed=bk.success(
                "XP given",
                f"Added **{amount}** XP to {member.mention} — they are now **level {new_level}**.",
            )
        )

    @level_command.command(name="remove")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="Member to remove XP from", amount="Amount of XP (1-1000000)")
    async def remove_cmd(
        self, ctx: commands.Context, member: discord.Member,
        amount: app_commands.Range[int, 1, 1000000],
    ) -> None:
        """Remove XP from a member (floored at 0) (Manage Server)."""
        new_level = await self.remove_xp(ctx.guild, member, int(amount))
        await ctx.send(
            embed=bk.success(
                "XP removed",
                f"Removed **{amount}** XP from {member.mention} — they are now **level {new_level}**.",
            )
        )

    @level_command.command(name="set")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="Member to set XP for", xp="New total XP amount")
    async def set_cmd(
        self, ctx: commands.Context, member: discord.Member,
        xp: app_commands.Range[int, 0, 100000000],
    ) -> None:
        """Set a member's total XP (level is recomputed) (Manage Server)."""
        row = bk.fetchone(
            "SELECT xp FROM levels WHERE user_id=? AND guild_id=?", (member.id, ctx.guild.id)
        )
        current = int(row["xp"] or 0) if row is not None else 0
        new_level = await self._apply_gain(ctx.guild, member, int(xp) - current)
        await ctx.send(
            embed=bk.success(
                "XP set",
                f"{member.mention} now has **{xp}** XP — they are **level {new_level}**.",
            )
        )

    @level_command.command(name="reset")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(target="A member (ID / mention / name) or 'all' to reset the server")
    async def reset_cmd(self, ctx: commands.Context, target: str) -> None:
        """Reset XP for a member or the whole server (Manage Server)."""
        raw = target.strip()
        if raw.lower() in ("all", "*", "everyone", "server"):
            count_row = bk.fetchone(
                "SELECT COUNT(*) AS n FROM levels WHERE guild_id=?", (ctx.guild.id,)
            )
            total = int(count_row["n"]) if count_row is not None else 0
            if total == 0:
                await ctx.send(embed=bk.info("Nothing to reset", "No XP records exist for this server."))
                return
            bk.run("DELETE FROM levels WHERE guild_id=?", (ctx.guild.id,))
            await ctx.send(
                embed=bk.success("XP reset", f"Reset XP for **{total}** member(s) in this server.")
            )
            return
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
        bk.run(
            "DELETE FROM levels WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id)
        )
        await ctx.send(embed=bk.success("XP reset", f"{member.mention}'s XP and level were reset."))

    @level_command.command(name="rewards")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    async def rewards_cmd(self, ctx: commands.Context) -> None:
        """List the configured level role rewards (Manage Server)."""
        rows = bk.fetchall(
            "SELECT level, role_id FROM level_roles WHERE guild_id=? ORDER BY level ASC",
            (ctx.guild.id,),
        )
        if not rows:
            await ctx.send(
                embed=bk.info(
                    "Level Rewards",
                    f"No level rewards configured yet — add one with "
                    f"`{ctx.clean_prefix}level reward add <level> <role>`.",
                )
            )
            return
        lines = [f"Level **{row['level']}** → <@&{row['role_id']}>" for row in rows]
        embed = bk.info("Level Rewards", "\n".join(lines)[:4000])
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @level_command.group(name="reward")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    async def reward_group(self, ctx: commands.Context) -> None:
        """Manage level-up role rewards — use `reward add` / `reward remove`."""
        await ctx.send(
            embed=bk.info(
                "Level Rewards",
                f"`{ctx.clean_prefix}level reward add <level> <role>` — award a role at a level\n"
                f"`{ctx.clean_prefix}level reward remove <level>` — remove the reward for a level\n"
                f"`{ctx.clean_prefix}level rewards` — list all rewards",
            )
        )

    @reward_group.command(name="add")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(level="Level that grants the role", role="Role to award")
    async def reward_add_cmd(
        self, ctx: commands.Context,
        level: app_commands.Range[int, 1, MAX_LEVEL], role: discord.Role,
    ) -> None:
        """Award a role when members reach a level (Manage Server)."""
        if role.managed or not bk.bot_can_manage_role(ctx.guild, role):
            await ctx.send(
                embed=bk.error(
                    "Role not assignable",
                    f"I can't hand out {role.mention} — it must be **below my top role**, "
                    "not be a managed/integration role, and I need the **Manage Roles** permission.",
                )
            )
            return
        if role.id == ctx.guild.id:
            await ctx.send(embed=bk.error("Invalid role", "That's the @everyone role, not a reward."))
            return
        bk.run(
            "INSERT OR REPLACE INTO level_roles(guild_id, level, role_id) VALUES(?,?,?)",
            (ctx.guild.id, int(level), role.id),
        )
        await ctx.send(
            embed=bk.success(
                "Level reward added",
                f"Members will receive {role.mention} when they reach **level {level}**.",
            )
        )

    @reward_group.command(name="remove")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(level="Level whose reward should be removed")
    async def reward_remove_cmd(
        self, ctx: commands.Context, level: app_commands.Range[int, 1, MAX_LEVEL]
    ) -> None:
        """Remove the level reward for a level (Manage Server)."""
        row = bk.fetchone(
            "SELECT role_id FROM level_roles WHERE guild_id=? AND level=?",
            (ctx.guild.id, int(level)),
        )
        if row is None:
            await ctx.send(embed=bk.error("No reward", f"There is no reward configured for level {level}."))
            return
        bk.run(
            "DELETE FROM level_roles WHERE guild_id=? AND level=?", (ctx.guild.id, int(level))
        )
        await ctx.send(
            embed=bk.success(
                "Level reward removed",
                f"The reward for **level {level}** (<@&{row['role_id']}>) was removed.\n"
                "Members who already have the role keep it.",
            )
        )

    @level_command.group(name="ignore")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    async def ignore_group(self, ctx: commands.Context) -> None:
        """Stop XP gain in channels/roles — use `ignore channel` / `ignore role`."""
        await ctx.send(
            embed=bk.info(
                "No-XP Lists",
                f"`{ctx.clean_prefix}level ignore channel <channel>` — no XP in a channel\n"
                f"`{ctx.clean_prefix}level ignore role <role>` — no XP for a role\n"
                f"`{ctx.clean_prefix}level unignore channel <channel>` / `unignore role <role>` — undo",
            )
        )

    @ignore_group.command(name="channel")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel where messages give no XP")
    async def ignore_channel_cmd(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Disable XP gain in a channel (Manage Server)."""
        cfg = self.get_config(ctx.guild.id)
        ids = set(self._no_xp_ids(cfg, "no_xp_channels"))
        if channel.id in ids:
            await ctx.send(
                embed=bk.warning("Already ignored", f"{channel.mention} already gives no XP.")
            )
            return
        ids.add(channel.id)
        self._set_config(ctx.guild.id, no_xp_channels=bk.jdump(sorted(ids)))
        await ctx.send(embed=bk.success("Channel ignored", f"Messages in {channel.mention} no longer give XP."))

    @ignore_group.command(name="role")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(role="Role whose members earn no XP")
    async def ignore_role_cmd(self, ctx: commands.Context, role: discord.Role) -> None:
        """Disable XP gain for members with a role (Manage Server)."""
        if role.id == ctx.guild.id:
            await ctx.send(embed=bk.error("Invalid role", "You can't ignore @everyone — that would disable XP for all."))
            return
        cfg = self.get_config(ctx.guild.id)
        ids = set(self._no_xp_ids(cfg, "no_xp_roles"))
        if role.id in ids:
            await ctx.send(
                embed=bk.warning("Already ignored", f"{role.mention} members already earn no XP.")
            )
            return
        ids.add(role.id)
        self._set_config(ctx.guild.id, no_xp_roles=bk.jdump(sorted(ids)))
        await ctx.send(embed=bk.success("Role ignored", f"Members with {role.mention} no longer earn XP."))

    @level_command.group(name="unignore")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    async def unignore_group(self, ctx: commands.Context) -> None:
        """Re-enable XP gain — use `unignore channel` / `unignore role`."""
        await ctx.send(
            embed=bk.info(
                "No-XP Lists",
                f"`{ctx.clean_prefix}level unignore channel <channel>` — re-enable a channel\n"
                f"`{ctx.clean_prefix}level unignore role <role>` — re-enable a role",
            )
        )

    @unignore_group.command(name="channel")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel to re-enable XP in")
    async def unignore_channel_cmd(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Re-enable XP gain in a channel (Manage Server)."""
        cfg = self.get_config(ctx.guild.id)
        ids = set(self._no_xp_ids(cfg, "no_xp_channels"))
        if channel.id not in ids:
            await ctx.send(embed=bk.warning("Not ignored", f"{channel.mention} isn't on the no-XP list."))
            return
        ids.discard(channel.id)
        self._set_config(ctx.guild.id, no_xp_channels=bk.jdump(sorted(ids)))
        await ctx.send(embed=bk.success("Channel re-enabled", f"Messages in {channel.mention} give XP again."))

    @unignore_group.command(name="role")
    @commands.guild_only()
    @_require_manage_guild()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(role="Role to re-enable XP for")
    async def unignore_role_cmd(self, ctx: commands.Context, role: discord.Role) -> None:
        """Re-enable XP gain for a role (Manage Server)."""
        cfg = self.get_config(ctx.guild.id)
        ids = set(self._no_xp_ids(cfg, "no_xp_roles"))
        if role.id not in ids:
            await ctx.send(embed=bk.warning("Not ignored", f"{role.mention} isn't on the no-XP list."))
            return
        ids.discard(role.id)
        self._set_config(ctx.guild.id, no_xp_roles=bk.jdump(sorted(ids)))
        await ctx.send(embed=bk.success("Role re-enabled", f"Members with {role.mention} earn XP again."))

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
                embed=bk.error("Guild only", "Leveling commands only work inside a server."),
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
        elif isinstance(err, commands.RoleNotFound):
            await ctx.send(
                embed=bk.error("Role not found", f"Couldn't find role `{str(err)[:200]}`."),
                ephemeral=ephemeral,
            )
        elif isinstance(err, commands.ChannelNotFound):
            await ctx.send(
                embed=bk.error("Channel not found", f"Couldn't find channel `{str(err)[:200]}`."),
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
            log.exception("Unhandled error in leveling command %s", ctx.command, exc_info=error)
            await ctx.send(
                embed=bk.error(
                    "Command error",
                    "Something went wrong running that command — it has been logged.",
                ),
                ephemeral=ephemeral,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LevelingCog(bot))
