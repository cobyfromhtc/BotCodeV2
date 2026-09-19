# -*- coding: utf-8 -*-
"""modules/support/help.py — dynamic FactionBot help system.

``!help`` (aliases ``!cmds``, ``!commands``, ``!h``; also ``!help``) replaces
the disabled default help command and the removed legacy ``!cmds``:

* no arguments     → category overview with a select dropdown
* ``!help <cmd>``  → detail page (signature, description, category, aliases,
                     cooldown, permission hints, group subcommands)
* ``!help <cat>``  → fuzzy-matched category page, 15 commands per page with
                     ⬅ / ➡ paging

Everything is computed from the LIVE command set at call time (never cached
at startup), so it keeps working as cogs are added or removed. Pages are sent
as regular messages. Components are gated to the invoking user and time out after 180s.
"""
from __future__ import annotations

import difflib
import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import discord
from discord.ext import commands

from utils import botkit  # relocated into utils/ by the SaaS restructure

log = logging.getLogger(__name__)

PAGE_SIZE = 15

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
CATEGORY_VERIFICATION = "✅ Verification"
CATEGORY_POLLS = "📊 Polls"
CATEGORY_INVITES = "📈 Invites"
CATEGORY_LEVELING = "⭐ Leveling"
CATEGORY_AUTOMOD = "🛡 AutoMod"
CATEGORY_TICKETS = "🎫 Tickets"
CATEGORY_REACTION = "🏷 Reaction Roles"
CATEGORY_MODERATION = "🛡 Moderation"
CATEGORY_ADMIN = "🛠 Setup & Admin"
CATEGORY_SETUP = "🛠 Setup"
CATEGORY_HELP = "❓ Help"
CATEGORY_UTILITY = "⚙️ Utility & Misc"

ALL_CATEGORIES: List[str] = [
    CATEGORY_VERIFICATION,
    CATEGORY_POLLS,
    CATEGORY_INVITES,
    CATEGORY_LEVELING,
    CATEGORY_AUTOMOD,
    CATEGORY_TICKETS,
    CATEGORY_REACTION,
    CATEGORY_MODERATION,
    CATEGORY_ADMIN,
    CATEGORY_SETUP,
    CATEGORY_HELP,
    CATEGORY_UTILITY,
]

# New subsystem cogs (loaded in parallel) → pretty category names.
COG_CATEGORY_MAP: Dict[str, str] = {
    "Verification": CATEGORY_VERIFICATION,
    "Polls": CATEGORY_POLLS,
    "InviteTracking": CATEGORY_INVITES,
    "Leveling": CATEGORY_LEVELING,
    "AutoMod": CATEGORY_AUTOMOD,
    "FactionSetup": CATEGORY_SETUP,
    "FactionHelp": CATEGORY_HELP,
}

# Ticket commands (by name — the legacy ticket system lives in the core and
# in the TicketTool package, so module detection alone is not enough).
TICKET_NAMES: Set[str] = {
    "naming", "schedule", "claimconfig", "roleauto", "automate", "automatelist",
    "automatedelete", "escalate", "escalateroute", "escalationhistory",
    "transcript", "transcriptconfig", "transcriptconfig2", "slaconfig",
    "slareport", "analytics", "csat", "staffstats", "staffanalytics", "export",
    "kb", "canned", "flow", "flowattach", "flowdelete", "flowlist",
    "flowapplication", "flowreviewconfig", "reviewdecision", "reviewpending",
    "panel", "panels", "multipanel", "dropdownpanel", "reactionpanel",
    "panelquestion", "panelembed", "panelembedenable", "panelembedlist",
    "panelembedremove", "panelupdate", "new", "ticket", "tickets", "ticket-info",
    "claim", "unclaim", "close", "closerequest", "add", "remove", "rename",
    "move", "priority", "note", "notes", "pause", "resume", "private",
    "unprivate", "rate", "reopen", "deletepanel", "setcategory", "tcategory",
    "channelrecycle", "ticketsettings", "ticketlog", "ticketstats",
    "ticketdebug", "ticketblacklist", "ticketunblacklist", "tickethelp",
    "threadtickets", "staffthread", "limitbypass", "abrev",
}

