# -*- coding: utf-8 -*-
"""Server verification system for FactionBot.

A button-based verification panel with lifecycle persistence:

* one persistent view with a STATIC custom_id covers every guild's panel
  (registered once in :meth:`VerificationCog.cog_load`);
* ``on_member_join`` hands out the unverified role and DMs a pointer;
* a 1-minute background loop enforces the unverified timeout (log or kick);
* everything is configured per guild through ``!verification`` / ``!v``
  (or ``!verification``) plus the ``!vsetup`` / ``!vsetup`` shortcut.

All persistence lives in the shared SQLite database via :mod:`utils.botkit`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks

from utils.botkit import (
    COLOR_BRAND,
    bot_can_manage_role,
    can_act_on,
    create_tables,
    error,
    fetchall,
    fetchone,
    fmt_dt,
    fmt_duration,
    info,
    is_exempt,
    now_iso,
    parse_iso,
    run,
    success,
    warning,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERIFY_BUTTON_CUSTOM_ID = "fb_verify_button"

DEFAULT_TITLE = "Verify Yourself"
DEFAULT_DESCRIPTION = "Click the button below to verify your account and unlock the server."
DEFAULT_BUTTON_LABEL = "✅ Verify"
DEFAULT_COLOR = "#9B59B6"
DEFAULT_DM_MESSAGE = "✅ You are now verified in **{server}** — welcome aboard, {user}!"

VERIFIED_ROLE_NAME = "Verified"
UNVERIFIED_ROLE_NAME = "Unverified"

MAX_TIMEOUT_MINUTES = 43200  # 30 days — sanity ceiling, not a Discord limit

_CFG_DEFAULTS: Dict[str, Any] = {
    "guild_id": 0,
    "enabled": 0,
    "mode": "button",
    "unverified_role_id": 0,
    "verified_role_id": 0,
    "log_channel_id": 0,
    "panel_channel_id": 0,
    "panel_message_id": 0,
    "title": "",
    "description": "",
    "button_label": DEFAULT_BUTTON_LABEL,
    "button_emoji": "",
    "color": DEFAULT_COLOR,
    "timeout_minutes": 0,
    "kick_on_timeout": 0,
    "account_age_days": 0,
    "dm_on_join": 1,
    "dm_message": "",
}

# `!v config <key> <value>` — key → verification_config column
_CFG_COLUMNS: Dict[str, str] = {
    "title": "title",
    "description": "description",
    "button_label": "button_label",
    "button_emoji": "button_emoji",
    "timeout_minutes": "timeout_minutes",
    "kick_on_timeout": "kick_on_timeout",
    "account_age_days": "account_age_days",
    "dm_on_join": "dm_on_join",
    "dm_message": "dm_message",
    "log_channel": "log_channel_id",
    "panel_channel": "panel_channel_id",
}
_CFG_BOOL_KEYS = frozenset({"kick_on_timeout", "dm_on_join"})
_CFG_INT_KEYS = frozenset({"timeout_minutes", "account_age_days"})
_CFG_CHANNEL_KEYS = frozenset({"log_channel", "panel_channel"})
_CFG_TEXT_KEYS = frozenset({"title", "description", "button_label", "button_emoji", "dm_message"})

_TABLES: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS verification_config (
        guild_id           INTEGER PRIMARY KEY,
        enabled            INTEGER DEFAULT 0,
        mode               TEXT    DEFAULT 'button',
        unverified_role_id INTEGER DEFAULT 0,
        verified_role_id   INTEGER DEFAULT 0,
        log_channel_id     INTEGER DEFAULT 0,
        panel_channel_id   INTEGER DEFAULT 0,
        panel_message_id   INTEGER DEFAULT 0,
        title              TEXT,
        description        TEXT,
        button_label       TEXT    DEFAULT '✅ Verify',
        button_emoji       TEXT,
        color              TEXT    DEFAULT '#9B59B6',
        timeout_minutes    INTEGER DEFAULT 0,
        kick_on_timeout    INTEGER DEFAULT 0,
        account_age_days   INTEGER DEFAULT 0,
        dm_on_join         INTEGER DEFAULT 1,
        dm_message         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verification_records (
        guild_id    INTEGER,
        user_id     INTEGER,
        verified_at TEXT,
        method      TEXT,
        PRIMARY KEY (guild_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verification_events (
        guild_id   INTEGER,
        user_id    INTEGER,
        event_type TEXT,
        detail     TEXT,
        created_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_verification_events_guild ON verification_events (guild_id, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_verification_records_guild ON verification_records (guild_id)",
)


def _parse_bool(value: str) -> Optional[bool]:
    lowered = value.strip().lower()
    if lowered in ("on", "true", "yes", "y", "1", "enable", "enabled"):
        return True
    if lowered in ("off", "false", "no", "n", "0", "disable", "disabled"):
        return False
    return None


# ---------------------------------------------------------------------------
# Persistent button view
# ---------------------------------------------------------------------------
class VerificationButtonView(discord.ui.View):
    """Persistent verification panel view.

    The button uses the STATIC custom id ``fb_verify_button`` and the view has
    ``timeout=None``, so a single registration via ``bot.add_view`` in
    ``cog_load`` dispatches presses from every guild's panel — including
    panels posted before the last restart.
    """

    def __init__(
        self,
        cog: "VerificationCog",
        label: str = DEFAULT_BUTTON_LABEL,
        emoji: Optional[str] = None,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        button = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label=label or DEFAULT_BUTTON_LABEL,
            emoji=emoji or None,
            custom_id=VERIFY_BUTTON_CUSTOM_ID,
        )
        button.callback = self.on_verify_press
        self.add_item(button)

    async def on_verify_press(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_verify_button(interaction)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class VerificationCog(commands.Cog, name="Verification"):
    """Verification panel, role handout, timeouts and audit log."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._persistent_view: Optional[VerificationButtonView] = None

    # -- lifecycle ---------------------------------------------------------
    async def cog_load(self) -> None:
        self._create_tables()
        # One static-custom_id registration covers panels in every guild.
        self._persistent_view = VerificationButtonView(self)
        self.bot.add_view(self._persistent_view)
        if not self.timeout_check.is_running():
            self.timeout_check.start()

    def cog_unload(self) -> None:
        if self.timeout_check.is_running():
            self.timeout_check.cancel()
        if self._persistent_view is not None:
            try:
                self.bot.remove_view(self._persistent_view)
            except Exception:
                log.warning("[Verification] failed to unregister persistent view", exc_info=True)
            self._persistent_view = None

    # -- DB helpers (everything wrapped + logged) ---------------------------
    def _create_tables(self) -> None:
        try:
            create_tables(_TABLES)
        except Exception:
            log.exception("[Verification] could not create tables")

    def _db_run(self, sql: str, args: Tuple[Any, ...] = ()) -> bool:
        try:
            run(sql, args)
            return True
        except Exception:
            log.exception("[Verification] DB write failed: %s", sql.strip().split("\n")[0])
            return False

    def _db_fetchone(self, sql: str, args: Tuple[Any, ...] = ()):
        try:
            return fetchone(sql, args)
        except Exception:
            log.exception("[Verification] DB read failed: %s", sql.strip().split("\n")[0])
            return None

    def _db_fetchall(self, sql: str, args: Tuple[Any, ...] = ()):
        try:
            return fetchall(sql, args)
        except Exception:
            log.exception("[Verification] DB read failed: %s", sql.strip().split("\n")[0])
            return []

    def _log_event(self, guild_id: int, user_id: int, event_type: str, detail: str = "") -> None:
        self._db_run(
            "INSERT INTO verification_events (guild_id, user_id, event_type, detail, created_at) "
            "VALUES (?,?,?,?,?)",
            (guild_id, user_id, event_type, detail or "", now_iso()),
        )

    # -- config -------------------------------------------------------------
    def _cfg_from_row(self, row) -> Dict[str, Any]:
        cfg = dict(_CFG_DEFAULTS)
        for key in cfg:
            try:
                value = row[key]
            except (IndexError, KeyError):
                continue
            if value is not None:
                cfg[key] = value
        return cfg

    def _cfg(self, guild_id: int) -> Dict[str, Any]:
        cfg = dict(_CFG_DEFAULTS)
        cfg["guild_id"] = guild_id
        row = self._db_fetchone("SELECT * FROM verification_config WHERE guild_id = ?", (guild_id,))
        if row is None:
            return cfg
        cfg = self._cfg_from_row(row)
        cfg["guild_id"] = guild_id
        return cfg

    def _fb_default(self, section: str, key: str, default: Any) -> Any:
        """Defensively read a default from the core config (bot.fb_config)."""
        fb_config = getattr(self.bot, "fb_config", None)
        if fb_config is None:
            return default
        try:
            section_obj = getattr(fb_config, section, None)
            if section_obj is None:
                return default
            value = getattr(section_obj, key, default)
        except Exception:
            return default
        return default if value is None else value

    # -- rendering ------------------------------------------------------------
    def _panel_embed(self, cfg: Dict[str, Any], guild: discord.Guild) -> discord.Embed:
        try:
            color = discord.Color.from_str(str(cfg.get("color") or DEFAULT_COLOR))
        except (ValueError, TypeError):
            color = COLOR_BRAND
        description = str(cfg.get("description") or DEFAULT_DESCRIPTION)
        extras: List[str] = []
        account_age_days = int(cfg.get("account_age_days") or 0)
        if account_age_days > 0:
            extras.append(f"🛡️ Accounts must be at least **{account_age_days} day(s)** old to verify.")
        timeout_minutes = int(cfg.get("timeout_minutes") or 0)
        if timeout_minutes > 0:
            action = "kicked" if int(cfg.get("kick_on_timeout") or 0) else "logged"
            extras.append(
                f"⏱️ Members who don't verify within **{fmt_duration(timeout_minutes * 60)}** are {action}."
            )
        if extras:
            description = f"{description}\n\n" + "\n".join(extras)
        embed = discord.Embed(
            title=str(cfg.get("title") or DEFAULT_TITLE),
            description=description,
            color=color,
        )
        embed.set_footer(text=f"{guild.name} • Verification")
        return embed

    def _build_view(self, cfg: Dict[str, Any]) -> VerificationButtonView:
        return VerificationButtonView(
            self,
            label=str(cfg.get("button_label") or DEFAULT_BUTTON_LABEL),
            emoji=str(cfg.get("button_emoji") or "") or None,
        )

    # -- helpers ------------------------------------------------------------
    async def _send_log(self, guild: discord.Guild, cfg: Dict[str, Any], embed: discord.Embed) -> None:
        channel_id = int(cfg.get("log_channel_id") or 0)
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("[Verification] couldn't write to log channel %s: %s", channel_id, exc)

    async def _delete_stored_panel(self, guild: discord.Guild, cfg: Dict[str, Any]) -> None:
        old_message_id = int(cfg.get("panel_message_id") or 0)
        if not old_message_id:
            return
        old_channel_id = int(cfg.get("panel_channel_id") or 0)
        old_channel = guild.get_channel(old_channel_id) if old_channel_id else None
        if old_channel is None:
            return
        try:
            message = await old_channel.fetch_message(old_message_id)
            await message.delete()
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.info("[Verification] couldn't delete old panel %s: %s", old_message_id, exc)

    async def _ensure_role(
        self, guild: discord.Guild, name: str, color: discord.Color, problems: List[str]
    ) -> Optional[discord.Role]:
        try:
            return await guild.create_role(
                name=name,
                color=color,
                reason="FactionBot verification setup",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            problems.append(
                f"I couldn't create the **{name}** role ({exc}). Create it manually and re-run setup."
            )
            return None

    async def _ephemeral_reply(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("[Verification] failed to deliver interaction response: %s", exc)

    def _resolve_channel(self, ctx: commands.Context, value: str) -> Optional[discord.TextChannel]:
        cleaned = value.strip()
        if cleaned.startswith("<#") and cleaned.endswith(">"):
            cleaned = cleaned[2:-1]
        if cleaned.isdigit():
            channel = ctx.guild.get_channel(int(cleaned))
            return channel if isinstance(channel, discord.TextChannel) else None
        return discord.utils.get(ctx.guild.text_channels, name=cleaned.lstrip("#"))

    # -- button handler -------------------------------------------------------
    async def handle_verify_button(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await self._ephemeral_reply(
                interaction, error("Server only", "The verify button only works inside a server.")
            )
            return

        cfg = self._cfg(guild.id)
        if not int(cfg.get("enabled") or 0):
            await self._ephemeral_reply(
                interaction,
                error("Verification unavailable", "Verification is not enabled on this server."),
            )
            return

        # Defer early — role changes + DM + log write can take a moment.
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.HTTPException, discord.Forbidden):
            return

        record = self._db_fetchone(
            "SELECT 1 FROM verification_records WHERE guild_id = ? AND user_id = ?",
            (guild.id, user.id),
        )
        verified_role = guild.get_role(int(cfg.get("verified_role_id") or 0))
        if record is not None or (verified_role is not None and verified_role in user.roles):
            await self._ephemeral_reply(
                interaction, info("Already verified", "You're already verified here — no need to click again.")
            )
            return

        account_age_days = int(cfg.get("account_age_days") or 0)
        if account_age_days > 0:
            age_seconds = (discord.utils.utcnow() - user.created_at).total_seconds()
            required_seconds = account_age_days * 86400
            if age_seconds < required_seconds:
                remaining = required_seconds - age_seconds
                remaining_days = int(remaining // 86400) + (1 if remaining % 86400 else 0)
                await self._ephemeral_reply(
                    interaction,
                    error(
                        "Account too new",
                        f"Your account must be at least **{account_age_days} day(s)** old to verify.\n"
                        f"Try again in about **{remaining_days} day(s)**.",
                    ),
                )
                return

        if verified_role is None:
            await self._ephemeral_reply(
                interaction,
                error(
                    "Not configured",
                    "This server's verified role is missing — ask a staff member to re-run setup.",
                ),
            )
            await self._send_log(
                guild,
                cfg,
                warning(
                    "Verification misconfigured",
                    f"No verified role set — {user.mention} ({user.id}) tried to verify.",
                ),
            )
            return

        me = guild.me
        if not bot_can_manage_role(guild, verified_role):
            await self._ephemeral_reply(
                interaction,
                warning(
                    "Role hierarchy problem",
                    f"I can't assign **{verified_role.name}** — it must be below my top role. Staff have been notified.",
                ),
            )
            await self._send_log(
                guild,
                cfg,
                warning(
                    "Role hierarchy problem",
                    f"Verified role **{verified_role.name}** is above my top role; "
                    f"{user.mention} ({user.id}) could not be verified.",
                ),
            )
            return

        # Remove the unverified role first (best effort — never blocks verify).
        unverified_role = guild.get_role(int(cfg.get("unverified_role_id") or 0))
        removed_unverified = False
        if unverified_role is not None and unverified_role in user.roles:
            if bot_can_manage_role(guild, unverified_role):
                try:
                    await user.remove_roles(unverified_role, reason="Verification complete")
                    removed_unverified = True
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning(
                        "[Verification] couldn't remove unverified role from %s: %s", user.id, exc
                    )
            else:
                log.warning(
                    "[Verification] unverified role %s is above my top role in guild %s",
                    unverified_role.id,
                    guild.id,
                )

        # Grant the verified role (critical path).
        try:
            await user.add_roles(verified_role, reason="Verification complete")
        except discord.Forbidden:
            await self._ephemeral_reply(
                interaction,
                error("Missing permissions", "I lost permission to assign the verified role. Ask staff to fix this."),
            )
            return
        except discord.HTTPException as exc:
            await self._ephemeral_reply(
                interaction,
                error("Verification failed", f"Discord rejected the role assignment: {exc}"),
            )
            return

        detail = f"granted {verified_role.name} (button)"
        if removed_unverified:
            detail += "; unverified role removed"
        self._db_run(
            "INSERT OR REPLACE INTO verification_records (guild_id, user_id, verified_at, method) "
            "VALUES (?,?,?,'button')",
            (guild.id, user.id, now_iso()),
        )
        self._log_event(guild.id, user.id, "verify", detail)

        dm_ok = True
        try:
            dm_text = str(cfg.get("dm_message") or DEFAULT_DM_MESSAGE).format(
                user=user.mention, server=guild.name
            )
        except (KeyError, IndexError, ValueError):
            dm_text = DEFAULT_DM_MESSAGE.format(user=user.mention, server=guild.name)
        try:
            await user.send(embed=success("Verified", dm_text))
        except (discord.Forbidden, discord.HTTPException):
            dm_ok = False

        await self._send_log(
            guild,
            cfg,
            success("Member verified", f"{user.mention} ({user.id}) verified via the panel button."),
        )

        description = "You're verified — enjoy your stay!"
        if not dm_ok:
            description += "\n*(I couldn't DM you — check your privacy settings if you expected a message.)*"
        await self._ephemeral_reply(interaction, success("Verified", description))

    # -- member join listener ---------------------------------------------------
    @commands.Cog.listener("on_member_join")
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.guild is None:
            return
        guild = member.guild
        cfg = self._cfg(guild.id)
        if not int(cfg.get("enabled") or 0):
            return

        age_days = (discord.utils.utcnow() - member.created_at).days
        self._log_event(guild.id, member.id, "join", f"account age {age_days}d")

        unverified_role = guild.get_role(int(cfg.get("unverified_role_id") or 0))
        if unverified_role is not None:
            if bot_can_manage_role(guild, unverified_role):
                try:
                    await member.add_roles(unverified_role, reason="Unverified on join")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning(
                        "[Verification] couldn't assign unverified role to %s: %s", member.id, exc
                    )
            else:
                log.warning(
                    "[Verification] unverified role %s is above my top role in guild %s",
                    unverified_role.id,
                    guild.id,
                )

        if int(cfg.get("dm_on_join") or 0):
            panel_channel = guild.get_channel(int(cfg.get("panel_channel_id") or 0))
            where = f"**#{panel_channel.name}**" if panel_channel is not None else "the verification channel"
            try:
                await member.send(
                    embed=info(
                        f"Welcome to {guild.name}!",
                        f"👋 Welcome, {member.mention}!\n\n"
                        f"To unlock the server, click the **Verify** button in {where}.\n"
                        f"If you don't verify, you'll stay in the waiting area.",
                    )
                )
            except (discord.Forbidden, discord.HTTPException):
                pass  # Closed DMs are normal — nothing to do.

    # -- timeout sweep --------------------------------------------------------
    @tasks.loop(minutes=1)
    async def timeout_check(self) -> None:
        rows = self._db_fetchall(
            "SELECT * FROM verification_config WHERE enabled = 1 AND timeout_minutes > 0"
        )
        if not rows:
            return
        now = discord.utils.utcnow()
        for row in rows:
            try:
                await self._process_guild_timeouts(row, now)
            except Exception:
                log.exception("[Verification] timeout sweep failed for guild %s", row["guild_id"])

    @timeout_check.before_loop
    async def before_timeout_check(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_guild_timeouts(self, row, now) -> None:
        guild = self.bot.get_guild(int(row["guild_id"]))
        if guild is None:
            return
        cfg = self._cfg_from_row(row)
        unverified_role = guild.get_role(int(row["unverified_role_id"] or 0))
        if unverified_role is None:
            return
        timeout_minutes = int(row["timeout_minutes"] or 0)
        if timeout_minutes <= 0:
            return
        kick_on_timeout = bool(int(row["kick_on_timeout"] or 0))
        # Take a snapshot of the member set. `role.members` is a live view
        # that mutates as we kick people; iterating it directly can raise
        # "set changed size during iteration" on busy guilds.
        members_snapshot = list(unverified_role.members)
        for member in members_snapshot:
            if member.bot or is_exempt(member):
                continue
            joined = member.joined_at
            if joined is None:
                continue
            elapsed_minutes = (now - joined).total_seconds() / 60
            if elapsed_minutes < timeout_minutes:
                continue
            # One action per join session — skip if already acted since they joined.
            joined_marker = joined.isoformat()
            already = self._db_fetchone(
                "SELECT 1 FROM verification_events WHERE guild_id = ? AND user_id = ? "
                "AND event_type IN ('timeout', 'kick') AND created_at >= ? LIMIT 1",
                (guild.id, member.id, joined_marker),
            )
            if already is not None:
                continue
            elapsed_text = fmt_duration(elapsed_minutes * 60)
            if kick_on_timeout:
                try:
                    await member.kick(
                        reason=f"Verification timeout: unverified for {int(elapsed_minutes)}m "
                        f"(limit {timeout_minutes}m)"
                    )
                    self._log_event(
                        guild.id, member.id, "kick", f"unverified {int(elapsed_minutes)}m > {timeout_minutes}m"
                    )
                    await self._send_log(
                        guild,
                        cfg,
                        warning(
                            "Verification timeout — kicked",
                            f"{member} ({member.id}) was kicked after {elapsed_text} without verifying.",
                        ),
                    )
                except discord.Forbidden:
                    self._log_event(
                        guild.id,
                        member.id,
                        "timeout",
                        f"kick failed (missing permission) after {int(elapsed_minutes)}m",
                    )
                    await self._send_log(
                        guild,
                        cfg,
                        warning(
                            "Verification timeout — kick failed",
                            f"I lack permission to kick {member.mention}; they remain unverified.",
                        ),
                    )
                except discord.HTTPException as exc:
                    self._log_event(
                        guild.id,
                        member.id,
                        "timeout",
                        f"kick failed ({exc.status}) after {int(elapsed_minutes)}m",
                    )
                    await self._send_log(
                        guild,
                        cfg,
                        warning(
                            "Verification timeout — kick failed",
                            f"Discord rejected kicking {member.mention}: {exc}",
                        ),
                    )
            else:
                self._log_event(
                    guild.id,
                    member.id,
                    "timeout",
                    f"unverified for {int(elapsed_minutes)}m (limit {timeout_minutes}m, log-only)",
                )
                await self._send_log(
                    guild,
                    cfg,
                    info(
                        "Verification timeout",
                        f"{member.mention} has been unverified for over {timeout_minutes} minutes "
                        "(no kick configured).",
                    ),
                )

    # -- command group ----------------------------------------------------------
    @commands.group(name="verification", aliases=["v"])
    @commands.guild_only()
    async def verification_group(self, ctx: commands.Context) -> None:
        """Manage the server verification system."""
        prefix = ctx.clean_prefix
        embed = info(
            "Verification",
            "Guided setup: `vsetup` — then manage with the subcommands below.",
        )
        embed.add_field(
            name="Subcommands",
            value=(
                f"`{prefix}v setup` · `{prefix}v config` · `{prefix}v panel` · `{prefix}v status`\n"
                f"`{prefix}v reset <member>` · `{prefix}v test` · `{prefix}v enable` · `{prefix}v disable`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Shortcut",
            value=f"`{prefix}vsetup [channel] [verified_role] [unverified_role]`",
            inline=False,
        )
        await ctx.send(embed=embed)

    # -- setup --------------------------------------------------------------------
    @verification_group.command(name="setup")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def verification_setup(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        verified_role: Optional[discord.Role] = None,
        unverified_role: Optional[discord.Role] = None,
    ) -> None:
        """Guided setup — every argument is optional and smart-defaulted."""
        await self._run_setup(ctx, channel, verified_role, unverified_role)

    @commands.command(name="vsetup")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def vsetup(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        verified_role: Optional[discord.Role] = None,
        unverified_role: Optional[discord.Role] = None,
    ) -> None:
        """One-command verification setup (alias of /verification setup)."""
        await self._run_setup(ctx, channel, verified_role, unverified_role)

    async def _run_setup(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel],
        verified_role: Optional[discord.Role],
        unverified_role: Optional[discord.Role],
    ) -> None:
        guild = ctx.guild
        me = guild.me
        problems: List[str] = []

        if not me.guild_permissions.manage_roles:
            await ctx.send(
                embed=error("Missing permission", "I need the **Manage Roles** permission to run verification.")
            )
            return

        # --- panel channel -----------------------------------------------------
        if channel is None:
            default_channel_id = int(self._fb_default("channels", "verification_main", 0) or 0)
            resolved = guild.get_channel(default_channel_id) if default_channel_id else None
            channel = resolved if isinstance(resolved, discord.TextChannel) else None
        if channel is None and isinstance(ctx.channel, discord.TextChannel):
            channel = ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await ctx.send(
                embed=error("No panel channel", "I couldn't find a text channel for the panel — pass one explicitly.")
            )
            return
        channel_perms = channel.permissions_for(me)
        if not (channel_perms.send_messages and channel_perms.embed_links):
            problems.append(f"I can't send embeds in {channel.mention} — the panel won't appear until fixed.")

        # --- verified role -------------------------------------------------------
        if verified_role is None:
            default_verified_id = int(self._fb_default("roles", "verified", 0) or 0)
            verified_role = guild.get_role(default_verified_id) if default_verified_id else None
        if verified_role is None:
            verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        if verified_role is None:
            verified_role = await self._ensure_role(
                guild, VERIFIED_ROLE_NAME, discord.Color.from_str(DEFAULT_COLOR), problems
            )
        if verified_role is None:
            await ctx.send(
                embed=error(
                    "No verified role",
                    "I couldn't find or create a verified role. Re-run setup and pass one, "
                    "e.g. `vsetup verified_role:@Verified`.",
                ),
            )
            return
        if not bot_can_manage_role(guild, verified_role):
            problems.append(
                f"Verified role **{verified_role.name}** is at or above my top role — I can't assign it."
            )

        # --- unverified role ------------------------------------------------------
        if unverified_role is None:
            unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
        if unverified_role is None:
            unverified_role = await self._ensure_role(
                guild, UNVERIFIED_ROLE_NAME, discord.Color.from_str("#95A5A6"), problems
            )
        if unverified_role is not None and not bot_can_manage_role(guild, unverified_role):
            problems.append(
                f"Unverified role **{unverified_role.name}** is at or above my top role — I can't remove it."
            )

        # --- sanity warnings --------------------------------------------------------
        for role, label in ((verified_role, "Verified"), (unverified_role, "Unverified")):
            if role is not None and any(member.bot for member in role.members):
                problems.append(f"The {label} role is already assigned to a bot — double-check that's intended.")

        log_channel_id = 0
        default_log_id = int(self._fb_default("channels", "log", 0) or 0)
        if default_log_id and guild.get_channel(default_log_id) is not None:
            log_channel_id = default_log_id

        account_age_days = int(self._fb_default("limits", "min_account_age_days", 0) or 0)

        existing = self._cfg(guild.id)
        cfg: Dict[str, Any] = {
            "enabled": 1,
            "mode": "button",
            "unverified_role_id": unverified_role.id if unverified_role is not None else 0,
            "verified_role_id": verified_role.id,
            "log_channel_id": log_channel_id,
            "panel_channel_id": channel.id,
            "panel_message_id": 0,
            "title": existing.get("title") or DEFAULT_TITLE,
            "description": existing.get("description") or DEFAULT_DESCRIPTION,
            "button_label": existing.get("button_label") or DEFAULT_BUTTON_LABEL,
            "button_emoji": existing.get("button_emoji") or "",
            "color": existing.get("color") or DEFAULT_COLOR,
            "timeout_minutes": int(existing.get("timeout_minutes") or 0),
            "kick_on_timeout": int(existing.get("kick_on_timeout") or 0),
            "account_age_days": account_age_days,
            "dm_on_join": int(existing.get("dm_on_join") if existing.get("dm_on_join") is not None else 1),
            "dm_message": existing.get("dm_message") or DEFAULT_DM_MESSAGE,
        }

        # Replace the previous panel, if any.
        await self._delete_stored_panel(guild, existing)

        try:
            message = await channel.send(embed=self._panel_embed(cfg, guild), view=self._build_view(cfg))
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(
                embed=error("Panel failed", f"Couldn't post the panel in {channel.mention}: {exc}")
            )
            return
        cfg["panel_message_id"] = message.id

        saved = self._db_run(
            "INSERT OR REPLACE INTO verification_config (guild_id, enabled, mode, unverified_role_id, "
            "verified_role_id, log_channel_id, panel_channel_id, panel_message_id, title, description, "
            "button_label, button_emoji, color, timeout_minutes, kick_on_timeout, account_age_days, "
            "dm_on_join, dm_message) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                guild.id,
                cfg["enabled"],
                cfg["mode"],
                cfg["unverified_role_id"],
                cfg["verified_role_id"],
                cfg["log_channel_id"],
                cfg["panel_channel_id"],
                cfg["panel_message_id"],
                cfg["title"],
                cfg["description"],
                cfg["button_label"],
                cfg["button_emoji"],
                cfg["color"],
                cfg["timeout_minutes"],
                cfg["kick_on_timeout"],
                cfg["account_age_days"],
                cfg["dm_on_join"],
                cfg["dm_message"],
            ),
        )
        if not saved:
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.warning("[Verification] couldn't delete unsaved panel message %s: %s", message.id, exc)
            await ctx.send(
                embed=error("Database error", "The panel was posted but I couldn't save the config — try again.")
            )
            return

        self._log_event(guild.id, guild.me.id, "setup", f"panel #{channel.id}/{message.id} by {ctx.author.id}")

        summary = success("Verification setup complete", "The verification system is **live**.")
        summary.add_field(name="Verified role", value=verified_role.mention, inline=True)
        summary.add_field(
            name="Unverified role",
            value=unverified_role.mention if unverified_role is not None else "*(none)*",
            inline=True,
        )
        summary.add_field(name="Panel", value=channel.mention, inline=True)
        summary.add_field(
            name="Log channel", value=f"<#{log_channel_id}>" if log_channel_id else "*(none)*", inline=True
        )
        summary.add_field(
            name="Account age", value=f"{account_age_days} days" if account_age_days else "any age", inline=True
        )
        summary.add_field(name="DM on join", value="on" if cfg["dm_on_join"] else "off", inline=True)
        await ctx.send(embed=summary)
        if problems:
            await ctx.send(
                embed=warning(
                    "Setup warnings",
                    "\n".join(f"• {problem}" for problem in problems)[:4000],
                )
            )

    # -- config ---------------------------------------------------------------------
    @verification_group.command(name="config")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def verification_config(
        self, ctx: commands.Context, key: Optional[str] = None, *, value: Optional[str] = None
    ) -> None:
        """View settings, or set one: config <key> <value>."""
        if key is None:
            await self._send_config_overview(ctx)
            return
        key = key.strip().lower()
        if key not in _CFG_COLUMNS:
            await ctx.send(
                embed=error(
                    "Unknown setting",
                    f"Unknown key `{key}`. Valid keys: {', '.join(sorted(_CFG_COLUMNS))}.",
                )
            )
            return
        if value is None or not value.strip():
            await ctx.send(
                embed=error("Missing value", f"Usage: `{ctx.clean_prefix}v config {key} <value>`")
            )
            return

        row = self._db_fetchone(
            "SELECT guild_id FROM verification_config WHERE guild_id = ?", (ctx.guild.id,)
        )
        if row is None:
            await ctx.send(embed=error("Not set up", "Run `vsetup` first — there's no configuration to edit."))
            return

        column = _CFG_COLUMNS[key]
        value = value.strip()
        store: Any
        display: str
        if key in _CFG_BOOL_KEYS:
            parsed = _parse_bool(value)
            if parsed is None:
                await ctx.send(
                    embed=error("Invalid value", f"`{key}` expects **on** or **off** (got `{value}`).")
                )
                return
            store = 1 if parsed else 0
            display = "on" if parsed else "off"
        elif key in _CFG_INT_KEYS:
            try:
                parsed = int(value)
            except ValueError:
                await ctx.send(
                    embed=error("Invalid value", f"`{key}` expects a whole number (got `{value}`).")
                )
                return
            if parsed < 0:
                await ctx.send(embed=error("Invalid value", f"`{key}` can't be negative."))
                return
            if key == "timeout_minutes" and parsed > MAX_TIMEOUT_MINUTES:
                await ctx.send(
                    embed=error("Invalid value", f"`{key}` is capped at {MAX_TIMEOUT_MINUTES} minutes.")
                )
                return
            store = parsed
            display = str(parsed)
        elif key in _CFG_CHANNEL_KEYS:
            channel = self._resolve_channel(ctx, value)
            if channel is None:
                await ctx.send(
                    embed=error(
                        "Channel not found",
                        f"I couldn't find a text channel from `{value}` — try a mention or an ID.",
                    )
                )
                return
            if not channel.permissions_for(ctx.guild.me).send_messages:
                await ctx.send(
                    embed=warning(
                        "Heads up", f"I can't send messages in {channel.mention} — fix that before relying on it."
                    )
                )
            store = channel.id
            display = channel.mention
        elif key in _CFG_TEXT_KEYS:
            if key == "button_emoji" and value.lower() in ("none", "off", "clear", "remove"):
                store = ""
                display = "*(cleared)*"
            else:
                store = value
                display = value if len(value) <= 60 else f"{value[:57]}…"
        else:
            # Unreachable — _CFG_COLUMNS is the union of the four key sets.
            await ctx.send(embed=error("Unknown setting", f"Key `{key}` has no value handler."))
            return

        if not self._db_run(
            f"UPDATE verification_config SET {column} = ? WHERE guild_id = ?", (store, ctx.guild.id)
        ):
            await ctx.send(embed=error("Database error", "Couldn't save that setting — try again."))
            return

        embed = success("Setting updated", f"`{key}` is now: {display}")
        if key in ("title", "description", "button_label", "button_emoji"):
            embed.description += f"\nRun `{ctx.clean_prefix}v panel` to repost the panel with the new look."
        elif key == "panel_channel":
            embed.description += f"\nRun `{ctx.clean_prefix}v panel` to move the panel there."
        await ctx.send(embed=embed)

    async def _send_config_overview(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        cfg = self._cfg(guild.id)
        prefix = ctx.clean_prefix

        def role_mention(role_id: int) -> str:
            return f"<@&{role_id}>" if role_id else "*(not set)*"

        def channel_mention(channel_id: int) -> str:
            return f"<#{channel_id}>" if channel_id else "*(not set)*"

        title = str(cfg.get("title") or DEFAULT_TITLE)
        description = str(cfg.get("description") or DEFAULT_DESCRIPTION)
        lines = [
            f"Enabled: **{'yes' if int(cfg.get('enabled') or 0) else 'no'}** · Mode: `{cfg.get('mode')}`",
            f"Verified role: {role_mention(int(cfg.get('verified_role_id') or 0))}",
            f"Unverified role: {role_mention(int(cfg.get('unverified_role_id') or 0))}",
            f"Panel: {channel_mention(int(cfg.get('panel_channel_id') or 0))}",
            f"Log channel: {channel_mention(int(cfg.get('log_channel_id') or 0))}",
            f"Account age: **{int(cfg.get('account_age_days') or 0)} day(s)**",
            f"Timeout: **{int(cfg.get('timeout_minutes') or 0)} min** · "
            f"Kick: **{'yes' if int(cfg.get('kick_on_timeout') or 0) else 'no'}**",
            f"DM on join: **{'yes' if int(cfg.get('dm_on_join') or 0) else 'no'}**",
            f"Title: {title[:100]}",
            f"Button: **{cfg.get('button_label') or DEFAULT_BUTTON_LABEL}** {cfg.get('button_emoji') or ''}".strip(),
            f"Description: {description[:150]}{'…' if len(description) > 150 else ''}",
        ]
        embed = info("Verification settings", "\n".join(lines))
        embed.add_field(
            name="Customize",
            value=(
                f"`{prefix}v config <key> <value>`\n"
                f"Keys: {', '.join(sorted(_CFG_COLUMNS))}"
            ),
            inline=False,
        )
        dm_message = str(cfg.get("dm_message") or DEFAULT_DM_MESSAGE)
        embed.add_field(
            name="Verify DM",
            value=dm_message[:1000] + ("…" if len(dm_message) > 1000 else ""),
            inline=False,
        )
        await ctx.send(embed=embed)

    # -- panel ------------------------------------------------------------------------
    @verification_group.command(name="panel")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def verification_panel(self, ctx: commands.Context) -> None:
        """Repost / refresh the verification panel."""
        guild = ctx.guild
        cfg = self._cfg(guild.id)
        if not int(cfg.get("verified_role_id") or 0):
            await ctx.send(embed=error("Not set up", "Run `vsetup` first — there's no panel to refresh."))
            return
        channel = guild.get_channel(int(cfg.get("panel_channel_id") or 0))
        if channel is None and isinstance(ctx.channel, discord.TextChannel):
            channel = ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await ctx.send(embed=error("No panel channel", "The configured panel channel is gone — set one via `v config panel_channel <channel>`."))
            return
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            await ctx.send(
                embed=error("Missing permission", f"I need **Send Messages** + **Embed Links** in {channel.mention}.")
            )
            return

        await self._delete_stored_panel(guild, cfg)
        try:
            message = await channel.send(embed=self._panel_embed(cfg, guild), view=self._build_view(cfg))
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(embed=error("Panel failed", f"Couldn't post the panel in {channel.mention}: {exc}"))
            return
        self._db_run(
            "UPDATE verification_config SET panel_channel_id = ?, panel_message_id = ? WHERE guild_id = ?",
            (channel.id, message.id, guild.id),
        )
        await ctx.send(embed=success("Panel reposted", f"The verification panel is live in {channel.mention}."))

    # -- status -----------------------------------------------------------------------
    @verification_group.command(name="status")
    @commands.guild_only()
    async def verification_status(self, ctx: commands.Context) -> None:
        """Verification stats for this server."""
        guild = ctx.guild
        cfg = self._cfg(guild.id)

        verified_count = 0
        row = self._db_fetchone(
            "SELECT COUNT(*) AS c FROM verification_records WHERE guild_id = ?", (guild.id,)
        )
        if row is not None:
            verified_count = int(row["c"])

        pending = 0
        unverified_role = guild.get_role(int(cfg.get("unverified_role_id") or 0))
        if unverified_role is not None:
            pending = sum(1 for member in unverified_role.members if not member.bot)

        event_count = 0
        row = self._db_fetchone(
            "SELECT COUNT(*) AS c FROM verification_events WHERE guild_id = ?", (guild.id,)
        )
        if row is not None:
            event_count = int(row["c"])

        last_verified = "—"
        row = self._db_fetchone(
            "SELECT verified_at FROM verification_records WHERE guild_id = ? ORDER BY verified_at DESC LIMIT 1",
            (guild.id,),
        )
        if row is not None:
            parsed = parse_iso(row["verified_at"])
            last_verified = fmt_dt(parsed) if parsed else "—"

        verified_role = guild.get_role(int(cfg.get("verified_role_id") or 0))
        timeout_minutes = int(cfg.get("timeout_minutes") or 0)
        embed = info(
            "Verification status",
            f"System: **{'✅ enabled' if int(cfg.get('enabled') or 0) else '❌ disabled'}** · Mode: `{cfg.get('mode')}`",
        )
        embed.add_field(
            name="Verified role",
            value=verified_role.mention if verified_role is not None else "*(not set)*",
            inline=True,
        )
        embed.add_field(
            name="Unverified role",
            value=unverified_role.mention if unverified_role is not None else "*(not set)*",
            inline=True,
        )
        embed.add_field(name="Panel", value=f"<#{cfg.get('panel_channel_id')}>" if int(cfg.get("panel_channel_id") or 0) else "*(not set)*", inline=True)
        embed.add_field(name="Verified members", value=str(verified_count), inline=True)
        embed.add_field(name="Awaiting verification", value=str(pending), inline=True)
        embed.add_field(name="Last verification", value=last_verified, inline=True)
        embed.add_field(
            name="Timeout",
            value=(
                f"{fmt_duration(timeout_minutes * 60)} · {'kick' if int(cfg.get('kick_on_timeout') or 0) else 'log only'}"
                if timeout_minutes
                else "off"
            ),
            inline=True,
        )
        embed.add_field(
            name="Account age gate",
            value=f"{int(cfg.get('account_age_days') or 0)} day(s)" if int(cfg.get("account_age_days") or 0) else "any age",
            inline=True,
        )
        embed.add_field(name="Log entries", value=str(event_count), inline=True)
        await ctx.send(embed=embed)

    # -- reset ---------------------------------------------------------------------------
    @verification_group.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def verification_reset(self, ctx: commands.Context, member: discord.Member) -> None:
        """Remove a member's verified status."""
        guild = ctx.guild
        cfg = self._cfg(guild.id)
        ok, reason = can_act_on(ctx.author, member)
        if not ok:
            await ctx.send(embed=error("Can't target that member", reason))
            return

        record = self._db_fetchone(
            "SELECT 1 FROM verification_records WHERE guild_id = ? AND user_id = ?", (guild.id, member.id)
        )
        verified_role = guild.get_role(int(cfg.get("verified_role_id") or 0))
        removed = False
        if verified_role is not None and verified_role in member.roles:
            if bot_can_manage_role(guild, verified_role):
                try:
                    await member.remove_roles(verified_role, reason=f"Verification reset by {ctx.author}")
                    removed = True
                except (discord.Forbidden, discord.HTTPException) as exc:
                    await ctx.send(
                        embed=warning("Role removal failed", f"Discord rejected removing the role: {exc}")
                    )
            else:
                await ctx.send(
                    embed=warning(
                        "Role hierarchy problem",
                        f"**{verified_role.name}** is above my top role — remove it manually from {member.mention}.",
                    )
                )
        if record is not None:
            self._db_run(
                "DELETE FROM verification_records WHERE guild_id = ? AND user_id = ?", (guild.id, member.id)
            )
        self._log_event(guild.id, member.id, "reset", f"reset by {ctx.author.id}; role_removed={removed}")
        await self._send_log(
            guild,
            cfg,
            warning(
                "Verification reset",
                f"{member.mention} ({member.id}) was un-verified by {ctx.author.mention}.",
            ),
        )
        embed = success("Member reset", f"{member.mention} must verify again to regain access.")
        if not removed and record is None:
            embed = info("Nothing to reset", f"{member.mention} had no verification record.")
        await ctx.send(embed=embed)

    # -- test ---------------------------------------------------------------------------
    @verification_group.command(name="test")
    @commands.guild_only()
    async def verification_test(self, ctx: commands.Context) -> None:
        """Preview the verification panel here (nothing is saved)."""
        guild = ctx.guild
        cfg = self._cfg(guild.id)
        embed = self._panel_embed(cfg, guild)
        embed.description = (
            f"{embed.description}\n\n*🧪 Test preview — settings aren't changed by this command.*"
        )
        try:
            await ctx.send(embed=embed, view=self._build_view(cfg))
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(embed=error("Preview failed", f"Couldn't render the preview: {exc}"))

    # -- enable / disable ------------------------------------------------------------------
    @verification_group.command(name="enable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def verification_enable(self, ctx: commands.Context) -> None:
        """Turn verification on."""
        row = self._db_fetchone(
            "SELECT verified_role_id FROM verification_config WHERE guild_id = ?", (ctx.guild.id,)
        )
        if row is None:
            await ctx.send(embed=error("Not set up", "Run `vsetup` first."))
            return
        self._db_run("UPDATE verification_config SET enabled = 1 WHERE guild_id = ?", (ctx.guild.id,))
        self._log_event(ctx.guild.id, ctx.author.id, "setup", f"enabled by {ctx.author.id}")
        await ctx.send(embed=success("Verification enabled", "New joins will get the unverified role again."))

    @verification_group.command(name="disable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def verification_disable(self, ctx: commands.Context) -> None:
        """Turn verification off (panel stays, button will report disabled)."""
        row = self._db_fetchone(
            "SELECT guild_id FROM verification_config WHERE guild_id = ?", (ctx.guild.id,)
        )
        if row is None:
            await ctx.send(embed=error("Not set up", "Run `vsetup` first."))
            return
        self._db_run("UPDATE verification_config SET enabled = 0 WHERE guild_id = ?", (ctx.guild.id,))
        self._log_event(ctx.guild.id, ctx.author.id, "setup", f"disabled by {ctx.author.id}")
        await ctx.send(
            embed=success(
                "Verification disabled",
                "The button now replies that verification is unavailable. Re-enable with `v enable`.",
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VerificationCog(bot))
