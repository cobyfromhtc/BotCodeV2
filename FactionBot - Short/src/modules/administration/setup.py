# -*- coding: utf-8 -*-
"""modules/administration/setup.py — FactionBot guided setup & global settings overview.

Commands
--------
``!csetup``   Interactive setup status panel: one line per subsystem
              (tickets, verification, polls, invites, leveling, automod,
              logging) with a ✅/⚙️/❌/🔘 status, plus per-system detail
              pages with current config values and EXACT next-step commands.
``!settings`` Global config summary (channels, roles, limits, feature
              toggles, prefix) read defensively from ``bot.fb_config``.

All DB access goes through :mod:`utils.botkit` (same SQLite file as the
legacy DataManager). Tables owned by the *other* new cogs (verification,
polls, invites, leveling, automod) may not exist yet while the bot is being
assembled — every query is therefore wrapped in try/except and a missing
table simply counts as "not set up".
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils import botkit  # relocated into utils/ by the SaaS restructure

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subsystem registry
# ---------------------------------------------------------------------------
KEY_TICKETS = "tickets"
KEY_VERIFICATION = "verification"
KEY_POLLS = "polls"
KEY_INVITES = "invites"
KEY_LEVELING = "leveling"
KEY_AUTOMOD = "automod"
KEY_LOGGING = "logging"
KEY_MAIN = "main"

# (page key, button label, emoji, main-panel headline)
SYSTEMS: List[Tuple[str, str, str, str]] = [
    (KEY_TICKETS, "Tickets", "🎫", "🎫 Tickets"),
    (KEY_VERIFICATION, "Verification", "✅", "✅ Verification"),
    (KEY_POLLS, "Polls", "📊", "📊 Polls"),
    (KEY_INVITES, "Invites", "📈", "📈 Invites"),
    (KEY_LEVELING, "Leveling", "⭐", "⭐ Leveling"),
    (KEY_AUTOMOD, "AutoMod", "🛡", "🛡 AutoMod"),
    (KEY_LOGGING, "Logging", "📋", "📋 Logging"),
]
SYSTEMS_BY_KEY: Dict[str, Tuple[str, str, str, str]] = {s[0]: s for s in SYSTEMS}

STATUS_EMOJI = {"ok": "✅", "partial": "⚙️", "missing": "❌", "disabled": "🔘"}
STATUS_TEXT = {
    "ok": "Configured",
    "partial": "Partially configured",
    "missing": "Not set up",
    "disabled": "Disabled",
}

_SKIP_CONFIG_KEYS = {
    "guild_id", "id", "rowid", "created_at", "updated_at",
    "panel_id", "message_id", "ticket_id",
}


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------
def _to_bool(value: Any) -> bool:
    """Coerce SQLite-ish values ('1'/'0'/1/0/True/False/'true'/'false') to bool."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(value)


def _format_value(key: str, value: Any) -> str:
    """Render a single config cell for an embed line."""
    if value is None:
        return "—"
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            return "<binary>"
    if isinstance(value, bool):
        return "✅ yes" if value else "❌ no"
    if isinstance(value, int):
        if "channel" in key and value:
            return f"<#{value}>"
        if "role" in key and value:
            return f"<@&{value}>"
        if key.endswith("_at") and value > 10_000_000_000:
            return botkit.fmt_dt(datetime.fromtimestamp(value, tz=timezone.utc))
        if key in ("enabled", "is_active") or key.endswith("_enabled"):
            return "✅ yes" if value else "❌ no"
        return f"{value}"
    text = str(value).strip()
    if not text:
        return "—"
    if key.endswith("_at"):
        parsed = botkit.parse_iso(text)
        if parsed is not None:
            return botkit.fmt_dt(parsed)
    if text.startswith(("[", "{")):  # JSON list/dict column
        text = " ".join(text.split())
    if len(text) > 120:
        text = text[:117] + "…"
    return text


def _config_lines(row: Optional[sqlite3.Row], *, max_lines: int = 12) -> List[str]:
    """Pretty-print a config row (skips ids / timestamps / empty cells).

    Works against any schema the parallel cogs may ship — unknown columns are
    still displayed generically instead of crashing.
    """
    if row is None:
        return []
    try:
        keys: Sequence[str] = row.keys()
    except Exception:
        return []
    lines: List[str] = []
    for key in keys:
        if key in _SKIP_CONFIG_KEYS or len(lines) >= max_lines:
            continue
        try:
            value = row[key]
        except Exception:
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        pretty = key.replace("_", " ").strip().title()
        lines.append(f"• **{pretty}**: {_format_value(key, value)}")
    return lines