# Moderation (by name; "unban"/"mute"/"unmute"/"softban" included only if the
# core registers them — unknown names simply never match).
MOD_NAMES: Set[str] = {
    "kick", "ban", "unban", "softban", "warn", "warnings", "clearwarnings",
    "purge", "nopurge", "mute", "unmute", "tempmute", "blacklist",
    "blacklistlist", "blacklistscan", "unblacklist", "checkprofile",
    "securitycheck", "msglog", "modmessage", "modmessagelist",
    "modmessageremove", "stickyrole",
    # verified to exist in Bot.py and clearly moderation:
    "banid", "lock", "unlock", "slowmode", "auditlog",
}

# Setup & admin (by name).
ADMIN_NAMES: Set[str] = {
    "csetup", "settings", "channelsetup", "rolesetup", "timingsetup",
    "limitssetup", "ows", "sync", "dbcleanup", "shutdown", "permissionlevel",
    "locale", "localelist", "localestring", "tutorial", "branding",
    "botbranding", "customcommand", "customcommandlist", "customcommandremove",
    # verified to exist in Bot.py and clearly admin/config:
    "setchannel", "botstatus",
}

RR_NAMES: Set[str] = {"reactionrole", "getallroles"}

_CATEGORY_KEYWORDS: Dict[str, str] = {
    "verify": CATEGORY_VERIFICATION, "verification": CATEGORY_VERIFICATION,
    "poll": CATEGORY_POLLS, "polls": CATEGORY_POLLS, "vote": CATEGORY_POLLS,
    "voting": CATEGORY_POLLS,
    "invite": CATEGORY_INVITES, "invites": CATEGORY_INVITES,
    "invitation": CATEGORY_INVITES, "invite tracking": CATEGORY_INVITES,
    "level": CATEGORY_LEVELING, "levels": CATEGORY_LEVELING,
    "leveling": CATEGORY_LEVELING, "xp": CATEGORY_LEVELING,
    "automod": CATEGORY_AUTOMOD, "auto": CATEGORY_AUTOMOD,
    "filter": CATEGORY_AUTOMOD, "filters": CATEGORY_AUTOMOD,
    "ticket": CATEGORY_TICKETS, "tickets": CATEGORY_TICKETS,
    "support": CATEGORY_TICKETS,
    "reaction": CATEGORY_REACTION, "reactions": CATEGORY_REACTION,
    "rr": CATEGORY_REACTION, "reactionrole": CATEGORY_REACTION,
    "reaction roles": CATEGORY_REACTION,
    "mod": CATEGORY_MODERATION, "moderation": CATEGORY_MODERATION,
    "punishment": CATEGORY_MODERATION, "punishments": CATEGORY_MODERATION,
    "admin": CATEGORY_ADMIN, "admins": CATEGORY_ADMIN,
    "configuration": CATEGORY_ADMIN, "config": CATEGORY_ADMIN,
    "setup": CATEGORY_SETUP, "settings": CATEGORY_SETUP,
    "help": CATEGORY_HELP, "commands": CATEGORY_HELP,
    "utility": CATEGORY_UTILITY, "utilities": CATEGORY_UTILITY,
    "misc": CATEGORY_UTILITY, "other": CATEGORY_UTILITY, "others": CATEGORY_UTILITY,
}


def _normalize(text: str) -> str:
    """Lowercase + strip emoji/punctuation so '🎫 Tickets' ~ 'tickets'."""
    text = re.sub(r"[^\w& ]", " ", text or "")
    return " ".join(text.lower().split())


def _split_emoji(name: str) -> Tuple[str, str]:
    """('🎫 Tickets') → ('🎫', 'Tickets'); ('Plain') → ('', 'Plain')."""
    parts = name.split(" ", 1)
    if len(parts) == 2 and parts[0] and not parts[0].isascii():
        return parts[0], parts[1].strip()
    return "", name