def _first_text(row: sqlite3.Row, candidates: Sequence[str]) -> str:
    """First non-empty column among candidates (schema-drift tolerant)."""
    try:
        keys = set(row.keys())
        for name in candidates:
            if name in keys:
                value = row[name]
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    return text if len(text) <= 60 else text[:57] + "…"
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Interactive panel view
# ---------------------------------------------------------------------------
class _SystemButton(discord.ui.Button):
    """Main-panel button → jumps to a subsystem detail page."""

    def __init__(self, panel: "SetupPanelView", key: str, label: str, emoji: str) -> None:
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)
        self._panel = panel
        self._key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._panel.goto(interaction, self._key)


class _BackButton(discord.ui.Button):
    def __init__(self, panel: "SetupPanelView") -> None:
        super().__init__(label="Back", emoji="◀", style=discord.ButtonStyle.secondary)
        self._panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._panel.goto(interaction, KEY_MAIN)


class _RefreshButton(discord.ui.Button):
    def __init__(self, panel: "SetupPanelView", row: Optional[int] = None) -> None:
        super().__init__(label="Refresh", emoji="🔄", style=discord.ButtonStyle.primary, row=row)
        self._panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._panel.goto(interaction, self._panel.page, force=True)


class _VerificationToggle(discord.ui.Button):
    """Enable/Disable switch — writes verification_config.enabled directly."""

    def __init__(self, panel: "SetupPanelView", enabled: bool) -> None:
        self._new_state = not enabled
        if self._new_state:
            super().__init__(label="Enable Verification", emoji="✅",
                             style=discord.ButtonStyle.success)
        else:
            super().__init__(label="Disable Verification", emoji="⛔",
                             style=discord.ButtonStyle.danger)
        self._panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        result = self._panel.cog.toggle_verification(self._panel.guild_id)
        if result is None:
            await interaction.response.send_message(
                embed=botkit.error(
                    "Cannot toggle verification",
                    "No `verification_config` row exists for this server yet — "
                    "run `!vsetup` first, then use this button.",
                ),
                ephemeral=True,
            )
            return
        state = "enabled" if result else "disabled"
        await interaction.response.send_message(
            embed=botkit.success(f"Verification {state}",
                                 "Written directly to the database. If the running "
                                 "verification cog caches this value, reload it to "
                                 "pick the change up."),
            ephemeral=True,
        )
        # The interaction was consumed by the ephemeral confirmation, so the
        # panel message itself is edited directly (not via the response).
        await self._panel.rebuild_message(interaction, KEY_VERIFICATION)


class SetupPanelView(discord.ui.View):
    """Author-gated interactive panel backing ``!csetup``.

    One persistent message that gets re-edited (embed + view) as the user
    navigates between the status page and per-system detail pages.
    """

    def __init__(self, cog: "SetupCog", guild_id: int, invoker_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.invoker_id = invoker_id
        self.page: str = KEY_MAIN
        self.message: Optional[discord.Message] = None
        self._rebuild()

    # -- security ---------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                embed=botkit.error(
                    "Not your panel",
                    "Only the person who ran `!csetup` can use these controls. "
                    "Run the command yourself to get your own panel.",
                ),
                ephemeral=True,
            )
            return False
        return True

    # -- layout -----------------------------------------------------------
    def _rebuild(self) -> None:
        self.clear_items()
        if self.page == KEY_MAIN:
            for key, label, emoji, _headline in SYSTEMS:
                self.add_item(_SystemButton(self, key, label, emoji))
            self.add_item(_RefreshButton(self, row=2))
        else:
            self.add_item(_BackButton(self))
            if self.page == KEY_VERIFICATION:
                enabled = self.cog.verification_enabled(self.guild_id)
                if enabled is not None:
                    self.add_item(_VerificationToggle(self, enabled))
            self.add_item(_RefreshButton(self))

    # -- navigation -------------------------------------------------------
    async def goto(self, interaction: discord.Interaction, page: str, *, force: bool = False) -> None:
        """Re-render the panel message for ``page`` (refresh if same page)."""
        if page != self.page or force:
            self.page = page
        embed = self.cog.build_embed(self.guild_id, self.page)
        self._rebuild()
        try:
            await interaction.response.edit_message(embed=embed, view=self)
            return
        except discord.InteractionResponded:
            # The callback already consumed the response (e.g. the ephemeral
            # confirmation in the verification toggle) — edit the message
            # directly instead.
            pass
        except discord.HTTPException as exc:
            log.warning("[Setup] panel edit failed: %s", exc)
        await self._edit_message_directly(interaction, embed)

    async def rebuild_message(self, interaction: discord.Interaction, page: str) -> None:
        """Re-render the panel for ``page`` after the response was consumed."""
        self.page = page
        embed = self.cog.build_embed(self.guild_id, self.page)
        self._rebuild()
        await self._edit_message_directly(interaction, embed)

    async def _edit_message_directly(self, interaction: discord.Interaction,
                                     embed: discord.Embed) -> None:
        message = self.message
        if message is None:
            message = getattr(interaction, "message", None)
            if message is not None:
                self.message = message
        if message is None:
            return
        try:
            await message.edit(embed=embed, view=self)
        except discord.HTTPException as exc:
            log.warning("[Setup] direct panel edit failed: %s", exc)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass  # message deleted / ephemeral token expired — nothing to do


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class SetupCog(commands.Cog, name="FactionSetup"):
    """Guided FactionBot setup (`!csetup`) and global settings (`!settings`)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Defensive config access (bot.fb_config is wired by the core in Task 8)
    # ------------------------------------------------------------------
    def _fb_conf(self) -> Any:
        cfg = getattr(self.bot, "fb_config", None)
        if cfg is None:
            cfg = getattr(self.bot, "config", None)
        return cfg

    def _conf_get(self, section: str, attr: str, default: Any = None) -> Any:
        cfg = self._fb_conf()
        if cfg is None:
            return default
        sec = getattr(cfg, section, None)
        if sec is None:
            return default
        value = getattr(sec, attr, None)
        return default if value is None else value

    def _conf_channel(self, attr: str) -> Optional[int]:
        value = self._conf_get("channels", attr, None)
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def _ows_flag(self, key: str) -> bool:
        """Owner-settings toggle (bot_config table, 'ows_' prefixed keys)."""
        try:
            row = botkit.fetchone("SELECT value FROM bot_config WHERE key = ?", (f"ows_{key}",))
        except sqlite3.Error as exc:
            log.debug("[Setup] ows read %s failed: %s", key, exc)
            return True
        if row is None:
            return True
        return _to_bool(row[0])

    def _prefix(self) -> str:
        value = getattr(self.bot, "command_prefix", None)
        if isinstance(value, str) and value:
            return value
        value = getattr(self._fb_conf(), "command_prefix", None)
        if isinstance(value, str) and value:
            return value
        return "!"

    # ------------------------------------------------------------------
    # Generic DB helpers (tables owned by other cogs may not exist yet)
    # ------------------------------------------------------------------
    def _count(self, table: str, guild_id: int) -> Optional[int]:
        try:
            row = botkit.fetchone(f"SELECT COUNT(*) AS n FROM {table} WHERE guild_id = ?", (guild_id,))
            return int(row[0]) if row is not None else 0
        except sqlite3.Error as exc:
            log.debug("[Setup] COUNT %s failed: %s", table, exc)
            return None

    def _get_row(self, table: str, guild_id: int) -> Optional[sqlite3.Row]:
        try:
            return botkit.fetchone(f"SELECT * FROM {table} WHERE guild_id = ? LIMIT 1", (guild_id,))
        except sqlite3.Error as exc:
            log.debug("[Setup] SELECT %s failed: %s", table, exc)
            return None

    def _row_exists(self, table: str, guild_id: int) -> bool:
        try:
            return botkit.fetchone(
                f"SELECT 1 FROM {table} WHERE guild_id = ? LIMIT 1", (guild_id,)
            ) is not None
        except sqlite3.Error as exc:
            log.debug("[Setup] EXISTS %s failed: %s", table, exc)
            return False

    def _get_row_col(self, table: str, guild_id: int, column: str) -> Optional[Any]:
        try:
            row = botkit.fetchone(
                f"SELECT {column} AS v FROM {table} WHERE guild_id = ? LIMIT 1", (guild_id,)
            )
            return row["v"] if row is not None else None
        except sqlite3.Error as exc:
            log.debug("[Setup] SELECT %s.%s failed: %s", table, column, exc)
            return None

    # ------------------------------------------------------------------
    # Per-subsystem status: ("ok"|"partial"|"missing"|"disabled", note)
    # ------------------------------------------------------------------
    def _tickets_state(self, guild_id: int) -> Tuple[str, str]:
        if not self._ows_flag("enable_tickets"):
            return "disabled", "Disabled — re-enable with `!ows`"
        panels = self._count("ticket_panels", guild_id)
        panels = 0 if panels is None else panels
        has_settings = self._row_exists("ticket_settings", guild_id)
        if panels > 0 and has_settings:
            return "ok", f"{panels} panel(s) + global settings"
        if panels > 0 or has_settings:
            missing = "global settings (`!ticketsettings`)" if not has_settings else "a panel (`!panel`)"
            return "partial", f"{panels} panel(s) — still missing {missing}"
        return "missing", "no panels yet — run `!panel`"

    def _verification_row(self, guild_id: int) -> Optional[sqlite3.Row]:
        return self._get_row("verification_config", guild_id)

    def _verification_roles_set(self, row: sqlite3.Row) -> bool:
        try:
            keys = list(row.keys())
        except Exception:
            return False
        for key in keys:
            if "role" not in key or key == "guild_id":
                continue
            value = row[key]
            if isinstance(value, int) and value:
                return True
            if isinstance(value, str):
                if value.strip() and value.strip().lower() not in ("0", "false", "none", "[]", "{}"):
                    return True
        return False

    def verification_enabled(self, guild_id: int) -> Optional[bool]:
        """None = no config row / table; otherwise the enabled flag."""
        row = self._verification_row(guild_id)
        if row is None:
            return None
        try:
            keys = row.keys()
            for candidate in ("enabled", "is_enabled", "active"):
                if candidate in keys:
                    return _to_bool(row[candidate])
        except Exception:
            pass
        return None

    def _verification_state(self, guild_id: int) -> Tuple[str, str]:
        if not self._ows_flag("enable_verification"):
            return "disabled", "Disabled — re-enable with `!ows`"
        row = self._verification_row(guild_id)
        if row is None:
            return "missing", "no configuration — run `!vsetup`"
        enabled = self.verification_enabled(guild_id)
        if enabled is False:
            return "disabled", "configured but switched off"
        if self._verification_roles_set(row):
            return "ok", "roles + settings configured"
        return "partial", "configured but no roles assigned"

    def _poll_counts(self, guild_id: int) -> Tuple[int, Optional[int]]:
        total = self._count("polls", guild_id)
        total = 0 if total is None else total
        active: Optional[int] = None
        # Column names tried in order: the polls cog uses status='active'
        # ('ended'/'cancelled' when finished); older/other schemas may use
        # is_active or closed_at — any miss falls through to the next.
        for condition in ("status = 'active'", "is_active = 1", "closed_at IS NULL"):
            try:
                row = botkit.fetchone(
                    f"SELECT COUNT(*) AS n FROM polls WHERE guild_id = ? AND {condition}",
                    (guild_id,))
                active = int(row[0]) if row is not None else 0
                break
            except sqlite3.Error:
                continue
        return total, active

    def _polls_state(self, guild_id: int) -> Tuple[str, str]:
        total, active = self._poll_counts(guild_id)
        active_text = f", {active} active" if active is not None else ""
        if total > 0:
            return "ok", f"{total} poll(s){active_text}"
        return "missing", "no polls yet — `!poll create 60 Question | Yes | No`"

    def _invites_state(self, guild_id: int) -> Tuple[str, str]:
        if not self._ows_flag("enable_invite_tracking"):
            return "disabled", "Disabled — re-enable with `!ows`"
        has_config = self._row_exists("invite_config", guild_id)
        stats = self._count("invite_stats", guild_id)
        stats = 0 if stats is None else stats
        if has_config:
            extra = f" · {stats} tracked member(s)" if stats else " · no tracking data yet"
            return "ok", "configured via `!invites setup`" + extra
        if stats > 0:
            return "partial", f"{stats} tracked member(s) but no config row — run `!invites setup`"
        return "missing", "run `!invites setup`"

    def _leveling_state(self, guild_id: int) -> Tuple[str, str]:
        if not self._ows_flag("enable_leveling"):
            return "disabled", "Disabled — re-enable with `!ows`"
        has_config = self._row_exists("level_config", guild_id)
        ranked = self._count("levels", guild_id)
        ranked = 0 if ranked is None else ranked
        if has_config:
            return "ok", f"configured · {ranked} ranked member(s)"
        if ranked > 0:
            return "partial", f"{ranked} ranked member(s) but no config row — run `!level config`"
        return "missing", "run `!level config`"

    def _automod_state(self, guild_id: int) -> Tuple[str, str]:
        rules = self._count("automod_rules", guild_id)
        rules = 0 if rules is None else rules
        if rules > 0:
            return "ok", f"{rules} rule(s) active"
        return "missing", "no rules — `!automod add words delete badword1`"

    def _logging_sources(self, guild_id: int) -> List[Tuple[str, Optional[int]]]:
        sources: List[Tuple[str, Optional[int]]] = []
        v_log = self._get_row_col("verification_config", guild_id, "log_channel_id")
        try:
            v_log = int(v_log) if v_log else None
        except (TypeError, ValueError):
            v_log = None
        sources.append(("verification", v_log))
        a_log = self._get_row_col("automod_config", guild_id, "log_channel_id")
        try:
            a_log = int(a_log) if a_log else None
        except (TypeError, ValueError):
            a_log = None
        sources.append(("automod", a_log))
        sources.append(("core", self._conf_channel("log")))
        return sources

    def _logging_state(self, guild_id: int) -> Tuple[str, str]:
        sources = self._logging_sources(guild_id)
        set_names = [name for name, channel_id in sources if channel_id]
        if len(set_names) >= 2:
            return "ok", "log channels: " + " · ".join(set_names)
        if len(set_names) == 1:
            return "partial", f"only {set_names[0]} log channel set"
        return "missing", "no log channels set"

    # ------------------------------------------------------------------
    # Verification master switch (direct DB write, per spec)
    # ------------------------------------------------------------------
    def toggle_verification(self, guild_id: int) -> Optional[bool]:
        """Flip verification_config.enabled; returns the new state or None."""
        try:
            row = botkit.fetchone(
                "SELECT enabled AS v FROM verification_config WHERE guild_id = ?",
                (guild_id,))
            if row is None:
                return None
            current = _to_bool(row["v"])
            new_value = 0 if current else 1
            botkit.run(
                "UPDATE verification_config SET enabled = ? WHERE guild_id = ?",
                (new_value, guild_id))
            log.info("[Setup] verification toggled to %s for guild %s", new_value, guild_id)
            return bool(new_value)
        except sqlite3.Error as exc:
            log.warning("[Setup] verification toggle failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Embed builders
    # ------------------------------------------------------------------
    def build_embed(self, guild_id: int, page: str) -> discord.Embed:
        try:
            if page == KEY_MAIN or page not in SYSTEMS_BY_KEY:
                return self._main_embed(guild_id)
            builder = {
                KEY_TICKETS: self._tickets_detail,
                KEY_VERIFICATION: self._verification_detail,
                KEY_POLLS: self._polls_detail,
                KEY_INVITES: self._invites_detail,
                KEY_LEVELING: self._leveling_detail,
                KEY_AUTOMOD: self._automod_detail,
                KEY_LOGGING: self._logging_detail,
            }[page]
            return builder(guild_id)
        except Exception:
            log.exception("[Setup] failed to build %s embed for guild %s", page, guild_id)
            return botkit.error(
                "Setup panel error",
                "Could not read the configuration for this page. "
                "The relevant cog or its tables may not be loaded yet — try Refresh.",
            )

    def _main_embed(self, guild_id: int) -> discord.Embed:
        states = {
            KEY_TICKETS: self._tickets_state(guild_id),
            KEY_VERIFICATION: self._verification_state(guild_id),
            KEY_POLLS: self._polls_state(guild_id),
            KEY_INVITES: self._invites_state(guild_id),
            KEY_LEVELING: self._leveling_state(guild_id),
            KEY_AUTOMOD: self._automod_state(guild_id),
            KEY_LOGGING: self._logging_state(guild_id),
        }
        lines: List[str] = []
        for key, _label, _emoji, headline in SYSTEMS:
            state_key, note = states[key]
            icon = STATUS_EMOJI[state_key]
            lines.append(f"{headline} {icon} {note}")
        description = "\n".join(lines)
        description += (
            "\n\nPress a button below for per-system setup steps, or 🔄 to re-check. "
            f"This panel closes after {int(self._panel_timeout() // 60)} minutes."
        )
        embed = botkit.neutral("🛠 FactionBot Setup", description)
        embed.add_field(
            name="Quick start",
            value=(
                "`!panel` tickets · `!vsetup` verification · `!poll create 60 Question | Yes | No`\n"
                "`!invites setup` · `!level config` · `!automod add words delete badword1`"
            ),
            inline=False,
        )
        embed.set_footer(text=(
            "Every system has its own commands — the buttons above show the exact "
            "next steps for each one."
        ))
        return embed

    @staticmethod
    def _panel_timeout() -> float:
        return 300.0

    def _status_header(self, guild_id: int, key: str) -> str:
        state_key, note = {
            KEY_TICKETS: self._tickets_state,
            KEY_VERIFICATION: self._verification_state,
            KEY_POLLS: self._polls_state,
            KEY_INVITES: self._invites_state,
            KEY_LEVELING: self._leveling_state,
            KEY_AUTOMOD: self._automod_state,
            KEY_LOGGING: self._logging_state,
        }[key](guild_id)
        return f"{STATUS_EMOJI[state_key]} **{STATUS_TEXT[state_key]}** — {note}"

    def _detail_embed(self, key: str, guild_id: int, description: str,
                      next_steps: List[str]) -> discord.Embed:
        _key, label, emoji, _headline = SYSTEMS_BY_KEY[key]
        embed = botkit.neutral(f"{emoji} {label} — Setup")
        embed.description = f"{self._status_header(guild_id, key)}\n{description}".strip()
        embed.add_field(name="🚀 Next steps", value="\n".join(next_steps), inline=False)
        return embed

    # -- tickets ---------------------------------------------------------
    def _tickets_detail(self, guild_id: int) -> discord.Embed:
        description = "\n"
        panel_lines: List[str] = []
        try:
            rows = botkit.fetchall(
                "SELECT name, channel_id, is_active FROM ticket_panels "
                "WHERE guild_id = ? ORDER BY rowid DESC LIMIT 10",
                (guild_id,))
            for row in rows:
                name = (row["name"] or "Unnamed panel")[:40]
                channel = f"<#{row['channel_id']}>" if row["channel_id"] else "unknown channel"
                state = "active" if _to_bool(row["is_active"]) else "inactive"
                panel_lines.append(f"• **{name}** — {channel} ({state})")
        except sqlite3.Error as exc:
            log.debug("[Setup] panel list failed: %s", exc)
        if panel_lines:
            total = self._count("ticket_panels", guild_id) or 0
            description += f"**Panels ({total}):**\n" + "\n".join(panel_lines) + "\n"
        settings_lines = _config_lines(self._get_row("ticket_settings", guild_id), max_lines=10)
        if settings_lines:
            description += "\n**Global settings (`ticket_settings`):**\n" + "\n".join(settings_lines)
        return self._detail_embed(
            KEY_TICKETS, guild_id, description,
            [
                "1. `!panel` — create a ticket panel (interactive builder)",
                "2. `!ticketsettings` — global ticket settings (limits, roles, closing)",
                "3. `!multipanel` · `!dropdownpanel` · `!reactionpanel` — advanced panels",
                "4. `!ticketlog` — where ticket events get logged",
            ],
        )

    # -- verification ----------------------------------------------------
    def _verification_detail(self, guild_id: int) -> discord.Embed:
        row = self._verification_row(guild_id)
        description = "\n"
        lines = _config_lines(row, max_lines=12)
        if lines:
            description += "**Current configuration:**\n" + "\n".join(lines)
        else:
            description += (
                "No `verification_config` row found yet. The configuration is created "
                "by the verification wizard."
            )
        steps = [
            "1. `!vsetup` (or `!verification setup`) — run the verification wizard",
            "2. Assign the verified/unverified roles and a log channel in the wizard",
        ]
        enabled = self.verification_enabled(guild_id)
        if enabled is not None:
            steps.append(
                "3. Use the Enable/Disable button on this page to flip the master switch"
            )
        embed = self._detail_embed(KEY_VERIFICATION, guild_id, description, steps)
        if enabled is not None:
            embed.set_footer(text=(
                "The toggle button writes verification_config.enabled directly to the "
                "database — if the running cog caches it, reload the cog to apply."
            ))
        return embed

    # -- polls -----------------------------------------------------------
    def _polls_detail(self, guild_id: int) -> discord.Embed:
        total, active = self._poll_counts(guild_id)
        description = "\n**Overview:**\n"
        description += f"• **Total polls**: {total}\n"
        if active is not None:
            description += f"• **Active polls**: {active}\n"
        try:
            rows = botkit.fetchall(
                "SELECT * FROM polls WHERE guild_id = ? ORDER BY rowid DESC LIMIT 5",
                (guild_id,))
            recent: List[str] = []
            for row in rows:
                label = _first_text(row, ("question", "title", "name", "topic", "subject"))
                if label:
                    created = ""
                    try:
                        if "created_at" in row.keys() and row["created_at"]:
                            created = " — " + botkit.fmt_dt(botkit.parse_iso(str(row["created_at"])))
                    except Exception:
                        pass
                    recent.append(f"• {label}{created}")
            if recent:
                description += "\n**Recent polls:**\n" + "\n".join(recent)
        except sqlite3.Error as exc:
            log.debug("[Setup] poll list failed: %s", exc)
        return self._detail_embed(
            KEY_POLLS, guild_id, description,
            [
                "1. `!poll create 60 Question | Yes | No` — create a 60-second poll",
                "2. `!poll` — poll lifecycle commands (end, results, list)",
            ],
        )

    # -- invites ---------------------------------------------------------
    def _invites_detail(self, guild_id: int) -> discord.Embed:
        description = "\n"
        config_lines = _config_lines(self._get_row("invite_config", guild_id), max_lines=12)
        stats = self._count("invite_stats", guild_id)
        stats = 0 if stats is None else stats
        description += f"**Tracked members (invite_stats):** {stats}\n"
        if config_lines:
            description += "\n**Current configuration:**\n" + "\n".join(config_lines)
        else:
            description += "\nNo `invite_config` row yet — run the setup command below."
        top: List[str] = []
        # Candidate schemas in order — the invites cog stores (user_id, real);
        # older/other layouts may use inviter_id/uses.
        for sql in (
            "SELECT user_id AS uid, real AS n FROM invite_stats WHERE guild_id = ? AND real > 0 ORDER BY real DESC LIMIT 5",
            "SELECT inviter_id AS uid, uses AS n FROM invite_stats WHERE guild_id = ? ORDER BY uses DESC LIMIT 5",
            "SELECT user_id AS uid, uses AS n FROM invite_stats WHERE guild_id = ? ORDER BY uses DESC LIMIT 5",
        ):
            try:
                rows = botkit.fetchall(sql, (guild_id,))
                for row in rows:
                    top.append(f"• <@{row['uid']}> — **{row['n']}** use(s)")
                break
            except sqlite3.Error:
                continue
        if top:
            description += "\n\n**Top inviters:**\n" + "\n".join(top)
        return self._detail_embed(
            KEY_INVITES, guild_id, description,
            [
                "1. `!invites setup` — configure invite tracking for this server",
                "2. `!invites` — leaderboard / stats commands",
            ],
        )

    # -- leveling --------------------------------------------------------
    def _leveling_detail(self, guild_id: int) -> discord.Embed:
        description = "\n"
        config_lines = _config_lines(self._get_row("level_config", guild_id), max_lines=12)
        ranked = self._count("levels", guild_id)
        ranked = 0 if ranked is None else ranked
        description += f"**Ranked members:** {ranked}\n"
        if config_lines:
            description += "\n**Current configuration:**\n" + "\n".join(config_lines)
        else:
            description += "\nNo `level_config` row yet — run the config command below."
        top: List[str] = []
        try:
            rows = botkit.fetchall(
                "SELECT user_id, xp, level FROM levels WHERE guild_id = ? "
                "ORDER BY xp DESC LIMIT 5",
                (guild_id,))
            for row in rows:
                top.append(f"• <@{row['user_id']}> — level **{row['level']}** ({row['xp']} XP)")
        except sqlite3.Error as exc:
            log.debug("[Setup] level top failed: %s", exc)
        if top:
            description += "\n\n**Top members:**\n" + "\n".join(top)
        return self._detail_embed(
            KEY_LEVELING, guild_id, description,
            [
                "1. `!level config` — open the leveling configuration",
                "2. `!level reward add <level> <@role>` — attach a role reward",
                "3. `!level` — stats, leaderboard and reward commands",
            ],
        )

    # -- automod ---------------------------------------------------------
    def _automod_detail(self, guild_id: int) -> discord.Embed:
        description = "\n"
        rules = self._count("automod_rules", guild_id)
        rules = 0 if rules is None else rules
        description += f"**Rules:** {rules}\n"
        rule_lines: List[str] = []
        try:
            rows = botkit.fetchall(
                "SELECT * FROM automod_rules WHERE guild_id = ? ORDER BY rowid DESC LIMIT 10",
                (guild_id,))
            for row in rows:
                label = _first_text(row, ("name", "rule_name", "label", "trigger", "pattern", "keyword"))
                extra = _first_text(row, ("action", "type", "rule_type"))
                text = f"• **{label or 'rule'}**"
                if extra:
                    text += f" — {extra}"
                rule_lines.append(text)
        except sqlite3.Error as exc:
            log.debug("[Setup] automod rules failed: %s", exc)
        if rule_lines:
            description += "\n**Latest rules:**\n" + "\n".join(rule_lines)
        config_lines = _config_lines(self._get_row("automod_config", guild_id), max_lines=8)
        if config_lines:
            description += "\n\n**Current configuration:**\n" + "\n".join(config_lines)
        return self._detail_embed(
            KEY_AUTOMOD, guild_id, description,
            [
                "1. `!automod add words delete badword1` — first rule (auto-delete on a word)",
                "2. `!automod config log_channel #log` — send automod hits to a log channel",
                "3. `!automod` — rule list and other rule types",
            ],
        )

    # -- logging ---------------------------------------------------------
    def _logging_detail(self, guild_id: int) -> discord.Embed:
        lines: List[str] = []
        for name, channel_id in self._logging_sources(guild_id):
            if channel_id:
                lines.append(f"• **{name.title()} log**: <#{channel_id}>")
            else:
                lines.append(f"• **{name.title()} log**: — not set")
        description = "\n" + "\n".join(lines)
        return self._detail_embed(
            KEY_LOGGING, guild_id, description,
            [
                "1. Verification log — set it in `!vsetup`",
                "2. AutoMod log — `!automod config log_channel #log`",
                "3. Core bot log — `!channelsetup` or `!setchannel log #log`",
            ],
        )

    # ------------------------------------------------------------------
    # !csetup — the guided setup panel
    # ------------------------------------------------------------------
    @commands.command(
        name="csetup",
        description="Guided FactionBot setup — interactive status panel with next steps",
    )
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def csetup(self, ctx: commands.Context) -> None:
        """Interactive setup status panel (author-only buttons)."""
        guild = ctx.guild
        if guild is None:  # belt & braces — guild_only() should cover both faces
            await ctx.send(
                embed=botkit.error("Server required", "Run `!csetup` inside a server."),
                ephemeral=True,
            )
            return
        view = SetupPanelView(self, guild.id, ctx.author.id)
        embed = self.build_embed(guild.id, KEY_MAIN)
        try:
            view.message = await ctx.send(embed=embed, view=view)
        except discord.HTTPException as exc:
            log.warning("[Setup] could not send csetup panel: %s", exc)
            return
        log.info("[Setup] csetup panel opened in guild %s by %s", guild.id, ctx.author.id)

    # ------------------------------------------------------------------
    # !settings — global config summary
    # ------------------------------------------------------------------
    @commands.command(
        name="settings",
        description="Global FactionBot config summary (channels, roles, limits, toggles)",
    )
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def settings(self, ctx: commands.Context) -> None:
        """Show the global config stored on the bot (defensively read)."""
        try:
            await ctx.send(embed=self._settings_embed(ctx))
        except discord.HTTPException as exc:
            log.warning("[Setup] could not send settings: %s", exc)

    # -- settings helpers -------------------------------------------------
    def _channel_mention(self, channel_id: Any) -> str:
        try:
            channel_id = int(channel_id) if channel_id else 0
        except (TypeError, ValueError):
            channel_id = 0
        if not channel_id:
            return "Not set"
        channel = self.bot.get_channel(channel_id)
        return channel.mention if channel is not None else f"`{channel_id}` (not found)"

    def _role_mention(self, guild: Optional[discord.Guild], role_id: Any) -> str:
        try:
            role_id = int(role_id) if role_id else 0
        except (TypeError, ValueError):
            role_id = 0
        if not role_id:
            return "Not set"
        if guild is not None:
            role = guild.get_role(role_id)
            if role is not None:
                return role.mention
        return f"`{role_id}`"

    def _feature_flag(self, name: str) -> bool:
        value = getattr(self._fb_conf(), name, None)
        if value is not None:
            return _to_bool(value)
        return self._ows_flag(name)

    def _settings_embed(self, ctx: commands.Context) -> discord.Embed:
        guild = ctx.guild
        p = self._prefix()

        channel_lines = []
        for attr, label in (
            ("welcome", "Welcome"),
            ("rules", "Rules"),
            ("log", "Log"),
            ("verification_main", "Verification main"),
            ("reports", "Reports"),
            ("tickets", "Tickets"),
            ("transcripts", "Transcripts"),
        ):
            channel_lines.append(f"• **{label}**: {self._channel_mention(self._conf_get('channels', attr))}")

        role_lines = []
        for attr, label in (("member", "Member"), ("staff", "Staff"), ("verified", "Verified")):
            role_lines.append(f"• **{label}**: {self._role_mention(guild, self._conf_get('roles', attr))}")

        def _limit(attr: str) -> str:
            value = self._conf_get("limits", attr, None)
            if value is None:
                return "Not set"
            if attr.endswith("duration"):
                return botkit.fmt_duration(int(value))
            return str(value)

        def _range(attr_min: str, attr_max: str, unit: str = "") -> str:
            low, high = self._conf_get("limits", attr_min, None), self._conf_get("limits", attr_max, None)
            if low is None and high is None:
                return "Not set"
            return f"{low if low is not None else '?'} – {high if high is not None else '?'}{unit}"

        age = self._conf_get("limits", "min_account_age_days", None)
        age_line = f"{age} day(s)" if age is not None else "Not set"
        limit_lines = [
            f"• **Min account age**: {age_line}",
            f"• **Max tickets per user**: {_limit('max_tickets_per_user')}",
            f"• **Poll options**: {_range('min_poll_options', 'max_poll_options')}",
            f"• **Poll duration**: {_range('min_poll_duration', 'max_poll_duration')}",
            f"• **Warnings before ban**: {_limit('max_warnings_before_ban')}",
        ]

        toggle_lines = []
        for attr, label in (
            ("enable_leveling", "Leveling"),
            ("enable_tickets", "Tickets"),
            ("enable_warnings", "Warnings"),
        ):
            state = "✅ enabled" if self._feature_flag(attr) else "❌ disabled"
            toggle_lines.append(f"• **{label}**: {state}")

        embed = botkit.neutral("⚙️ FactionBot Settings", "Global configuration (shared by every guild).")
        embed.add_field(name="📢 Channels", value="\n".join(channel_lines), inline=False)
        embed.add_field(name="👥 Roles", value="\n".join(role_lines), inline=False)
        embed.add_field(name="🔢 Limits", value="\n".join(limit_lines), inline=False)
        embed.add_field(name="🧩 Features", value="\n".join(toggle_lines), inline=False)
        embed.add_field(name="⌨️ Prefix", value=f"`{p}`", inline=False)
        embed.set_footer(text=(
            f"Change these with {p}channelsetup · {p}rolesetup · {p}timingsetup · "
            f"{p}limitssetup — owner-level toggles: {p}ows"
        ))
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