def _first_line(text: Optional[str], limit: int = 90) -> str:
    if not text:
        return ""
    line = str(text).strip().split("\n", 1)[0].strip()
    if len(line) > limit:
        line = line[: limit - 1] + "…"
    return line


# ---------------------------------------------------------------------------
# Interactive view (select + paging, author-gated)
# ---------------------------------------------------------------------------
class _CategorySelect(discord.ui.Select):
    def __init__(self, view: "HelpView", options: List[discord.SelectOption]) -> None:
        super().__init__(
            placeholder="Choose a category…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self._help_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values:
            await self._help_view.select_category(interaction, self.values[0])


class _PrevButton(discord.ui.Button):
    def __init__(self, view: "HelpView", disabled: bool) -> None:
        super().__init__(label="Prev", emoji="⬅", style=discord.ButtonStyle.secondary,
                         disabled=disabled)
        self._help_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._help_view.step_page(interaction, -1)


class _NextButton(discord.ui.Button):
    def __init__(self, view: "HelpView", disabled: bool) -> None:
        super().__init__(label="Next", emoji="➡", style=discord.ButtonStyle.secondary,
                         disabled=disabled)
        self._help_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._help_view.step_page(interaction, +1)


class _HomeButton(discord.ui.Button):
    def __init__(self, view: "HelpView") -> None:
        super().__init__(label="All categories", emoji="🏠", style=discord.ButtonStyle.secondary)
        self._help_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._help_view.go_home(interaction)


class HelpView(discord.ui.View):
    """Select-a-category dropdown + ⬅/➡ paging, gated to the invoker."""

    def __init__(self, cog: "HelpCog", author_id: int,
                 category: Optional[str] = None, page: int = 0) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.category = category
        self.page = page
        self.message: Optional[discord.Message] = None
        self._rebuild()

    # -- security ---------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=botkit.error(
                    "Not your help session",
                    "Only the person who ran the help command can use this menu — "
                    "run `!help` yourself to get your own.",
                ),
                ephemeral=True,
            )
            return False
        return True

    # -- layout -----------------------------------------------------------
    def _rebuild(self) -> None:
        self.clear_items()
        cats = self.cog.categorized()
        if self.category and not cats.get(self.category):
            self.category = None  # commands changed while browsing
        options = self.cog.select_options(self.category)
        if options:
            self.add_item(_CategorySelect(self, options))
        if self.category:
            total = len(cats.get(self.category) or [])
            total_pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1
            self.page = max(0, min(self.page, total_pages - 1))
            if total_pages > 1:
                self.add_item(_PrevButton(self, disabled=self.page <= 0))
                self.add_item(_NextButton(self, disabled=self.page >= total_pages - 1))
            self.add_item(_HomeButton(self))

    # -- navigation -------------------------------------------------------
    async def _edit(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        self._rebuild()
        try:
            await interaction.response.edit_message(embed=embed, view=self)
            return
        except discord.InteractionResponded:
            # Response already consumed — edit the menu message directly.
            pass
        except discord.HTTPException as exc:
            log.warning("[Help] menu edit failed: %s", exc)
        message = self.message
        if message is None:
            message = getattr(interaction, "message", None)
            if message is not None:
                self.message = message
        if message is not None:
            try:
                await message.edit(embed=embed, view=self)
            except discord.HTTPException as exc:
                log.warning("[Help] direct menu edit failed: %s", exc)

    async def select_category(self, interaction: discord.Interaction, category: str) -> None:
        self.category = category
        self.page = 0
        embed, _page, _total = self.cog.category_embed(category, 0)
        await self._edit(interaction, embed)

    async def step_page(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(0, self.page + delta)
        embed, actual, _total = self.cog.category_embed(self.category or "", self.page)
        self.page = actual
        await self._edit(interaction, embed)

    async def go_home(self, interaction: discord.Interaction) -> None:
        self.category = None
        self.page = 0
        await self._edit(interaction, self.cog.main_embed())

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
class HelpCog(commands.Cog, name="FactionHelp"):
    """Dynamic ``!help`` — category browser + command detail pages."""

    #: how long a categorized snapshot stays fresh before we rebuild it
    CACHE_TTL_SECONDS = 300

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._categorized_cache: Optional[Dict[str, List[commands.Command]]] = None
        self._categorized_cache_ts: float = 0.0
        # Invalidate the cache whenever a cog is added or removed so a
        # hot-reloaded command set is never served from a stale snapshot.
        bot.add_listener(self._on_cog_change, "on_cog_add")
        bot.add_listener(self._on_cog_change, "on_cog_remove")

    async def _on_cog_change(self, _cog) -> None:
        self._categorized_cache = None
        self._categorized_cache_ts = 0.0

    # ------------------------------------------------------------------
    # Prefix
    # ------------------------------------------------------------------
    def _prefix(self) -> str:
        value = getattr(self.bot, "command_prefix", None)
        if isinstance(value, str) and value:
            return value
        cfg = getattr(self.bot, "fb_config", None) or getattr(self.bot, "config", None)
        value = getattr(cfg, "command_prefix", None)
        if isinstance(value, str) and value:
            return value
        return "!"

    # ------------------------------------------------------------------
    # Categorization (live, never cached)
    # ------------------------------------------------------------------
    def _category(self, command: commands.Command) -> str:
        """Map a command to a display category (see module docstring)."""
        top = command.root_parent or command
        cog = top.cog or command.cog
        if cog is not None:
            mapped = COG_CATEGORY_MAP.get(cog.qualified_name)
            if mapped is not None:
                return mapped
        name = command.name.lower()
        if name in ADMIN_NAMES:
            return CATEGORY_ADMIN
        if name in MOD_NAMES:
            return CATEGORY_MODERATION
        if name in TICKET_NAMES:
            return CATEGORY_TICKETS
        if name in RR_NAMES:
            return CATEGORY_REACTION
        module = getattr(getattr(command, "callback", None), "__module__", "") or ""
        if module == "TicketTool" or module.startswith("TicketTool."):
            return CATEGORY_TICKETS
        if module == "ReactionRoles" or module.startswith("ReactionRoles."):
            return CATEGORY_REACTION
        return CATEGORY_UTILITY

    def categorized(self) -> Dict[str, List[commands.Command]]:
        """category → sorted command list, computed from the live command set.

        Cached for ``CACHE_TTL_SECONDS`` seconds. The help menu rebuilds
        the select options on every interaction; on a 200-command bot that
        walked every command each time. The TTL cache keeps the menu
        instant while still picking up newly-loaded cogs within 5 minutes
        (or immediately, on the add/remove listener).
        """
        now = time.time()
        if (
            self._categorized_cache is not None
            and (now - self._categorized_cache_ts) < self.CACHE_TTL_SECONDS
        ):
            return self._categorized_cache

        cats: Dict[str, List[commands.Command]] = {name: [] for name in ALL_CATEGORIES}
        for command in self.bot.commands:
            try:
                cats.setdefault(self._category(command), []).append(command)
            except Exception:
                log.exception("[Help] failed to categorize %s", command)
                cats.setdefault(CATEGORY_UTILITY, []).append(command)
        for commands_list in cats.values():
            commands_list.sort(key=lambda c: (getattr(c, "name", "") or "").lower())

        self._categorized_cache = cats
        self._categorized_cache_ts = now
        return cats

    # ------------------------------------------------------------------
    # Short/long descriptions
    # ------------------------------------------------------------------
    @staticmethod
    def _short_desc(command: commands.Command) -> str:
        candidates = (
            getattr(command, "brief", None),
            getattr(command, "description", None),
            getattr(command, "help", None),
        )
        for value in candidates:
            line = _first_line(value)
            if line:
                return line
        app_command = getattr(command, "app_command", None)
        line = _first_line(getattr(app_command, "description", None))
        if line:
            return line
        return "—"

    @staticmethod
    def _long_desc(command: commands.Command) -> str:
        for value in (getattr(command, "help", None), getattr(command, "description", None)):
            if value and str(value).strip():
                text = str(value).strip()
                if len(text) > 1000:
                    text = text[:997] + "…"
                return text
        app_command = getattr(command, "app_command", None)
        text = (getattr(app_command, "description", None) or "").strip()
        if text:
            return text[:1000]
        return ""

    # ------------------------------------------------------------------
    # Embeds
    # ------------------------------------------------------------------
    def main_embed(self) -> discord.Embed:
        cats = self.categorized()
        p = self._prefix()
        total = sum(len(v) for v in cats.values())
        used = [name for name in ALL_CATEGORIES if cats.get(name)]
        lines = [f"**{name}** — {len(cats[name])} command(s)" for name in used]
        description = "\n".join(lines)
        description += (
            f"\n\n**{total}** commands in **{len(used)}** categories.\n"
            f"Pick a category below, or run `{p}help <command>` for details "
            f"and `{p}help <category>` to browse."
        )
        return botkit.neutral("❓ FactionBot Help", description)

    def select_options(self, current: Optional[str]) -> List[discord.SelectOption]:
        cats = self.categorized()
        options: List[discord.SelectOption] = []
        for name in ALL_CATEGORIES:
            count = len(cats.get(name) or [])
            if count == 0:
                continue
            emoji, label = _split_emoji(name)
            options.append(discord.SelectOption(
                label=label,
                value=name,
                description=f"{count} command(s)",
                emoji=emoji or None,
                default=(name == current),
            ))
        return options[:25]

    def category_embed(self, category: str, page: int) -> Tuple[discord.Embed, int, int]:
        """(embed, actual_page, total_pages) — pages clamp to live data."""
        cats = self.categorized()
        commands_list = cats.get(category) or []
        p = self._prefix()
        total_pages = max(1, math.ceil(len(commands_list) / PAGE_SIZE))
        page = max(0, min(page, total_pages - 1))
        chunk = commands_list[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines: List[str] = []
        for command in chunk:
            marker = " *(group)*" if isinstance(command, commands.Group) else ""
            lines.append(f"**{p}{command.qualified_name}**{marker} — {self._short_desc(command)}")
        description = "\n".join(lines) or "No commands in this category."
        description += (
            f"\n\n`{p}help <command>` — details for any command above"
        )
        embed = botkit.neutral(f"{category} — {len(commands_list)} commands", description)
        if total_pages > 1:
            embed.set_footer(text=f"Page {page + 1}/{total_pages}")
        return embed, page, total_pages

    # ------------------------------------------------------------------
    # Command resolution
    # ------------------------------------------------------------------
    def _resolve_command(self, query: str) -> Optional[commands.Command]:
        """Resolve '<name>', '!name', 'name sub [subsub]]' to a Command."""
        tokens = [t for t in (query or "").strip().split() if t]
        if not tokens:
            return None
        first = tokens[0].lower().lstrip("!/")
        pool: Dict[str, commands.Command] = {}
        for command in self.bot.commands:
            pool.setdefault(command.name.lower(), command)
            for alias in (getattr(command, "aliases", None) or []):
                pool.setdefault(str(alias).lower(), command)  # first registration wins
        command = pool.get(first)
        if command is None:
            return None
        for token in tokens[1:]:
            if not isinstance(command, commands.Group):
                break
            sub_pool: Dict[str, commands.Command] = {}
            for sub in command.commands:
                sub_pool.setdefault(sub.name.lower(), sub)
                for alias in (getattr(sub, "aliases", None) or []):
                    sub_pool.setdefault(str(alias).lower(), sub)
            nxt = sub_pool.get(token.lower().lstrip("!/"))
            if nxt is None:
                break
            command = nxt
        return command

    def _all_names(self) -> List[str]:
        """Every invocable name (commands, aliases, subcommand paths) for suggestions."""
        names: Set[str] = set()

        def _walk(command: commands.Command) -> None:
            names.add(command.name.lower())
            for alias in (getattr(command, "aliases", None) or []):
                names.add(str(alias).lower())
            if isinstance(command, commands.Group):
                for sub in command.commands:
                    names.add(f"{command.qualified_name} {sub.name}".lower())
                    _walk(sub)

        for command in self.bot.commands:
            try:
                _walk(command)
            except Exception:
                continue
        return sorted(names)

    # ------------------------------------------------------------------
    # Category matching
    # ------------------------------------------------------------------
    def _match_category(self, query: str) -> Optional[str]:
        q = _normalize(query)
        if not q:
            return None
        # 1. exact normalized category name
        for name in ALL_CATEGORIES:
            if _normalize(name) == q:
                return name
        # 2. exact keyword
        keyword = _CATEGORY_KEYWORDS.get(q)
        if keyword is not None:
            return keyword
        # 3. prefix match (needs at least 3 chars)
        if len(q) >= 3:
            for name in ALL_CATEGORIES:
                if _normalize(name).replace(" ", "").startswith(q.replace(" ", "")):
                    return name
        # 4. fuzzy keyword / category
        keyword_matches = difflib.get_close_matches(q, list(_CATEGORY_KEYWORDS), n=1, cutoff=0.6)
        if keyword_matches:
            return _CATEGORY_KEYWORDS[keyword_matches[0]]
        normalized_names = {_normalize(name): name for name in ALL_CATEGORIES}
        fuzzy = difflib.get_close_matches(q, list(normalized_names), n=1, cutoff=0.6)
        if fuzzy:
            return normalized_names[fuzzy[0]]
        return None

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------
    @staticmethod
    def _cooldown_text(command: commands.Command) -> str:
        try:
            cooldown = command.cooldown
        except Exception:
            return ""
        if cooldown is None:
            return ""
        # The bucket type lives on the CooldownMapping, not the Cooldown.
        bucket_obj = getattr(getattr(command, "_buckets", None), "type", None) \
            or getattr(cooldown, "type", None)
        bucket = str(bucket_obj or "").rsplit(".", 1)[-1].replace("_", " ").strip() or "user"
        try:
            return f"{cooldown.rate} use(s) every {cooldown.per:g}s per {bucket}"
        except Exception:
            return ""

    @staticmethod
    def _describe_check(check: Any) -> Optional[str]:
        """Best-effort identification of a command check.

        discord.py stores the raw predicate closures in ``Command.checks``
        (they are all named ``predicate`` / ``wrapper``), so we inspect the
        closure cells (has_permissions keeps its perms dict) and the code
        object's referenced names (which exceptions it raises). Anything
        unidentifiable returns None and is summarized generically.
        """
        try:
            for cell in (getattr(check, "__closure__", None) or ()):
                try:
                    value = cell.cell_contents
                except ValueError:
                    continue
                if (isinstance(value, dict) and value
                        and set(value) <= set(discord.Permissions.VALID_FLAGS)):
                    perms = [k.replace("_", " ").title()
                             for k, v in value.items() if v]
                    if perms:
                        return "Requires permission: " + ", ".join(perms[:6])
            code = getattr(check, "__code__", None)
            names = set(getattr(code, "co_names", ()) or ())
            if "BotMissingPermissions" in names:
                return "Bot requires Discord permissions"
            if "MissingPermissions" in names:
                return "Requires Discord permissions"
            if "NoPrivateMessage" in names:
                return "Server only (no DMs)"
            if "NotOwner" in names or "is_owner" in names:
                return "Bot owner only"
            if "MissingRole" in names or "MissingAnyRole" in names:
                return "Requires a specific role"
            name = (getattr(check, "__name__", "") or "").lower()
            if "has_permissions" in name:
                return "Requires Discord permissions"
            if "bot_has_permissions" in name:
                return "Bot requires Discord permissions"
            if "guild_only" in name:
                return "Server only (no DMs)"
            if "is_owner" in name:
                return "Bot owner only"
        except Exception:
            return None
        return None

    @classmethod
    def _perm_hints(cls, command: commands.Command) -> List[str]:
        hints: Set[str] = set()
        for check in getattr(command, "checks", []):
            described = cls._describe_check(check)
            if described is not None:
                hints.add(described)
        if not hints and getattr(command, "checks", None):
            hints.add("Additional checks apply (try the command to see)")
        return sorted(hints)

    def _command_embed(self, command: commands.Command) -> discord.Embed:
        p = self._prefix()
        qualified = command.qualified_name
        embed = botkit.neutral(f"❓ Command: {p}{qualified}")

        parts: List[str] = []
        long_desc = self._long_desc(command)
        if long_desc:
            parts.append(long_desc)
        if isinstance(command, commands.Group):
            subs = sorted(command.commands, key=lambda c: c.name)
            if subs:
                lines = [f"`{p}{qualified} {sub.name}` — {self._short_desc(sub)}"
                         for sub in subs[:15]]
                text = "\n".join(lines)
                if len(text) > 1000:
                    text = text[:997] + "…"
                parts.append(f"**Subcommands ({len(subs)}):**\n{text}")
        embed.description = "\n\n".join(parts) or "No description available."

        signature = (command.signature or "").strip()
        embed.add_field(
            name="Usage",
            value=f"`{p}{qualified}{' ' + signature if signature else ''}`",
            inline=False,
        )
        top = command.root_parent or command
        embed.add_field(name="Category", value=self._category(top), inline=True)
        aliases = list(getattr(command, "aliases", None) or [])
        if aliases:
            shown = ", ".join(f"`{p}{a}`" for a in aliases[:10])
            if len(aliases) > 10:
                shown += f" (+{len(aliases) - 10})"
            embed.add_field(name="Aliases", value=shown, inline=True)
        cooldown = self._cooldown_text(command)
        if cooldown:
            embed.add_field(name="Cooldown", value=cooldown, inline=True)
        hints = self._perm_hints(command)
        if hints:
            embed.add_field(name="Permissions", value="\n".join(hints[:6]), inline=False)
        return embed

    def _unknown_embed(self, query: str) -> discord.Embed:
        p = self._prefix()
        q = (query or "").strip()
        description = f"No command or category matches `{q}`."
        matches = difflib.get_close_matches(q.lower(), self._all_names(), n=5, cutoff=0.5)
        if matches:
            description += "\n\n**Did you mean:**\n" + "\n".join(f"`{p}{m}`" for m in matches)
        normalized_names = {_normalize(name): name for name in ALL_CATEGORIES}
        category_matches = difflib.get_close_matches(
            _normalize(q), list(normalized_names), n=3, cutoff=0.5)
        if category_matches:
            description += "\n\n**Similar categories:**\n" + "\n".join(
                normalized_names[m] for m in category_matches)
        description += f"\n\nRun `{p}help` to browse every category."
        return botkit.error("Command not found", description)

    # ------------------------------------------------------------------
    # !help / /help
    # ------------------------------------------------------------------
    @commands.command(
        name="help",
        aliases=["cmds", "commands", "h"],
        description="FactionBot help — browse categories or inspect a command",
    )
    @commands.cooldown(1, 3.0, commands.BucketType.user)
    async def help_cmd(self, ctx: commands.Context, *, query: Optional[str] = None) -> None:
        """Show the interactive help menu, a command detail page, or a category."""
        ephemeral = ctx.interaction is not None
        query = (query or "").strip()
        try:
            if not query:
                view = HelpView(self, ctx.author.id)
                message = await ctx.send(embed=self.main_embed(), view=view,
                                         ephemeral=ephemeral)
                view.message = message
                return
            command = self._resolve_command(query)
            if command is not None:
                await ctx.send(embed=self._command_embed(command), ephemeral=ephemeral)
                return
            category = self._match_category(query)
            if category is not None:
                embed, page, _total = self.category_embed(category, 0)
                view = HelpView(self, ctx.author.id, category=category, page=page)
                message = await ctx.send(embed=embed, view=view, ephemeral=ephemeral)
                view.message = message
                return
            await ctx.send(embed=self._unknown_embed(query), ephemeral=ephemeral)
        except discord.HTTPException as exc:
            log.warning("[Help] failed to send help output: %s", exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
