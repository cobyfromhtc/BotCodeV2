# -*- coding: utf-8 -*-
import signal
import discord
from discord.ext import commands, tasks
import discord.utils
import asyncio
import copy
import os
import logging
import sys

# Force UTF-8 output on all platforms (fixes garbled emoji/symbols on Windows)
try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass
from datetime import datetime, timedelta, timezone
import random
import json
from discord.ui import Button, View, Modal, TextInput, Select
from discord import app_commands
import time
from logging.handlers import RotatingFileHandler
from typing import Optional, Set, Dict, Tuple, List, Any, Callable
from collections import OrderedDict
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import sqlite3
import pickle
import re
import uuid as _uuid
import html as _html
from urllib.parse import urlparse


# ═════════════════════════════════════════════════════════════════════════
# PATH ANCHORS — resolved from THIS file's real location so the bot always
# reads/writes inside its own project folder, no matter which working
# directory it was launched from. Each variant (Full/Short) therefore keeps
# a fully separate data/bot_data.db, log file, and lock file — launching
# from the repo root can never make two variants share one database.
# (Same convention as utils.botkit.PROJECT_ROOT.)
# ═══════════════════════════════════════════════════════════════════════
_HERE = os.path.dirname(os.path.abspath(__file__))    # .../src
_PROJECT_ROOT = os.path.dirname(_HERE)                 # variant root

from core.paths import _DATA_DIR
_LOG_DIR = os.path.join(_DATA_DIR, "logs")

# ═══════════════════════════════════════════════════════════════════════════
# LOGGING — configured FIRST, before anything else, so EVERY error is visible.
# Previously this was called at line 16085, meaning 156 logging.*() calls
# before it produced NO output — silently swallowing startup errors.
# ═══════════════════════════════════════════════════════════════════════════

def setup_logging() -> None:
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    _instance = (os.environ.get('FACTIONBOT_INSTANCE') or '').strip().lower()
    _log_name = f'bot-{_instance}.log' if _instance else 'bot.log'
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(_LOG_DIR, _log_name),
            maxBytes=1024*1024,
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setFormatter(log_formatter)
    except Exception:
        file_handler = None
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(log_formatter)
    handlers = [h for h in [file_handler, stream_handler] if h is not None]
    logging.basicConfig(level=logging.INFO, handlers=handlers)

setup_logging()
logging.info("[Startup] Logging configured — beginning bot initialization...")



# ═══════════════════════════════════════════════════════════════════════════
# TICKET TOOL + REACTION ROLES — bundled feature packages.
# These used to be embedded as inline source strings + an exec() loader;
# they now live as regular importable packages:
#   packages/tickettool/      (26 modules)
#   packages/reactionroles/   (5 modules)
# The import surface is unchanged (TicketTool.commands, TicketTool.wiring,
# ReactionRoles.commands, ...) so every call site below keeps working.
# ═══════════════════════════════════════════════════════════════════════════
# (_HERE / _PROJECT_ROOT are defined near the top of this file, before
# logging starts, so every path decision below can rely on them.)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from packages import tickettool as TicketTool
from packages import reactionroles as ReactionRoles
from packages import faction_access as FactionAccess

PREMIUM_AVAILABLE = True
RR_AVAILABLE = True

# ═══════════════════════════════════════════════════════════════════════════
# LAYERED CORE — infrastructure extracted from this module (SaaS restructure):
#   config.settings     Config class (branding/channels/roles/timing/limits)
#   core.data_manager   DataManager (SQLite persistence)
#   core.state          the config + data_manager singletons
#   config.environment  token loading (.env / tokens.txt)
# src/bot.py keeps ONLY the application layer: bot subclass, views, commands,
# events, tasks, the multi-bot domain machinery and main().
# ═══════════════════════════════════════════════════════════════════════════
from core import EDITION, __version__
from core.data_manager import DataManager
from core.state import config, data_manager
from config.environment import (
    get_bot_token,
    is_placeholder_token,
    load_local_env_file,
    read_token_from_file,
    _clean_token_value,
)


def _esc(value) -> str:
    """HTML-escape user-controlled text for safe embedding in transcript HTML."""
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _safe_url(url) -> str:
    """Return the URL only if it parses to a safe http(s) scheme, else empty."""
    if not url:
        return ""
    try:
        parsed = urlparse(str(url))
    except Exception:
        return ""
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return _esc(str(url))
    return ""

try:
    import aiosqlite
    SQLITE_AVAILABLE = True
except ImportError:
    SQLITE_AVAILABLE = False

# Roblox in-game verification support removed; server-only verification flow is used.


# ═══════════════════════════════════════════════════════════════════════════
# INTERNAL STUB TYPES
# ═══════════════════════════════════════════════════════════════════════════
class _BulkDeleteStub:
    """Lightweight message-shaped object used by the bulk-delete logger.

    `MessageLogSystem.log_bulk_delete` expects objects exposing `.id`,
    `.content`, `.author`, and `.attachments`. When we only have a cached
    snapshot (no live `discord.Message`), we build one of these so the
    logger can render the entry without an extra API fetch.
    """
    __slots__ = ("id", "content", "author", "attachments")

    def __init__(self, *, message_id: int, content: str, author, attachments):
        self.id = message_id
        self.content = content
        self.author = author
        self.attachments = attachments or []

    def __repr__(self) -> str:
        return f"<_BulkDeleteStub id={self.id} author={self.author}>"


# --- ENUMERATIONS ---
class VerificationStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    PENDING_INFO = "pending_info"


class TicketStatus(Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class WarningType(Enum):
    SPAM = "spam"
    HARASSMENT = "harassment"
    TOXIC = "toxic"
    RAID = "raid"
    ADVERTISING = "advertising"
    NSFW = "nsfw"
    CUSTOM = "custom"


def brand_text(text: str) -> str:
    """
    Rebrand hardcoded legacy names AND [GANG NAME]/[GANG ABBR] placeholders
    with the configured gang name / abbreviation.

    Uses unique sentinel placeholders so that a configured gang name or
    abbreviation containing a legacy token can never re-trigger another
    replacement and corrupt the text.
    """
    if not text:
        return text

    # Sentinels for legacy tokens
    PH_FULL_1 = "\x00\x01BRAND_FULL_1\x01\x00"   # "Mask Off Society"
    PH_FULL_2 = "\x00\x01BRAND_FULL_2\x01\x00"   # "Shoot On Sight"
    PH_FULL_3 = "\x00\x01BRAND_FULL_3\x01\x00"   # "SOS"
    PH_ABBR   = "\x00\x01BRAND_ABBR\x01\x00"     # "MOS"
    PH_SERVER = "\x00\x01BRAND_SERVER\x01\x00"   # "VPRP"
    # Sentinels for [GANG NAME] / [GANG ABBR] placeholders
    PH_PH_FULL = "\x00\x01BRAND_PH_FULL\x01\x00"
    PH_PH_ABBR = "\x00\x01BRAND_PH_ABBR\x01\x00"

    # 1) Replace every token with a unique sentinel.
    result = text.replace("Mask Off Society", PH_FULL_1)
    result = result.replace("Shoot On Sight", PH_FULL_2)
    result = result.replace("[GANG NAME]", PH_PH_FULL)       # NEW
    result = result.replace("[GANG ABBR]", PH_PH_ABBR)       # NEW
    result = result.replace("SOS", PH_FULL_3)
    result = result.replace("MOS", PH_ABBR)
    result = result.replace("VPRP", PH_SERVER)

    # 2) Resolve sentinels to configured values exactly once.
    result = result.replace(PH_FULL_1, config.gang_name)
    result = result.replace(PH_FULL_2, config.gang_name)
    result = result.replace(PH_FULL_3, config.gang_name)
    result = result.replace(PH_PH_FULL, config.gang_name)   # NEW
    result = result.replace(PH_PH_ABBR, config.gang_abbreviation)  # NEW
    result = result.replace(PH_ABBR, config.gang_abbreviation)
    result = result.replace(PH_SERVER, "Server")
    return result


# --- BOT INTENTS AND INSTANCE ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.invites = True
intents.presences = True
intents.reactions = True


class TicketBot(commands.Bot):
    """commands.Bot subclass with a one-time setup_hook.

    discord.py calls setup_hook() EXACTLY ONCE during startup, before
    on_ready. This is the correct place for DB initialization and other
    one-time work that must not repeat when the gateway reconnects and
    on_ready fires again.

    Previously, data_manager.connect() and several load_*() calls lived
    inside on_ready. Because on_ready can fire multiple times (on every
    gateway reconnect), those calls would re-run — and even though
    DataManager.connect() is now idempotent (it early-returns if already
    connected), re-running the loads is wasteful and re-registering
    persistent views is noisy. setup_hook eliminates the concern entirely.
    """

    async def setup_hook(self) -> None:
        # --- One-time DB + cache initialization (runs exactly once) ---
        data_manager.connect()                # idempotent: safe even if called again
        hydrate_ows_settings()
        config.load_branding_settings()
        config.load_channel_settings()
        config.load_role_settings()
        config.load_timing_settings()
        config.load_limits_settings()

        # Load persisted state from the (now-connected) SQLite DB into the
        # in-memory caches. These must run before on_ready so the caches
        # are populated before the first guild event arrives.
        load_blacklist_data()
        load_warnings_data()

        # Warm the no-purge exclusion cache so !nopurge-protected messages
        # are respected by the auto-purge system and !purge/!purgeall from
        # the very first message, without a lazy-load DB hit on each check.
        try:
            data_manager.load_all_no_purge_message_ids()
        except Exception as exc:
            logging.warning(f"[NoPurge] could not load exclusion cache on startup: {exc}")

        # One-time JSON → SQLite migration (if enabled and not already done).
        if ows_get("json_to_sqlite_import"):
            import_json_to_sqlite()

        # Pre-register the generic persistent views (the per-panel
        # ones are registered in on_ready, where self.guilds is
        # available — setup_hook runs before guilds are fully cached).
        self.add_view(TicketControlView(""))
        self.add_view(TicketModeratorView())
        # Generic dropdown-panel select (stable custom_id). Real per-message
        # views with live panel options are re-registered in on_ready from
        # the multi_panels table; this covers the setup→ready window.
        try:
            self.add_view(TicketPanelSelectView([]))
        except Exception as exc:
            logging.warning(f"[SetupHook] generic panel-select registration failed: {exc}")
        self.add_view(GetAllRolesView())
        logging.info("[SetupHook] One-time initialization complete (DB + caches + generic views)")

        # --- FACTIONBOT SUBSYSTEM MODULES ---
        # Verification / Polls / Invites / Leveling / AutoMod / Setup / Help.
        # Modules access the core via attributes (never import this module):
        #   bot.fb_config / bot.fb_data_manager / bot.embed_builder
        # The default help command is disabled so modules.support.help can own !help.
        self.fb_config = config
        self.fb_data_manager = data_manager
        try:
            from utils import botkit as _botkit
            _botkit.set_footer(f"FactionBot • {config.gang_name}" if config.gang_name else "FactionBot")
        except Exception as exc:
            logging.warning(f"[SetupHook] botkit branding footer not applied: {exc}")
        self.help_command = None
        from modules import EXTENSIONS

        for _ext in EXTENSIONS:
            try:
                await self.load_extension(_ext)
                logging.info(f"[SetupHook] loaded extension {_ext}")
            except Exception as exc:
                logging.exception(f"[SetupHook] extension {_ext} FAILED to load: {exc}")

        # --- PREMIUM TIER 1 SCHEMA INSTALL ---
        # Installs new tables + idempotent column migrations on top of the
        # base ticket_* tables created above. Safe to call repeatedly.
        # NOTE: stashes EmbedBuilder on the bot so extracted packages
        # (ReactionRoles, etc.) can build branded embeds without importing
        # from this monolith module.
        self.embed_builder = EmbedBuilder
        if PREMIUM_AVAILABLE:
            try:
                TicketTool.wiring.on_setup_hook(data_manager, self)
            except Exception as exc:
                logging.exception(f"[SetupHook] TicketTool.on_setup_hook failed: {exc}")

        # --- REACTION ROLES SCHEMA INSTALL + ACCESSOR ---
        # Installs the reaction_roles table (idempotent) and stashes the
        # ReactionRolesDB accessor on the bot as `bot.reaction_roles_db`.
        # Safe to call repeatedly. Mirrors the TicketTool contract above.
        if RR_AVAILABLE:
            try:
                ReactionRoles.wiring.on_setup_hook(data_manager, self)
            except Exception as exc:
                logging.exception(f"[SetupHook] ReactionRoles.on_setup_hook failed: {exc}")

        # --- FACTIONACCESS SCHEMA INSTALL + SERVICE ---
        # Installs the faction_* tables (idempotent), builds the licensing
        # service and stashes it as bot.faction_access / state.faction_access.
        # Owns the multi-guild license lifecycle, per-guild identity and the
        # data behind the global command gate installed earlier in this file.
        try:
            FactionAccess.wiring.on_setup_hook(data_manager, self)
        except Exception as exc:
            logging.exception(f"[SetupHook] FactionAccess.on_setup_hook failed: {exc}")

        # --- TICKET TOOL SYSTEM (moved from on_ready) ---
        # Previously created in on_ready. That meant any gateway event
        # arriving before on_ready finished (or after a setup_hook failure)
        # saw `ticket_tool = None` and short-circuited. `TicketToolSystem`
        # only needs a live DB connection and the bot instance — both are
        # available here — so we build it in setup_hook.
        global ticket_tool
        ticket_tool = TicketToolSystem(data_manager, self)
        self.ticket_tool = ticket_tool
        logging.info("[SetupHook] TicketToolSystem initialized")

        # Register persistent views that can only exist once ticket_tool
        # is available. (The `TicketPanelView` / `TicketPanelSelectView`
        # registrations for stored panels still happen in on_ready because
        # they need `bot.guilds` to be fully cached.)
        self.add_view(TicketModeratorView())

        # Warm the reaction-panel cache now that the DB is live.
        for guild in self.guilds:
            try:
                for row in data_manager.load_reaction_panels_by_guild(guild.id):
                    try:
                        mapping = json.loads(row.get('mapping') or '{}')
                    except (ValueError, TypeError):
                        continue
                    if isinstance(mapping, dict) and mapping:
                        _cache_reaction_panel(row['message_id'], mapping)
            except Exception as exc:
                logging.warning(f"[SetupHook] reaction-panel cache warm failed: {exc}")

        # Quick sanity log so operators can confirm ticket commands are
        # usable from the very first message the bot receives.
        logging.info("[SetupHook] Ticket subsystem ready (commands usable pre-ready)")




# =============================================================================
# MULTI-BOT DOMAIN SUPPORT (ModBot / TicketBot / UtilityBot)
# =============================================================================
# The project can run as SEVERAL Discord bots at once, each with its own
# token and its own entry file (src/ModBot.py, TicketBot.py,
# UtilityBot.py — see RunBots.py). Each domain bot keeps ONLY the commands,
# events and background tasks of its domain; all instances share the same
# SQLite database (WAL mode) so tickets/panels/settings are common.
#
# Running plain `python Bot.py` = the original full bot (all domains),
# exactly as before. See the tail of this file for the launch logic.
# =============================================================================

# Domain name for THIS process: 'full' for the classic single bot, or one of
# 'mod' | 'ticket' | 'utility' when launched via a domain entry file.
INSTANCE_DOMAIN: str = (os.environ.get('FACTIONBOT_INSTANCE') or 'full').strip().lower()

# Domains whose commands/events/tasks this instance handles.
ACTIVE_DOMAINS: set = {'mod', 'ticket', 'utility'}

# Commands owned by each domain (Bot.py commands; premium-package commands
# are added below after registration). Anything unlisted defaults to utility.
TICKET_DOMAIN_COMMANDS: set = {
    # core ticket lifecycle
    'add', 'remove', 'claim', 'unclaim', 'close', 'closerequest', 'reopen',
    'transcript', 'rename', 'move', 'note', 'notes', 'priority',
    # panels
    'panel', 'panels', 'deletepanel', 'panelupdate', 'panelquestion',
    'multipanel', 'dropdownpanel', 'reactionpanel', 'limitbypass',
    # command-style tickets
    'new', 'ticket',
    # pause/resume + info + privacy + rating
    'pause', 'resume', 'ticket-info', 'private', 'unprivate', 'rate',
    # ticket categories (internal folders)
    'tcategory', 'setcategory',
    # ticket admin/diagnostics
    'ticketsettings', 'ticketlog', 'ticketblacklist', 'ticketunblacklist',
    'tickets', 'ticketstats', 'ticketdebug', 'permissionlevel', 'tickethelp',
    'dbcleanup',
}
MOD_DOMAIN_COMMANDS: set = {
    # keyword blacklist / auto-ban
    'blacklist', 'blacklistlist', 'blacklistscan', 'unblacklist', 'checkprofile',
    # warnings
    'warn', 'warnings', 'clearwarnings',
    # channel moderation
    'purge', 'lock', 'unlock', 'slowmode', 'nopurge',
    # member punishment
    'ban', 'banid', 'kick', 'softban', 'mute', 'unmute', 'tempmute',
    # role management (moderation)
    'addrole', 'removerole', 'roleall',
    # verification / security
    'verify', 'verifyuser', 'securitycheck',
    # moderation logging / intake
    'msglog', 'auditlog', 'report',
}
# Everything else (leveling, invites, verification, rules, setup
# commands, OWS, polls, sticky roles, branding, misc) = utility.


def _command_domain(name: str) -> str:
    """Domain owning a command (used for domain-bot command pruning).

    Config moves (ModBot_Cmds=... / FunBot_Cmds=...) are checked FIRST — a
    command explicitly assigned to a bot always wins over the defaults."""
    moved = _moved_command_map()
    if name in moved:
        return moved[name]
    if name in TICKET_DOMAIN_COMMANDS:
        return 'ticket'
    if name in MOD_DOMAIN_COMMANDS:
        return 'mod'
    return 'utility'


def domain_active(domain: str) -> bool:
    """True when THIS instance was launched FOR the given domain."""
    return domain in ACTIVE_DOMAINS


# =============================================================================
# CUSTOM BOTS (any name beyond the 3 standard domains)
# =============================================================================
# tokens.txt / .env can define MORE bots, e.g.:
#     FunBot_Token=...              (token — required)
#     FunBot_Cmds=poll,rank         (commands it runs — required)
# The listed commands are MOVED to that bot from whichever standard bot
# normally runs them. RunBots.py launches every configured bot automatically.
# =============================================================================

_RESERVED_DOMAIN_NAMES = {'mod', 'ticket', 'utility', 'all', 'bot', 'custom',
                          'full', 'new', 'multi'}
_CUSTOM_DOMAINS_CACHE = None
_MOVED_COMMANDS_CACHE = None
_BASE_MOVES_CACHE = None


def _read_all_config_pairs() -> Dict[str, str]:
    """Every KEY=value pair from tokens.txt plus the environment (.env is
    loaded into the environment first, so it's included). Only well-formed
    config keys (letters/digits/underscore) are kept, so prose and template
    instructions can never be mistaken for configuration."""
    import re as _key_re
    valid_key = _key_re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
    pairs: Dict[str, str] = {}
    load_local_env_file()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for filepath in (
        os.path.join(script_dir, 'tokens.txt'),
        os.path.join(script_dir, '..', 'tokens.txt'),
        os.path.join(os.getcwd(), 'tokens.txt'),
        'tokens.txt',
    ):
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    for raw in f:
                        line = raw.strip()
                        if not line or line.startswith('#') or '=' not in line:
                            continue
                        key, value = line.split('=', 1)
                        key = key.strip()
                        if key and valid_key.match(key):
                            pairs.setdefault(key, value.strip())
                break
        except Exception:
            continue
    for key, value in os.environ.items():
        if value:
            pairs[key] = value
    return pairs


def resolve_custom_domains() -> Dict[str, Dict]:
    """Custom bots from config: every '<Name>Bot_Token' that isn't one of
    the three standard domains, plus its '<Name>Bot_Cmds' command list.

    Returns {domain_name: {'token': str, 'commands': [str]}}."""
    global _CUSTOM_DOMAINS_CACHE
    if _CUSTOM_DOMAINS_CACHE is not None:
        return _CUSTOM_DOMAINS_CACHE
    import re as _re
    token_re = _re.compile(r'^([A-Za-z0-9]+)Bot_Token$', _re.IGNORECASE)
    cmds_re = _re.compile(r'^([A-Za-z0-9]+)Bot_Cmds$', _re.IGNORECASE)
    cmds_re2 = _re.compile(r'^([A-Za-z0-9]+)Bot_Commands$', _re.IGNORECASE)
    pairs = _read_all_config_pairs()
    domains: Dict[str, Dict] = {}
    cmds_by_name: Dict[str, List[str]] = {}
    for key, value in pairs.items():
        m = token_re.match(key)
        if m:
            name = m.group(1).lower()
            if name in _RESERVED_DOMAIN_NAMES or name in _DOMAIN_ORDER:
                continue
            token = _first_token(value)
            if token:
                domains[name] = {'token': token, 'commands': []}
            continue
        m = cmds_re.match(key) or cmds_re2.match(key)
        if m:
            name = m.group(1).lower()
            if name in _DOMAIN_ORDER:
                continue  # base-domain moves handled by _resolve_base_moves()
            cmds = [c.strip() for c in value.split(',') if c.strip()]
            if cmds:
                cmds_by_name.setdefault(name, []).extend(cmds)
    for name, dom in domains.items():
        dom['commands'] = cmds_by_name.pop(name, [])
    for name, cmds in cmds_by_name.items():
        if name not in _RESERVED_DOMAIN_NAMES:
            logging.warning(f"[Startup] {name.capitalize()}Bot_Cmds found but no "
                            f"{name.capitalize()}Bot_Token — that bot is not started.")
    if domains:
        logging.info(f"[Startup] Custom bots configured: "
                     f"{', '.join(d + 'Bot' for d in sorted(domains))}")
    _CUSTOM_DOMAINS_CACHE = domains
    return domains


def _resolve_base_moves() -> Dict[str, List[str]]:
    """Command moves onto the STANDARD bots from config
    (ModBot_Cmds=..., TicketBot_Cmds=..., UtilityBot_Cmds=...)."""
    global _BASE_MOVES_CACHE
    if _BASE_MOVES_CACHE is not None:
        return _BASE_MOVES_CACHE
    import re as _re
    moves: Dict[str, List[str]] = {}
    cmds_re = _re.compile(r'^([A-Za-z0-9]+)Bot_Cmds$', _re.IGNORECASE)
    for key, value in _read_all_config_pairs().items():
        m = cmds_re.match(key)
        if not m:
            continue
        name = m.group(1).lower()
        if name in _DOMAIN_ORDER:
            cmds = [c.strip() for c in value.split(',') if c.strip()]
            if cmds:
                moves.setdefault(name, []).extend(cmds)
    _BASE_MOVES_CACHE = moves
    return moves


def _moved_command_map() -> Dict[str, str]:
    """{command_name: owning_domain} for every command explicitly assigned
    via <Name>Bot_Cmds (standard OR custom bots). Unknown command names are
    warned + skipped; conflicts go to the first bot in order (mod, ticket,
    utility, then customs alphabetically)."""
    global _MOVED_COMMANDS_CACHE
    if _MOVED_COMMANDS_CACHE is not None:
        return _MOVED_COMMANDS_CACHE
    known_commands = {c.name for c in bot.commands}
    result: Dict[str, str] = {}

    def _claim(domain: str, raw_cmds: List[str]) -> None:
        for cmd in raw_cmds:
            cmd = cmd.strip().split()[0] if cmd.strip() else ''  # drop inline annotations
            if not cmd or ' ' in cmd or not cmd.replace('-', '').replace('_', '').isalnum():
                logging.warning(f"[Startup] {domain.capitalize()}Bot_Cmds lists invalid "
                                f"entry '{cmd}' — ignored.")
                continue
            if cmd not in known_commands:
                logging.warning(f"[Startup] {domain.capitalize()}Bot_Cmds lists unknown "
                                f"command '{cmd}' — ignored (see /cmds for valid names).")
                continue
            if cmd in result:
                logging.warning(f"[Startup] Command '{cmd}' is claimed by both "
                                f"{result[cmd].capitalize()}Bot and {domain.capitalize()}Bot — "
                                f"keeping it on {result[cmd].capitalize()}Bot.")
                continue
            result[cmd] = domain

    for base in _DOMAIN_ORDER:
        if base in _resolve_base_moves():
            _claim(base, _resolve_base_moves()[base])
    for custom in sorted(resolve_custom_domains()):
        _claim(custom, resolve_custom_domains()[custom].get('commands') or [])
    _MOVED_COMMANDS_CACHE = result
    return result


def custom_domain_names() -> List[str]:
    """Custom bot domain names, alphabetically."""
    return sorted(resolve_custom_domains().keys())


def _all_domain_names() -> List[str]:
    """Every possible domain: the 3 standard ones + customs."""
    return list(_DOMAIN_ORDER) + custom_domain_names()


def _default_domain_of(command: str) -> str:
    """Which standard domain owns a command BEFORE any config moves."""
    if command in TICKET_DOMAIN_COMMANDS:
        return 'ticket'
    if command in MOD_DOMAIN_COMMANDS:
        return 'mod'
    return 'utility'


def commands_owned_by(domain: str) -> List[str]:
    """Every command this domain runs (defaults + config moves)."""
    if domain == 'mod':
        return sorted(MOD_DOMAIN_COMMANDS | {c for c, d in _moved_command_map().items() if d == 'mod'})
    if domain == 'ticket':
        return sorted(TICKET_DOMAIN_COMMANDS | {c for c, d in _moved_command_map().items() if d == 'ticket'})
    if domain == 'utility':
        moved_away = {c for c, d in _moved_command_map().items() if d != 'utility'}
        all_cmds = {c.name for c in bot.commands}
        return sorted((all_cmds - MOD_DOMAIN_COMMANDS - TICKET_DOMAIN_COMMANDS - moved_away)
                      | {c for c, d in _moved_command_map().items() if d == 'utility'})
    # custom bot: exactly its moved commands
    return sorted(c for c, d in _moved_command_map().items() if d == domain)


def _task_domain_owner(base_domain: str) -> Optional[str]:
    """Which instance runs base_domain's background events/tasks.

    The standard bot if it's deployed; otherwise the FIRST other bot (in
    order mod, ticket, utility, customs) that owns at least one command of
    that domain — so a FunBot that took all the utility commands also
    inherits the utility background jobs when UtilityBot isn't running."""
    if resolve_domain_token(base_domain):
        return base_domain
    for domain in _all_domain_names():
        if domain == base_domain:
            continue
        owned = commands_owned_by(domain)
        if any(_default_domain_of(c) == base_domain for c in owned):
            return domain
    return None


def instance_handles(base_domain: str) -> bool:
    """True when THIS instance must run base_domain's events + background
    tasks (the domain-active check, with inheritance for custom bots that
    replace an undeployed standard bot)."""
    if INSTANCE_DOMAIN == 'full':
        return True
    if INSTANCE_DOMAIN == base_domain:
        return True
    if INSTANCE_DOMAIN in resolve_custom_domains():
        return _task_domain_owner(base_domain) == INSTANCE_DOMAIN
    return False


def _is_lead_instance() -> bool:
    """True for the single instance that runs cross-domain singleton work
    (owner tutorial, report DMs, lock file cleanup).

    Deterministic across processes: every instance reads the SAME token
    config, so they agree on which domains are deployed; the lead is the
    first deployed domain in the fixed order (mod, ticket, utility, customs)."""
    if INSTANCE_DOMAIN == 'full':
        return True
    deployed = [d for d in _all_domain_names() if d in resolve_deployed_domains()]
    return bool(deployed) and INSTANCE_DOMAIN == deployed[0]


print("[Startup] Creating bot instance...")
bot = TicketBot(command_prefix=config.command_prefix, intents=intents)
# Disable discord.py's built-in !help command at import time (not just in
# setup_hook) so modules.support.help can register its own !help command without
# a CommandRegistrationError. setup_hook repeats this for safety, but having
# it here means the built-in is gone before ANY cog loads.
bot.help_command = None
print(f"[Startup] Bot instance created. Commands so far: {len(bot.commands)}")



# Register TicketTool prefix commands on the bot instance.
if PREMIUM_AVAILABLE:
    try:
        _cmds_before_premium = {c.name for c in bot.commands}
        TicketTool.commands.register(bot)
        TICKET_DOMAIN_COMMANDS |= ({c.name for c in bot.commands} - _cmds_before_premium)
        logging.info("[TicketTool] registered prefix commands (inline)")
        print(f"[Startup] TicketTool commands registered. Total commands: {len(bot.commands)}")
    except Exception as exc:
        logging.exception(f"[TicketTool] command registration failed: {exc}")
        print(f"[Startup] WARNING: TicketTool registration FAILED: {exc}")

# Register ReactionRoles prefix commands (!rr group + 5 subcommands).
if RR_AVAILABLE:
    try:
        _cmds_before_rr = {c.name for c in bot.commands}
        ReactionRoles.commands.register(bot)
        logging.info("[ReactionRoles] registered prefix commands (inline)")
        print(f"[Startup] ReactionRoles commands registered. Total commands: {len(bot.commands)}")
    except Exception as exc:
        logging.exception(f"[ReactionRoles] command registration failed: {exc}")
        print(f"[Startup] WARNING: ReactionRoles registration FAILED: {exc}")

# Register FactionAccess prefix commands (!license group + !request), install
# the global command gate and take ownership of on_guild_join/remove (both
# unclaimed in this monolith). The gate is fail-open until setup_hook attaches
# the service, so the home faction can never be locked out by a startup
# ordering issue.
try:
    FactionAccess.commands.register(bot)
    FactionAccess.gating.install(bot)
    FactionAccess.wiring.register_events(bot)
    logging.info("[FactionAccess] registered !license group + !request, installed the command gate and claimed guild membership events")
    print(f"[Startup] FactionAccess registered. Total commands: {len(bot.commands)}")
except Exception as exc:
    logging.exception(f"[FactionAccess] registration failed: {exc}")
    print(f"[Startup] WARNING: FactionAccess registration FAILED: {exc}")



# Once-flag: ensure the one-time portion of on_ready runs only on the first
# connect, not on every gateway reconnect. (setup_hook handles DB + caches;
# this flag guards the guild-dependent work in on_ready that can't move to
# setup_hook because it needs bot.guilds to be fully cached.)
_on_ready_initialized: bool = False


# --- GLOBAL STATE ---
messages_enabled: bool = True
start_time: float = time.time()
blacklisted_keywords: Set[str] = set()

# New V2 data stores
warnings_data: Dict[int, List[Dict]] = {}  # guild_id -> list of warnings
# NOTE: tickets are persisted entirely in SQLite via data_manager (no in-memory cache).


# --- PROCESS MANAGER ---
class ProcessManager:
    def __init__(self):
        self._active_processes: int = 0
    
    def is_busy(self) -> bool:
        return self._active_processes > 0
    
    def _update_lock_file(self) -> None:
        if not ows_get("process_lock_file"):
            return
        try:
            if self.is_busy():
                with open(config.lock_file, 'w') as f:
                    f.write(f"busy since: {datetime.now().isoformat()}")
            else:
                if os.path.exists(config.lock_file):
                    os.remove(config.lock_file)
        except Exception as e:
            logging.error(f"Error updating lock file: {e}")
    
    def clear_lock_file(self) -> None:
        try:
            if os.path.exists(config.lock_file):
                os.remove(config.lock_file)
        except Exception as e:
            logging.error(f"Error removing lock file: {e}")


process_manager = ProcessManager()

# ═══════════════════════════════════════════════════════════════════════════
# LIVE TIMING-LOOP APPLIERS
# ═══════════════════════════════════════════════════════════════════════════
# Each callable re-reads config.timing and updates the matching task
# loop's interval. The registry lives on the config singleton
# (config.timing_appliers — see config/settings.py) so the extracted
# Config class can iterate it without importing this module. Populated
# right after the loops are defined and invoked by
# Config.apply_timing_to_loops() whenever `!timingsetup` writes a new value.


def _register_timing_appliers() -> None:
    """Populate `config.timing_appliers` once the task loops are defined.

    Called at module bottom, immediately after both loops exist. Idempotent
    — safe to call more than once.
    """
    config.timing_appliers.clear()

    def _apply_report_interval() -> None:
        new_minutes = max(1, int(config.timing.report_message_interval_minutes))
        try:
            send_report_message.change_interval(minutes=new_minutes)
        except Exception as exc:
            logging.warning(f"[Timing] could not update broadcast interval: {exc}")
            return
        if messages_enabled and not send_report_message.is_running():
            try:
                send_report_message.start()
            except RuntimeError:
                pass  # already running from a race — harmless
        logging.info(f"[Timing] Broadcast interval set to {new_minutes} minute(s)")

    def _apply_auto_scan_interval() -> None:
        new_hours = max(1, int(config.timing.auto_scan_interval_hours))
        try:
            auto_blacklist_scan.change_interval(hours=new_hours)
        except Exception as exc:
            logging.warning(f"[Timing] could not update auto-scan interval: {exc}")
            return
        if blacklisted_keywords and not auto_blacklist_scan.is_running():
            try:
                auto_blacklist_scan.start()
            except RuntimeError:
                pass
        logging.info(f"[Timing] Auto-scan interval set to {new_hours} hour(s)")

    config.timing_appliers.append(_apply_report_interval)
    config.timing_appliers.append(_apply_auto_scan_interval)


WELCOME_TEMPLATES: List[str] = [
    "Welcome {mention} to {server}. We expect you to put in work.",
    "What's good, {mention}? Brought any Pizza? No? Whatever. Welcome to {server}, we expect you to work hard.",
    "Yoooo big dawg {mention} just dropped in, what's good?",
    "Is that who I think it is? {mention}, welcome to {server}. We expect you to put in work.",
    "Ay {mention}, we gotta Recruit soon, let me know when you're free Dawg.",
    "Well, well, well, if it isn't the one and only {mention}. We're glad to have you around."
]

# ═══════════════════════════════════════════════════════════════════════════
# PERIODIC BROADCAST TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════
# Sent by the `send_report_message` task loop on the configured interval
# (config.timing.report_message_interval_minutes). Only fires when the OWS
# toggle `periodic_broadcasts` is ON. Format placeholders:
#   {server}          -> the guild's name
#   {rules_channel}   -> mention of config.channels.rules
#   {reports_channel} -> mention of config.channels.reports
#
# `send_report_message` passes these via .format() after brand_text() runs,
# so any braces other than the documented ones will raise a KeyError.
REPORT_TEMPLATES: List[str] = [
    "📢 Reminder — read the rules in {rules_channel}. Everyone is expected to keep it clean.",
    "🛡️ If you witness a rule break, report it in {reports_channel}. Do not engage.",
    "💬 Keep the chat respectful. Toxicity gets muted, not rewarded.",
    "🎫 Need support? Open a ticket with `!new` — staff will be with you shortly.",
    "🔥 Stay active and climb the leaderboard. Top ranks earn perks.",
    "👥 Bring your people. Every verified invite helps {server} grow.",
    "🚨 No advertising, no leaks, no drama. We handle it quietly and permanently.",
    "📌 Check the pinned messages in {rules_channel} for the latest updates.",
    "⏰ Rules apply everywhere, including in DMs with staff.",
    "💯 Shout out to everyone doing their part — the work never goes unnoticed.",
    "🎯 Events and giveaways are posted in {rules_channel}. Keep notifications on.",
    "🧹 Report spam in {reports_channel} instead of replying to it.",
]

REPORT_CATEGORIES: Dict[str, str] = {
    "1️⃣": "Rule Violation",
    "2️⃣": "Harassment",
    "3️⃣": "Scamming",
    "4️⃣": "Cheating",
    "5️⃣": "Other"
}

TICKET_CATEGORIES: Dict[str, str] = {
    "general": "General Support",
    "report": "Player Report",
    "appeal": "Ban Appeal",
    "verification": "Verification Help",
    "other": "Other",
    "alliance": "Request an Alliance",
    "opp": "Request us to Add opp gangs / players"
}

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

# --- EMBED BUILDER (V2 Enhancement) ---
class EmbedBuilder:
    @staticmethod
    def success(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def error(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def warning(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def info(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def verification(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def ticket(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    
    @staticmethod
    def branded(base_embed: discord.Embed, guild_id: Optional[int]) -> discord.Embed:
        """Apply per-guild custom branding (footer / color / thumbnail / image)
        to an existing embed. Falls back gracefully if no branding is configured
        or the data manager isn't ready yet (called very early in startup).

        FactionAccess integration: when the guild carries a faction identity
        override (an allied faction's own tag and name), [GANG NAME] /
        [GANG ABBR] tokens in the footer resolve to THAT guild's names, and
        otherwise-unbranded embeds get the default
        "FactionBot • <that guild's gang name>" footer. The home faction keeps
        its exact pre-FactionAccess appearance (resolve falls back to the
        global config values there)."""
        try:
            if data_manager is None or data_manager._connection is None:
                return base_embed
            if guild_id is None:
                return base_embed
            # Per-guild faction identity — resolved off the bot instance so no
            # new module-level coupling is introduced in this monolith.
            faction = getattr(bot, 'faction_access', None)
            gang_name = config.gang_name
            has_identity_override = False
            if faction is not None:
                try:
                    identity = faction.identity_for(guild_id)
                    gang_name = identity.gang_name or gang_name
                    has_identity_override = identity.is_override
                except Exception:
                    pass
            branding = data_manager.get_branding(guild_id)
            footer = branding.get('embed_footer')
            if footer:
                if faction is not None:
                    # Per-guild substitution (legacy tokens + placeholders).
                    try:
                        base_embed.set_footer(text=faction.resolve_text(footer, guild_id))
                    except Exception:
                        base_embed.set_footer(text=brand_text(footer))
                else:
                    base_embed.set_footer(text=brand_text(footer))
            elif has_identity_override:
                # Allied guild with an identity but no custom footer: still
                # present as its own faction instead of going unbranded.
                base_embed.set_footer(text=f"FactionBot • {gang_name}")
            color = branding.get('embed_color')
            if isinstance(color, int):
                base_embed.colour = discord.Color(color)
            thumb = branding.get('embed_thumbnail')
            if thumb:
                base_embed.set_thumbnail(url=thumb)
            img = branding.get('embed_image')
            if img:
                base_embed.set_image(url=img)
        except Exception as exc:
            logging.debug(f"[EmbedBuilder.branded] skipped branding: {exc}")
        return base_embed


# =============================================================================
# VERIFICATION EMBED BUILDER - Consistent, Modern Design
# =============================================================================

class TicketToolSystem:
    """Main ticket tool system manager - handles all ticket operations."""
    
    def __init__(self, data_mgr: DataManager, bot_instance: commands.Bot):
        self.data_manager = data_mgr
        self.bot = bot_instance
        # Per-(guild, user) locks to prevent the ticket-limit race condition:
        # two near-simultaneous "Create Ticket" clicks could both pass the
        # open-count check before either row is written. Serializing per user
        # closes that window without blocking unrelated users.
        self._creation_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        self._creation_locks_guard = asyncio.Lock()
    
    async def _get_creation_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        async with self._creation_locks_guard:
            key = (guild_id, user_id)
            lock = self._creation_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._creation_locks[key] = lock
            return lock
    
    async def create_ticket(
        self, 
        guild: discord.Guild, 
        user: discord.Member,
        panel: Dict,
        subject: str = None,
        answers: Dict = None
    ) -> Tuple[Optional[discord.TextChannel], str]:
        """Create a new ticket channel.

        Transactional ordering (fixes orphan-channel / orphan-row bugs):
          1. Insert the ticket row with status='pending' FIRST (no channel yet).
          2. Create the Discord channel.
             - On failure: delete the pending row so DB and Discord stay
               consistent.
          3. Update the row with channel_id and status='open'.
          4. Persist any panel answers. If answer persistence fails, the
             ticket itself still exists (logged, not fatal).

        A per-user asyncio.Lock guards the limit check so two concurrent
        create requests can't both pass it.
        """
        ticket_id = str(_uuid.uuid4())[:8]
        now_iso = datetime.now(timezone.utc).isoformat()

        # Owner Settings gate: if the Tickets System is disabled, refuse all
        # new ticket creation. Management commands (close/claim/transcript) are
        # intentionally left available so staff can wind down existing tickets,
        # but no NEW tickets can be opened while the system is disabled.
        if not ows_get("enable_tickets"):
            return None, "The ticket system is currently disabled by the server owner. Please try again later."

        # Check if user is blacklisted
        blacklisted, reason = self.data_manager.is_user_blacklisted(guild.id, user.id)
        if blacklisted:
            return None, f"You are blacklisted from creating tickets. Reason: {reason}"
        
        # Serialize ticket creation per user to close the limit-check race window.
        # The lock is held across the ENTIRE create (limit-check → pending insert
        # → Discord channel creation → open status update) so a second concurrent
        # request from the same user cannot slip in between the limit check and
        # the channel creation. The limit check also now counts BOTH 'pending'
        # and 'open' tickets, so the in-flight 'pending' row is visible to it.
        creation_lock = await self._get_creation_lock(guild.id, user.id)
        async with creation_lock:
            settings = self.data_manager.load_ticket_settings(guild.id)
            # TicketTool-style limit bypass: members holding any configured
            # bypass role skip BOTH the guild-wide and per-panel limits.
            bypass = _member_has_limit_bypass(user, panel, settings)
            if not bypass:
                # Guild-wide limit (TicketTool "Global Ticket Limit"). Gated by
                # the OWS enforce_max_tickets toggle; config.limits is the
                # fallback when no DB settings row exists yet.
                if ows_get("enforce_max_tickets"):
                    max_tickets = (
                        settings.get('max_tickets_per_user')
                        if settings and settings.get('max_tickets_per_user')
                        else getattr(config.limits, 'max_tickets_per_user', 3)
                    )
                    active_count = self.data_manager.count_active_tickets_by_creator(user.id, guild.id)
                    if active_count >= max_tickets:
                        return None, f"You already have {active_count} active ticket(s). Close one first."
                    # TicketTool closed-ticket limit (checked at creation to
                    # prevent open/close cycling). 0/None disables.
                    max_closed = settings.get('max_closed_tickets_per_user') if settings else None
                    if max_closed:
                        closed_count = self.data_manager.count_closed_tickets_by_creator(user.id, guild.id)
                        if closed_count >= int(max_closed):
                            return None, (
                                f"You have already closed {closed_count} ticket(s) "
                                f"(limit: {max_closed}). Please contact staff for further help."
                            )
                    # TicketTool "open tickets all users" cap. 0/None disables.
                    max_open_all = settings.get('max_open_tickets_all') if settings else None
                    if max_open_all:
                        open_all = self.data_manager.count_open_tickets_in_guild(guild.id)
                        if open_all >= int(max_open_all):
                            return None, (
                                f"The ticket queue is full ({open_all}/{max_open_all} open). "
                                f"Please try again later."
                            )
                # Per-panel limit (TicketTool per-panel "open tickets per user").
                panel_limit = panel.get('ticket_limit') if panel else None
                if panel_limit:
                    panel_count = self.data_manager.count_active_tickets_by_creator_and_panel(
                        user.id, guild.id, panel.get('panel_id'),
                    )
                    if panel_count >= int(panel_limit):
                        return None, (
                            f"You already have {panel_count} open ticket(s) in this panel. "
                            f"Close one first."
                        )
        
            # Get panel settings
            # Fall back to config defaults so tickets always land in the
            # configured Tickets category even before the owner runs !channelsetup.
            # This fixes the bug where clicking "Create Ticket" created the
            # channel with NO category (because panel/settings had none set).
            category_id = (
                panel.get('category_id')
                or (settings.get('category_id') if settings else None)
                or config.channels.tickets
            )
            support_role_id = (
                panel.get('support_role_id')
                or (settings.get('support_role_id') if settings else None)
                or config.roles.ticket_support
            )
            
            # Channel name: premium naming templates (with guild-wide ticket
            # counter + zero padding) when configured, else the classic
            # ticket-{username}-{ticket_id} convention.
            ticket_number = None
            safe_name = ''.join(c if c.isalnum() or c == '-' else '-' for c in user.display_name.lower())[:40]
            channel_name = f"ticket-{safe_name}-{ticket_id}"[:90]
            if PREMIUM_AVAILABLE:
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    try:
                        ticket_number = TicketTool.naming.reserve_number(pdb, guild.id)
                    except Exception as exc:
                        logging.debug(f"[TicketTool] reserve_number failed: {exc}")
                    try:
                        channel_name, computed_subject = TicketTool.naming.compute_open_name(
                            pdb, panel,
                            guild={'id': guild.id, 'name': guild.name},
                            ticket_id=ticket_id,
                            creator={'id': user.id, 'name': user.display_name},
                            ticket_count=ticket_number,
                            subject=subject,
                        )
                        if computed_subject and not subject:
                            subject = computed_subject
                    except Exception as exc:
                        logging.debug(f"[TicketTool] compute_open_name failed: {exc}")
            
            # Setup permissions
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
            }
            
            # Add support role
            if support_role_id:
                support_role = guild.get_role(support_role_id)
                if support_role:
                    overwrites[support_role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True, attach_files=True
                    )
            
            # Get category
            category = guild.get_channel(category_id) if category_id else None
            
            # ---- STEP 1: persist a 'pending' ticket row BEFORE creating the channel ----
            ticket_data = {
                'ticket_id': ticket_id,
                'guild_id': guild.id,
                'channel_id': None,           # filled in after channel creation
                'panel_id': panel.get('panel_id'),
                'creator_id': user.id,
                'category': panel.get('name', 'General'),
                'subject': subject,
                'status': 'pending',
                'created_at': now_iso,
                # Internal Ticket Category (folder) inherited from the panel.
                # NULL/absent on old panels = "Uncategorized" (backwards compat).
                'ticket_category_id': panel.get('ticket_category_id') if panel else None,
            }
            await self.data_manager.async_save_ticket(ticket_data)

            # ---- STEP 2: acquire a ticket channel/thread (INSIDE the user lock) ----
            # Holding the lock here is the key race fix: the 'pending' row is
            # already persisted, so a concurrent second request from the same
            # user would see it via count_active_tickets_by_creator and be
            # rejected at the limit check above before reaching this point.
            #
            # Acquisition order (TicketTool behavior):
            #   a) thread-style ticket, when the panel is configured for threads
            #   b) a recycled channel from the panel's recycle pool
            #   c) a freshly created text channel
            channel = None
            if PREMIUM_AVAILABLE:
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    # (a) Thread-style tickets (Ticket Tool premium thread style).
                    try:
                        thread = await TicketTool.wiring.on_ticket_create_thread_check(
                            bot=self.bot, pdb=pdb, guild=guild, panel=panel,
                            ticket_id=ticket_id, channel_name=channel_name,
                            creator=user, support_role_id=support_role_id,
                        )
                        if thread is not None:
                            channel = thread
                            ticket_data['is_thread'] = 1
                            ticket_data['thread_id'] = thread.id
                    except Exception as exc:
                        logging.warning(f"[TicketTool] thread ticket check failed: {exc}")
                    # (b) Channel recycling (dodge the 500-channel guild cap).
                    if channel is None:
                        try:
                            recycled_channel = await TicketTool.channel_recycle.acquire_channel(
                                guild=guild, panel=panel, new_name=channel_name,
                                creator=user, support_role_id=support_role_id,
                                category_id=category_id, pdb=pdb,
                            )
                            if recycled_channel is not None:
                                channel = recycled_channel
                                ticket_data['recycled_from_channel_id'] = recycled_channel.id
                        except Exception as exc:
                            logging.warning(f"[TicketTool] channel recycle acquire failed: {exc}")

            # (c) Fresh text channel.
            if channel is None:
                try:
                    channel = await guild.create_text_channel(
                        channel_name,
                        category=category,
                        overwrites=overwrites,
                        topic=f"Ticket {ticket_id} - {user}"
                    )
                except Exception as e:
                    # Channel creation failed: roll back the pending DB row so we
                    # don't leave an orphan ticket with no channel.
                    logging.error(f"[TicketTool] Failed to create channel: {e}")
                    try:
                        await self.data_manager.async_save_ticket({
                            **ticket_data, 'status': 'failed', 'close_reason': f'Channel creation failed: {e}',
                            'closed_at': datetime.now(timezone.utc).isoformat(),
                        })
                    except Exception:
                        pass
                    return None, f"Failed to create ticket channel: {e}"

            # ---- STEP 3: update the row with the real channel_id and status='open' ----
            ticket_data['channel_id'] = channel.id
            ticket_data['status'] = 'open'
            try:
                await self.data_manager.async_save_ticket(ticket_data)
            except Exception as e:
                # DB update failed after the channel exists. Try to clean up the
                # channel so we don't leave an orphan channel with no DB record.
                logging.error(f"[TicketTool] DB save failed after channel creation: {e}")
                try:
                    await channel.delete(reason="Ticket DB record could not be saved")
                except Exception:
                    pass
                return None, "Ticket channel was created but could not be saved. Please try again."
        
        # ---- STEP 4: persist panel answers (non-fatal if this fails) ----
        # Outside the lock: answer persistence doesn't affect the limit check.
        if answers:
            for qid, answer_text in answers.items():
                try:
                    await self.data_manager.async_save_ticket_answer({
                        'answer_id': str(uuid.uuid4())[:8],
                        'ticket_id': ticket_id,
                        'question_id': qid,
                        'user_id': user.id,
                        'answer_text': answer_text,
                        'answered_at': datetime.now(timezone.utc).isoformat()
                    })
                except Exception as e:
                    logging.warning(f"[TicketTool] Failed to save answer for {ticket_id}: {e}")

        # --- PREMIUM TIER 1: on_ticket_create hook ---
        # Fires role-automation 'open', schedules delayed/no_response
        # automations, initializes SLA state, and fires the 'created' trigger.
        # Reload the ticket row so the hook sees the final 'open' status.
        if PREMIUM_AVAILABLE:
            try:
                fresh_ticket = await self.data_manager.async_load_ticket(ticket_id)
                if fresh_ticket and ticket_number is not None:
                    # Reuse the number reserved for the channel name so the
                    # wiring hook doesn't reserve a second one.
                    fresh_ticket['_count'] = ticket_number
                await TicketTool.wiring.on_ticket_create(
                    bot=self.bot, ticket_tool=self, channel=channel,
                    ticket=fresh_ticket or ticket_data, panel=panel,
                    creator=user,
                )
            except Exception as exc:
                logging.warning(f"[TicketTool] TicketTool.on_ticket_create failed: {exc}")

        # TicketTool-style ticket logging: "Ticket Created" entry in the
        # configured log channel.
        try:
            await log_ticket_event(
                guild, 'created', ticket_data,
                actor=user,
                detail=f"Panel: {panel.get('name', 'General') if panel else 'General'}",
                channel_ref=channel,
            )
        except Exception as exc:
            logging.debug(f"[TicketLog] created event failed: {exc}")

        return channel, ticket_id
    
    async def close_ticket(
        self,
        channel: discord.TextChannel,
        closed_by: discord.Member,
        reason: str = "No reason provided"
    ) -> bool:
        """Close a ticket and generate transcript.

        The open→closing transition is done atomically via
        DataManager.atomic_begin_closing so two near-simultaneous close
        requests (e.g. a staff button click racing the ticket creator's close
        command) can't both proceed to generate a transcript and delete the
        channel. Only the first caller wins the UPDATE; the second sees
        rowcount==0 and returns False immediately.
        """
        ticket = await self.data_manager.async_load_ticket_by_channel(channel.id)
        if not ticket:
            return False

        if ticket.get('status') in ('closed', 'closing'):
            return False

        # Atomic open→closing transition. If this returns False, another caller
        # already started closing (or the ticket was closed/not-found between
        # our load and this UPDATE). Bail out — do NOT generate a transcript.
        won = await asyncio.to_thread(
            self.data_manager.atomic_begin_closing,
            ticket['ticket_id'], closed_by.id, reason,
        )
        if not won:
            logging.info(f"[TicketTool] close_ticket lost the race for {ticket['ticket_id']} (already closing/closed); aborting to avoid duplicate transcript.")
            return False

        # Reload so the in-memory dict reflects the committed 'closing' state
        # (status / closed_by / close_reason) before transcript generation.
        ticket = await self.data_manager.async_load_ticket(ticket['ticket_id'])
        if not ticket:
            logging.error("[TicketTool] Ticket disappeared after atomic_begin_closing; cannot generate transcript.")
            return False
        try:
            transcript = await self._generate_transcript(channel, ticket, closed_by)
        except Exception as e:
            logging.error(f"[TicketTool] Transcript generation failed, aborting close: {e}")
            # Revert to 'open' so the ticket can be retried. Use a conditional
            # UPDATE so we don't clobber a concurrent state change.
            try:
                await asyncio.to_thread(
                    self.data_manager.atomic_revert_closing, ticket['ticket_id']
                )
            except Exception:
                pass
            return False
        
        ticket['status'] = 'closed'
        ticket['closed_at'] = datetime.now(timezone.utc).isoformat()
        try:
            await self.data_manager.async_save_ticket(ticket)
        except Exception as e:
            logging.error(f"[TicketTool] Could not persist final 'closed' status: {e}")
        
        settings = await self.data_manager.async_load_ticket_settings(channel.guild.id) \
            if hasattr(self.data_manager, 'async_load_ticket_settings') \
            else self.data_manager.load_ticket_settings(channel.guild.id)
        settings = settings or {}

        # Resolve the panel row once for the close flow (two-step check, log
        # detail, recycle check all need it).
        panel_row = None
        if ticket.get('panel_id'):
            try:
                panel_row = self.data_manager.load_ticket_panel(ticket['panel_id'])
            except Exception:
                panel_row = None

        # When the premium transcript config is ENABLED, the premium
        # on_ticket_close hook (below) posts the transcript with all the
        # custom message / DM / archive options — skip the default posting so
        # we don't double-post (and double-DM) the same transcript.
        premium_transcripts_active = False
        if PREMIUM_AVAILABLE:
            try:
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    tr_cfg = TicketTool.transcripts.get_config(pdb)
                    premium_transcripts_active = bool(tr_cfg and tr_cfg.get('enabled'))
            except Exception:
                premium_transcripts_active = False

        if not premium_transcripts_active:
            transcripts_channel_id = settings.get('transcripts_channel_id') or getattr(config.channels, 'transcripts', None)
            if transcripts_channel_id:
                transcripts_channel = channel.guild.get_channel(transcripts_channel_id)
                if transcripts_channel:
                    try:
                        await transcripts_channel.send(embed=transcript['embed'], file=transcript['file'])
                        try:
                            await log_ticket_event(channel.guild, 'transcript', ticket, actor=closed_by,
                                                   detail='Posted to transcripts channel')
                        except Exception:
                            pass
                    except Exception as e:
                        logging.warning(f"[Tickets] Could not post transcript to transcripts channel: {e}")
        
        if not premium_transcripts_active and settings.get('dm_transcripts') and ows_get("dm_transcript_on_close"):
            try:
                creator = channel.guild.get_member(ticket['creator_id'])
                if creator:
                    from io import BytesIO
                    dm_file = discord.File(
                        BytesIO(transcript['html'].encode('utf-8')),
                        filename=f"transcript-{ticket['ticket_id']}.html"
                    )
                    await creator.send(
                        embed=discord.Embed(
                            title=f"Ticket Closed - {channel.guild.name}",
                            description=f"Your ticket has been closed.\n**Reason:** {reason}\n\nYour transcript is attached below.",
                            color=discord.Color.orange()
                        ),
                        file=dm_file
                    )
            except Exception as e:
                logging.warning(f"[Tickets] Could not DM transcript to user: {e}")

        # TicketTool-style ticket logging: "Ticket Closed" entry.
        try:
            await log_ticket_event(
                channel.guild, 'closed', ticket, actor=closed_by,
                detail=f"Reason: {reason}",
            )
        except Exception:
            pass

        # --- PREMIUM TIER 1: on_ticket_close hook ---
        # Fires BEFORE channel.delete() so the channel still exists for any
        # final actions (rename to closed template, post custom transcript,
        # apply close role-automation, mark SLA resolution met, cancel delayed
        # automations, fire the 'closed' trigger). Reload the ticket so the
        # hook sees the committed 'closed' status.
        if PREMIUM_AVAILABLE:
            try:
                closed_ticket = await self.data_manager.async_load_ticket(ticket['ticket_id'])
                await TicketTool.wiring.on_ticket_close(
                    bot=self.bot, ticket_tool=self, channel=channel,
                    ticket=closed_ticket or ticket, closed_by=closed_by,
                    transcript=transcript, panel=panel_row,
                )
            except Exception as exc:
                logging.warning(f"[TicketTool] TicketTool.on_ticket_close failed: {exc}")

        # --- TicketTool "Two Step Ticket": retain the closed channel ---
        # When the panel enables two_step_ticket, do NOT delete/recycle the
        # channel: apply the closed permission set, move to the closed
        # category, and post the moderator message (Re-Open / Delete /
        # Transcript buttons). Staff can later re-open in place.
        if panel_row and panel_row.get('two_step_ticket') and isinstance(channel, discord.TextChannel):
            try:
                closed_ticket = await self.data_manager.async_load_ticket(ticket['ticket_id'])
                await apply_closed_ticket_state(channel, closed_ticket or ticket, panel_row, closed_by)
                return True
            except Exception as exc:
                logging.warning(f"[TwoStep] closed-state application failed, falling back to delete: {exc}")

        # --- PREMIUM TIER 2: channel recycling check ---
        # If the panel has recycling enabled, recycle the channel instead of
        # deleting it. on_ticket_close_recycle_check returns True if recycled.
        recycled = False
        if PREMIUM_AVAILABLE:
            try:
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    recycled = await TicketTool.wiring.on_ticket_close_recycle_check(
                        bot=self.bot, pdb=pdb, channel=channel, panel=panel_row,
                    )
            except Exception as exc:
                logging.warning(f"[TicketTool] recycle check failed: {exc}")

        if not recycled:
            try:
                await channel.delete(reason=f"Ticket closed by {closed_by}: {reason}")
            except Exception as e:
                logging.warning(f"[Tickets] Could not delete channel {channel.id}: {e}")
        return True
    
    def _is_ticket_staff(self, member: discord.Member, ticket: Dict) -> bool:
        """Return True if `member` is allowed to act as staff on this ticket.

        Staff = a member with Manage Channels/Administrator, OR a member who
        holds the ticket's support role (resolved from the ticket's panel
        first, falling back to the guild ticket settings). The ticket creator
        is NEVER considered staff for their own ticket — this is what prevents
        a user from claiming their own ticket via the Claim button.
        """
        if member is None:
            return False
        try:
            if member.guild_permissions.administrator or member.guild_permissions.manage_channels:
                return True
        except Exception:
            pass

        support_role_id = None
        panel_id = ticket.get('panel_id') if ticket else None
        if panel_id:
            try:
                panel = self.data_manager.load_ticket_panel(panel_id)
                if panel:
                    support_role_id = panel.get('support_role_id')
            except Exception:
                pass
        if not support_role_id:
            try:
                settings = self.data_manager.load_ticket_settings(member.guild.id)
                if settings:
                    support_role_id = settings.get('support_role_id')
            except Exception:
                pass
        if support_role_id:
            role = member.guild.get_role(support_role_id)
            if role and role in member.roles:
                return True
        return False

    async def claim_ticket(self, channel: discord.TextChannel, user: discord.Member) -> Tuple[bool, str]:
        """Claim a ticket atomically.

        Uses a single conditional UPDATE so two staff members clicking Claim
        at nearly the same time can't both succeed — only the first UPDATE
        affects a row, the second is a no-op.

        PERMISSION FIX: only staff (Manage Channels / Administrator / support
        role) may claim, and the ticket creator is explicitly blocked from
        claiming their own ticket. Without this, a ticket creator who has
        send_messages access to the channel could click Claim and take
        ownership of their own support ticket.
        """
        ticket = await self.data_manager.async_load_ticket_by_channel(channel.id)
        if not ticket:
            return False, "This is not a ticket channel."

        # --- PREMIUM TIER 1: advanced-claim policy ---
        # Honors per-panel allow_owner_claim / auto_replace_claimer. If premium
        # is unavailable or has no config, this block is a no-op and the
        # original (stricter) logic below applies unchanged.
        panel_row = None
        if PREMIUM_AVAILABLE and ticket.get('panel_id'):
            try:
                panel_row = self.data_manager.load_ticket_panel(ticket['panel_id'])
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    allowed, why = TicketTool.claiming.should_allow_claim(
                        pdb, member=user, ticket=ticket, panel=panel_row,
                        is_staff=self._is_ticket_staff(user, ticket),
                    )
                    if not allowed:
                        return False, why
                    # auto_replace_claimer: clear any existing claim first so
                    # the atomic UPDATE below succeeds.
                    if TicketTool.claiming.should_auto_replace(pdb, panel_row) and ticket.get('claimed_by'):
                        try:
                            await asyncio.to_thread(
                                self.data_manager.atomic_clear_claim, ticket['ticket_id']
                            )
                        except AttributeError:
                            # atomic_clear_claim not present: fall back to a
                            # direct save (slightly less race-safe but fine).
                            ticket['claimed_by'] = None
                            ticket['claimed_at'] = None
                            self.data_manager.save_ticket(ticket)
                        ticket = await self.data_manager.async_load_ticket_by_channel(channel.id)
            except Exception as exc:
                logging.warning(f"[TicketTool] premium claim policy failed: {exc}")

        # The ticket creator may never claim their own ticket.
        if ticket.get('creator_id') == user.id:
            return False, "You can't claim your own ticket. Please wait for staff to respond."

        # Only staff may claim.
        if not self._is_ticket_staff(user, ticket):
            return False, "Only staff can claim tickets."

        success, msg = await asyncio.to_thread(
            self.data_manager.atomic_claim_ticket, ticket['ticket_id'], user.id
        )
        if not success:
            # Already claimed — return a friendly message naming the claimer.
            existing_id = ticket.get('claimed_by')
            if not existing_id:
                # Reload to get the freshly written claimer.
                fresh = await self.data_manager.async_load_ticket(ticket['ticket_id'])
                existing_id = fresh.get('claimed_by') if fresh else None
            if existing_id:
                claimer = channel.guild.get_member(existing_id)
                return False, f"This ticket is already claimed by {claimer.mention if claimer else 'someone'}."
            return False, msg

        # --- PREMIUM TIER 1: on_ticket_claim hook ---
        # Applies rename/move/hide/perms/custom-message + claim role-automation
        # + fires the 'claim' trigger. Reload the ticket so the hook sees the
        # committed claimed_by.
        if PREMIUM_AVAILABLE:
            try:
                claimed_ticket = await self.data_manager.async_load_ticket(ticket['ticket_id'])
                await TicketTool.wiring.on_ticket_claim(
                    bot=self.bot, ticket_tool=self, channel=channel,
                    ticket=claimed_ticket or ticket, panel=panel_row, claimer=user,
                )
            except Exception as exc:
                logging.warning(f"[TicketTool] TicketTool.on_ticket_claim failed: {exc}")

        # TicketTool-style ticket logging: "Ticket Claimed" entry.
        try:
            await log_ticket_event(channel.guild, 'claim', ticket, actor=user)
        except Exception:
            pass
        return True, f"Ticket claimed by {user.mention}"
    
    async def unclaim_ticket(self, channel: discord.TextChannel, user: discord.Member) -> Tuple[bool, str]:
        """Release a ticket claim."""
        ticket = self.data_manager.load_ticket_by_channel(channel.id)
        if not ticket:
            return False, "This is not a ticket channel."

        if not ticket.get('claimed_by'):
            return False, "This ticket is not claimed."

        # --- PREMIUM TIER 1: advanced-claim unclaim gating ---
        # only_claimer_unclaim is enforced here (admin always bypasses).
        panel_row = None
        if PREMIUM_AVAILABLE and ticket.get('panel_id'):
            try:
                panel_row = self.data_manager.load_ticket_panel(ticket['panel_id'])
                pdb = getattr(self.bot, 'premium_db', None)
                if pdb is not None:
                    allowed, why = TicketTool.claiming.is_authorized(
                        pdb, actor=user, action='unclaim', ticket=ticket,
                        panel=panel_row,
                        is_admin=user.guild_permissions.administrator,
                    )
                    if not allowed:
                        return False, why
            except Exception as exc:
                logging.warning(f"[TicketTool] premium unclaim policy failed: {exc}")

        if ticket['claimed_by'] != user.id and not user.guild_permissions.administrator:
            return False, "You can only unclaim tickets you claimed (or be admin)."

        # Capture the previous claimer so the side-effects hook can reverse
        # their claim roles (role_automation 'unclaim' is applied to the
        # CLAIMER, not the actor who released it).
        previous_claimer_id = ticket.get('claimed_by')
        ticket['claimed_by'] = None
        ticket['claimed_at'] = None
        self.data_manager.save_ticket(ticket)

        # --- PREMIUM TIER 1: on_ticket_unclaim hook ---
        # Reverses rename/perms/custom-message + applies unclaim role-automation
        # + fires the 'unclaim' trigger.
        if PREMIUM_AVAILABLE:
            try:
                unclaimer = user
                if previous_claimer_id and int(previous_claimer_id) != user.id:
                    unclaimer = channel.guild.get_member(int(previous_claimer_id)) or user
                await TicketTool.wiring.on_ticket_unclaim(
                    bot=self.bot, ticket_tool=self, channel=channel,
                    ticket=ticket, panel=panel_row, unclaimer=unclaimer,
                )
            except Exception as exc:
                logging.warning(f"[TicketTool] TicketTool.on_ticket_unclaim failed: {exc}")

        # TicketTool-style ticket logging: "Ticket Unclaimed" entry.
        try:
            await log_ticket_event(channel.guild, 'unclaim', ticket, actor=user)
        except Exception:
            pass
        return True, "Ticket unclaimed."
    
    async def _generate_transcript(self, channel: discord.TextChannel, ticket: Dict,
                                   closed_by: discord.Member, limit: Optional[int] = None) -> Dict:
        """Generate HTML transcript like Ticket Tool.

        `limit` caps the number of messages included (Ticket Tool caps
        transcripts at 1000 messages); None pulls the entire history.
        """
        from io import BytesIO
        
        messages = []
        message_count = 0
        
        # limit=None pulls the entire channel history. Discord paginates this
        # under the hood (100 msgs per request), so very long tickets take
        # longer but are no longer silently truncated at 500 messages.
        # When a limit is requested we fetch newest-first then reverse, so the
        # transcript contains the MOST RECENT `limit` messages in order.
        history_limit = limit if limit is not None else None
        oldest_first = limit is None
        async for msg in channel.history(limit=history_limit, oldest_first=oldest_first):
            if msg.author.bot and msg.embeds:
                continue  # Skip bot embeds
            
            message_count += 1
            messages.append({
                'author_id': msg.author.id,
                'author_name': msg.author.display_name,
                'author_avatar': str(msg.author.avatar.url) if msg.author.avatar else str(msg.author.default_avatar.url),
                'content': msg.content,
                'attachments': [att.url for att in msg.attachments],
                'timestamp': msg.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'embeds': len(msg.embeds)
            })
        if limit is not None:
            messages.reverse()

        # FALLBACK: if channel history is empty (e.g. messages were
        # bulk-deleted, or the channel was partially lost before close),
        # reconstruct the message list from the ticket_messages backup table
        # that on_message populates. This keeps the transcript usable even
        # when Discord's own history is gone, and gives the previously-unused
        # ticket_messages table a real purpose.
        if not messages:
            try:
                backup = await self.data_manager.async_load_ticket_messages(ticket['ticket_id'])
            except Exception as exc:
                logging.warning(f"[TicketTool] ticket_messages fallback load failed: {exc}")
                backup = []
            for row in backup:
                raw_atts = row.get('attachments')
                if isinstance(raw_atts, str):
                    try:
                        atts = json.loads(raw_atts) if raw_atts else []
                    except Exception:
                        atts = []
                elif isinstance(raw_atts, list):
                    atts = raw_atts
                else:
                    atts = []
                # Normalize the stored created_at ISO timestamp into the
                # 'YYYY-MM-DD HH:MM:SS' display format the transcript expects.
                ts_raw = row.get('created_at') or ''
                try:
                    parsed = datetime.fromisoformat(ts_raw.replace('Z', '+00:00'))
                    ts_disp = parsed.strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    ts_disp = ts_raw[:19] if ts_raw else ''
                messages.append({
                    'author_id': row.get('author_id'),
                    'author_name': row.get('author_name', 'Unknown'),
                    'author_avatar': row.get('author_avatar', ''),
                    'content': row.get('content', ''),
                    'attachments': atts,
                    'timestamp': ts_disp,
                    'embeds': 0,
                })
            message_count = len(messages)
            if messages:
                logging.info(f"[TicketTool] Transcript used ticket_messages fallback ({message_count} rows) for {ticket['ticket_id']}")

        # Generate HTML (all user-controlled values are escaped inside).
        html_content = self._generate_html_transcript(channel, ticket, messages, closed_by)
        
        # Save transcript to database
        transcript_id = str(_uuid.uuid4())[:8]
        transcript_data = {
            'transcript_id': transcript_id,
            'ticket_id': ticket['ticket_id'],
            'guild_id': channel.guild.id,
            'channel_id': channel.id,
            'creator_id': ticket['creator_id'],
            'closed_by': closed_by.id,
            'claimed_by': ticket.get('claimed_by'),
            'category': ticket.get('category'),
            'created_at': ticket.get('created_at'),
            'closed_at': datetime.now(timezone.utc).isoformat(),
            'message_count': message_count,
            'html_content': html_content
        }
        await self.data_manager.async_save_transcript(transcript_data)
        
        # Create embed with better formatting like Ticket Tool
        embed = discord.Embed(
            title=f"📋 Ticket Transcript - {ticket['ticket_id']}",
            description=(
                f"**Type:** {ticket.get('category', 'General')}\n"
                f"**Category:** {_resolve_ticket_category_for_display(channel.guild.id, ticket)}\n"
                f"**Subject:** {ticket.get('subject', 'N/A')}"
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        creator = channel.guild.get_member(ticket['creator_id'])
        embed.add_field(name="👤 Creator", value=creator.mention if creator else f"<@{ticket['creator_id']}>", inline=True)
        embed.add_field(name="🔒 Closed By", value=closed_by.mention, inline=True)
        embed.add_field(name="💬 Messages", value=str(message_count), inline=True)
        
        if ticket.get('claimed_by'):
            claimer = channel.guild.get_member(ticket['claimed_by'])
            embed.add_field(name="🙋 Claimed By", value=claimer.mention if claimer else f"<@{ticket['claimed_by']}>", inline=True)
        
        # Add message preview (first 5 messages) - this was missing!
        if messages:
            preview_text = ""
            for i, msg in enumerate(messages[:5]):
                content_preview = msg.get('content', '')[:100]
                if len(msg.get('content', '')) > 100:
                    content_preview += "..."
                preview_text += f"**{msg.get('author_name', 'Unknown')}:** {content_preview}\n"
            if len(messages) > 5:
                preview_text += f"\n*...and {len(messages) - 5} more messages*"
            embed.add_field(name="📝 Message Preview", value=preview_text or "No messages", inline=False)
        
        # Add ticket duration
        if ticket.get('created_at'):
            try:
                created = datetime.fromisoformat(ticket['created_at'].replace('Z', '+00:00'))
                duration = datetime.now(timezone.utc) - created
                hours, remainder = divmod(int(duration.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                duration_str = f"{hours}h {minutes}m {seconds}s" if hours > 0 else f"{minutes}m {seconds}s"
                embed.add_field(name="⏱️ Duration", value=duration_str, inline=True)
            except:
                pass
        
        embed.set_footer(text=f"Ticket ID: {ticket['ticket_id']} • Download HTML for full transcript")
        
        # Create file
        file = discord.File(
            BytesIO(html_content.encode('utf-8')),
            filename=f"transcript-{ticket['ticket_id']}.html"
        )
        
        return {'embed': embed, 'file': file, 'html': html_content}
    
    def _generate_html_transcript(self, channel: discord.TextChannel, ticket: Dict, messages: List[Dict], closed_by: discord.Member) -> str:
        """Generate a Ticket Tool style HTML transcript.

        SECURITY: every value that comes from a user-controlled source
        (message content, display names, attachment URLs, panel names) is
        HTML-escaped via _esc() / _safe_url() before being interpolated.
        This closes the XSS vector where a ticket message containing
        `<img src=x onerror=...>` would execute when the transcript was
        opened in a browser.
        """
        guild = channel.guild
        creator = guild.get_member(ticket['creator_id'])

        # Pre-escape header values.
        esc_ticket_id = _esc(ticket['ticket_id'])
        esc_category = _esc(ticket.get('category', 'General'))
        esc_created_at = _esc((ticket.get('created_at') or 'N/A')[:19])
        esc_creator = _esc(creator.display_name if creator else 'Unknown')
        esc_closed_by = _esc(closed_by.display_name)
        esc_bot_name = _esc(self.bot.user.name if self.bot.user else 'Bot')
        claimed_by = ticket.get('claimed_by')
        closed_now = _esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ticket Transcript - {esc_ticket_id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #fff;
        }}
        .container {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
        .header {{
            background: linear-gradient(135deg, #5865F2 0%, #7289DA 100%);
            padding: 30px;
            border-radius: 15px;
            margin-bottom: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
        }}
        .header h1 {{ font-size: 28px; margin-bottom: 10px; }}
        .header .info {{ display: flex; gap: 20px; flex-wrap: wrap; margin-top: 15px; }}
        .header .info-item {{ 
            background: rgba(255,255,255,0.1);
            padding: 8px 15px;
            border-radius: 8px;
            font-size: 14px;
        }}
        .messages {{ background: #2f3136; border-radius: 15px; overflow: hidden; }}
        .message {{
            padding: 15px 20px;
            border-bottom: 1px solid #36393f;
            display: flex;
            gap: 15px;
        }}
        .message:hover {{ background: rgba(79, 84, 92, 0.16); }}
        .message:last-child {{ border-bottom: none; }}
        .message-avatar {{ width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0; }}
        .message-content {{ flex: 1; }}
        .message-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 5px; }}
        .message-author {{ font-weight: 600; color: #fff; }}
        .message-timestamp {{ font-size: 12px; color: #72767d; }}
        .message-text {{ color: #dcddde; line-height: 1.5; word-wrap: break-word; white-space: pre-wrap; }}
        .attachment {{
            background: #2f3136;
            border: 1px solid #4f545c;
            border-radius: 8px;
            padding: 10px;
            margin-top: 8px;
            display: inline-block;
        }}
        .attachment a {{ color: #00b0f4; text-decoration: none; }}
        .footer {{
            text-align: center;
            padding: 20px;
            color: #72767d;
            font-size: 14px;
        }}
        .claimed-badge {{
            background: #faa61a;
            color: #000;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 12px;
            margin-left: 10px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Ticket Transcript</h1>
            <div class="info">
                <div class="info-item">ID: {esc_ticket_id}</div>
                <div class="info-item">Category: {esc_category}</div>
                <div class="info-item">Created: {esc_created_at}</div>
                <div class="info-item">Closed: {closed_now}</div>
            </div>
            <div class="info" style="margin-top: 10px;">
                <div class="info-item">Creator: {esc_creator}</div>
                <div class="info-item">Closed By: {esc_closed_by}</div>
                {f'<div class="info-item">Claimed By: {_esc(claimed_by)}</div>' if claimed_by else ''}
            </div>
        </div>
        <div class="messages">
"""
        for msg in messages:
            # Escape every user-controlled field. content is rendered with
            # white-space: pre-wrap so newlines are preserved without <br>.
            esc_author_name = _esc(msg.get('author_name', 'Unknown'))
            esc_author_avatar = _safe_url(msg.get('author_avatar', ''))
            esc_timestamp = _esc(msg.get('timestamp', ''))
            content = msg.get('content') or ''
            esc_content = _esc(content) if content else '<em>No content</em>'

            html += f"""
            <div class="message">
                <img class="message-avatar" src="{esc_author_avatar}" alt="Avatar">
                <div class="message-content">
                    <div class="message-header">
                        <span class="message-author">{esc_author_name}</span>
                        <span class="message-timestamp">{esc_timestamp}</span>
                    </div>
                    <div class="message-text">{esc_content}</div>
"""
            for att in (msg.get('attachments') or []):
                safe_att = _safe_url(att)
                if safe_att:
                    html += f"""
                    <div class="attachment"><a href="{safe_att}" target="_blank" rel="noopener noreferrer">📎 Attachment</a></div>
"""
            html += """
                </div>
            </div>
"""

        html += f"""
        </div>
        <div class="footer">
            Generated by {esc_bot_name} • {closed_now}
        </div>
    </div>
</body>
</html>"""

        return html


# Global ticket tool instance
ticket_tool: Optional[TicketToolSystem] = None


def build_ticket_commands_embed(panel: Optional[Dict] = None) -> discord.Embed:
    """Build an embed listing ONLY the commands available inside a ticket channel.

    This is shown the moment a ticket opens, BEFORE the welcome message, so the
    user immediately knows which commands they (and staff) can run in-ticket.
    Only in-ticket commands are listed — panel/settings/blacklist admin commands
    are intentionally excluded.
    """
    try:
        color = discord.Color(panel.get('embed_color', 0x5865F2)) if panel else discord.Color.blurple()
    except (TypeError, ValueError):
        color = discord.Color.blurple()

    embed = discord.Embed(
        title="📋 Ticket Commands",
        description=(
            "Welcome! Here are the commands you can use inside this ticket.\n"
            "Use them with the bot prefix."
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="🙋 Claiming & Closing",
        value=(
            "`!claim` — Claim this ticket\n"
            "`!unclaim` — Release your claim\n"
            "`!close [reason]` — Close this ticket\n"
            "`!closerequest [reason]` — Request staff to close (alias `!ca`)\n"
            "`!rate` — Staff: send the rating prompt"
        ),
        inline=False,
    )

    embed.add_field(
        name="⏸️ Automation & Info",
        value=(
            "`!pause [duration]` — Pause all automations (30m/1h/2d/1w)\n"
            "`!resume` — Resume automations\n"
            "`!ticket-info` — Full status overview of this ticket"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔒 Privacy",
        value=(
            "`!private` — Hide this ticket from other staff\n"
            "`!unprivate` — Restore staff access"
        ),
        inline=False,
    )

    embed.add_field(
        name="📜 Transcripts",
        value="`!transcript [channel] [lines]` — Generate a transcript of this ticket",
        inline=False,
    )

    embed.add_field(
        name="👥 Members",
        value=(
            "`!add @user|@role` — Add a user or role to this ticket\n"
            "`!remove @user|@role` — Remove a user or role from this ticket"
        ),
        inline=False,
    )

    embed.add_field(
        name="📝 Notes & Priority",
        value=(
            "`!note <text>` — Add a private staff note\n"
            "`!notes` — View staff notes for this ticket\n"
            "`!priority <level>` — Set priority (low/normal/high/urgent)\n"
            "`!setcategory` — Set this ticket's category (folder)"
        ),
        inline=False,
    )

    embed.add_field(
        name="💬 Canned Replies",
        value=(
            "`!canned send <name>` — Insert a saved response\n"
            "`!canned list` — Browse saved responses"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔁 Channel Management",
        value=(
            "`!rename <name>` — Rename this ticket channel\n"
            "`!move <panel_id>` — Move this ticket to another category\n"
            "`!reopen <ticket_id>` — Reopen a closed ticket"
        ),
        inline=False,
    )

    embed.set_footer(text="Ticket Commands • Available in this ticket channel")
    return embed


# =============================================================================
# TICKET LOGGING (TicketTool-style Logging Channel)
# =============================================================================
# Ticket Tool's free tier logs toggleable ticket actions (Created, Closed,
# (Re)Opened, Renamed, Deleted, Transcript Saved) to a configured log channel.
# The bot's ticket_settings.log_channel_id column existed but was never read;
# this implementation makes it functional, with a configurable event list.
# =============================================================================

TICKET_LOG_EVENT_INFO: Dict[str, Tuple[str, int]] = {
    'created':   ('🎫 Ticket Created',     0x57F287),
    'closed':    ('🔒 Ticket Closed',      0xE67E22),
    'reopened':  ('🔓 Ticket Reopened',    0x57F287),
    'renamed':   ('✏️ Ticket Renamed',     0x5865F2),
    'deleted':   ('🗑️ Ticket Deleted',     0xED4245),
    'transcript': ('📜 Transcript Saved',  0x5865F2),
    'claim':     ('🙋 Ticket Claimed',     0x57F287),
    'unclaim':   ('🙋 Ticket Unclaimed',   0xE67E22),
    'priority':  ('🚨 Ticket Priority Changed', 0xFEE75C),
    'category':  ('📁 Ticket Category Changed', 0x5865F2),
}
# Events logged when the guild has no explicit log_events config
# (matches Ticket Tool's free-tier default action set).
DEFAULT_TICKET_LOG_EVENTS = ['created', 'closed', 'reopened', 'renamed', 'deleted', 'transcript']


def get_ticket_log_events(guild_id: int) -> List[str]:
    """Parse a guild's configured ticket-log event list (JSON array)."""
    try:
        settings = data_manager.load_ticket_settings(guild_id) or {}
        raw = settings.get('log_events')
        if raw:
            events = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(events, list):
                return [str(e) for e in events if e in TICKET_LOG_EVENT_INFO]
    except Exception:
        pass
    return list(DEFAULT_TICKET_LOG_EVENTS)


async def log_ticket_event(guild: discord.Guild, event: str, ticket: Optional[Dict],
                           *, actor: Optional[discord.abc.User] = None,
                           detail: Optional[str] = None,
                           channel_ref: Optional[discord.abc.GuildChannel] = None) -> bool:
    """Post a TicketTool-style log entry to the guild's ticket log channel.

    Returns True when an embed was actually posted. Silently no-ops when no
    log channel is configured or the event is not in the guild's event list,
    so call sites never need their own guards.
    """
    if guild is None or data_manager is None:
        return False
    try:
        settings = data_manager.load_ticket_settings(guild.id)
    except Exception:
        settings = None
    if not settings or not settings.get('log_channel_id'):
        return False
    if event not in get_ticket_log_events(guild.id):
        return False
    log_channel = guild.get_channel(settings['log_channel_id'])
    if log_channel is None:
        return False

    title, color = TICKET_LOG_EVENT_INFO.get(event, (f'Ticket {event}', 0x5865F2))
    embed = discord.Embed(title=title, color=discord.Color(color),
                          timestamp=datetime.now(timezone.utc))

    desc_lines: List[str] = []
    if ticket:
        ticket_channel_id = ticket.get('channel_id')
        channel_mention = f"<#{ticket_channel_id}>" if ticket_channel_id else "`deleted`"
        desc_lines.append(f"**Ticket:** `{ticket.get('ticket_id', '?')}` ({channel_mention})")
        creator_id = ticket.get('creator_id')
        if creator_id:
            desc_lines.append(f"**Creator:** <@{creator_id}>")
        panel_name = ticket.get('category')
        if panel_name:
            desc_lines.append(f"**Panel:** {panel_name}")
    if actor:
        desc_lines.append(f"**By:** {actor.mention} (`{actor.display_name}`)")
    if channel_ref is not None and not ticket:
        desc_lines.append(f"**Channel:** <#{channel_ref.id}>")
    if detail:
        desc_lines.append(f"**Detail:** {detail}")
    embed.description = '\n'.join(desc_lines) or None
    embed.set_footer(text=f"Ticket Log • {guild.name}")

    try:
        await log_channel.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        logging.warning(f"[TicketLog] Could not post {event} event: {exc}")
        return False


# --- TICKET CATEGORY HELPERS (internal ticket "folders") ---
# A Ticket Category groups related tickets inside the bot (e.g. a "Staff"
# category containing "Apply for Staff" and "Staff Training" tickets). It is
# completely separate from Discord channel categories — tickets in the same
# category can still live in the same Discord channel category.
UNCATEGORIZED_LABEL = 'Uncategorized'
_CUSTOM_EMOJI_RE = re.compile(r'^<a?(:[^:]+:)(\d{15,21})>$')


def _validate_category_emoji(raw: str) -> Tuple[bool, str]:
    """Validate an optional category emoji/icon.

    Accepts either a single unicode emoji (or any short non-space string,
    e.g. '🎫' or '⭐') or a full custom emoji mention like ``<:name:id>`` /
    ``<a:name:id>``. Returns (ok, cleaned_value).
    """
    value = (raw or '').strip()
    if not value:
        return True, ''  # empty = no icon, always allowed
    if ':' in value:
        if _CUSTOM_EMOJI_RE.match(value):
            return True, value
        return False, value
    if len(value) > 32 or any(ch.isspace() for ch in value):
        return False, value
    return True, value


def _ticket_category_label(guild_id: Optional[int], category_id: Optional[str]) -> str:
    """Display label ('emoji Name' or plain name) for a category id.

    Falls back to ``Uncategorized`` when the id is empty/unknown — this is
    what tickets created before the feature (or after a category deletion)
    show. Uses the sync DataManager because every call site here already runs
    in a worker thread or is a cheap indexed lookup.
    """
    if category_id and data_manager is not None:
        try:
            row = data_manager.load_ticket_category(category_id)
            if row and row.get('guild_id') == (guild_id if guild_id is not None else row.get('guild_id')):
                emoji = (row.get('emoji') or '').strip()
                return f"{emoji} {row['name']}".strip()
        except Exception:
            pass
    return UNCATEGORIZED_LABEL


def _resolve_ticket_category_for_display(guild_id: Optional[int], ticket: Optional[Dict]) -> str:
    """Label for a ticket's category row in embeds (Type/Category split:
    ``ticket['category']`` is the panel name = ticket TYPE; the new
    ``ticket['ticket_category_id']`` is the internal folder)."""
    if not ticket:
        return UNCATEGORIZED_LABEL
    return _ticket_category_label(guild_id, ticket.get('ticket_category_id'))


def _member_has_limit_bypass(member: discord.Member, panel: Optional[Dict],
                             settings: Optional[Dict]) -> bool:
    """TicketTool-style limit bypass: members holding any bypass role skip the
    open-ticket limits. Bypass roles are configured per panel
    (ticket_panels.limit_bypass_role_ids, JSON array)."""
    if member is None:
        return False
    raw = panel.get('limit_bypass_role_ids') if panel else None
    if not raw:
        return False
    try:
        role_ids = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return False
    if not isinstance(role_ids, list) or not role_ids:
        return False
    member_role_ids = {r.id for r in getattr(member, 'roles', [])}
    return any(int(rid) in member_role_ids for rid in role_ids)


def _ticket_automation_paused(ticket: Optional[Dict]) -> bool:
    """Pure pause check for Bot.py-side automation consumers (auto-close).

    A pause is active when automation_paused=1 AND (no auto-resume deadline
    OR the deadline is still in the future). Mirrors
    TicketTool.automations.is_ticket_paused without importing the package.
    """
    if not ticket or not int(ticket.get('automation_paused') or 0):
        return False
    until = ticket.get('automation_paused_until')
    if until:
        try:
            deadline = datetime.fromisoformat(str(until).replace('Z', '+00:00'))
            if deadline <= datetime.now(timezone.utc):
                return False  # expired
        except (ValueError, TypeError):
            pass
    return True


_DURATION_RE = None  # compiled lazily


def parse_pause_duration(raw: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """Parse a Ticket Tool-style pause duration ("30m", "1h", "2d", "1w").

    Returns (seconds, error). Passing None / "indefinite" / "forever" (or an
    unparseable value) yields an indefinite pause: (None, None)."""
    import re as _re
    global _DURATION_RE
    if _DURATION_RE is None:
        _DURATION_RE = _re.compile(r'^\s*(\d{1,4})\s*([smhdw])\s*$', _re.IGNORECASE)
    if not raw or not str(raw).strip():
        return None, None  # indefinite
    text = str(raw).strip().lower()
    if text in ('indefinite', 'forever', 'inf', 'none', '-'):
        return None, None
    m = _DURATION_RE.match(text)
    if not m:
        return None, (f"Invalid duration `{raw}`. Use e.g. `30m`, `1h`, `2d`, `1w` — "
                      "or omit it for an indefinite pause.")
    value = int(m.group(1))
    unit = m.group(2).lower()
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    return value * multipliers[unit], None


def _build_panel_message_embeds(panel: Dict) -> List[discord.Embed]:
    """Build the embed(s) for a panel message.

    Uses the premium multi-embed set when the panel has multi-embed enabled
    and embeds configured; otherwise the classic single panel embed. This
    wires TicketTool.multi_embed.build_panel_embeds into the panel message
    rendering (previously configurable but never displayed).
    """
    default = discord.Embed(
        title=panel.get('embed_title') or 'Support Tickets',
        description=panel.get('embed_description') or 'Click the button below to create a ticket.',
        color=discord.Color(panel.get('embed_color', 0x5865F2)),
    )
    if not PREMIUM_AVAILABLE:
        return [default]
    try:
        pdb = getattr(bot, 'premium_db', None)
        if pdb is not None and TicketTool.multi_embed.is_multi_embed_enabled(panel):
            embeds = TicketTool.multi_embed.build_panel_embeds(pdb, panel)
            if embeds:
                return embeds[:10]
    except Exception as exc:
        logging.debug(f"[Premium] multi-embed panel build failed: {exc}")
    return [default]


# =============================================================================
# TWO-STEP TICKET (TicketTool "Two Step Ticket") + CLOSE REQUEST
# =============================================================================
# When a panel enables two_step_ticket, closing a ticket no longer deletes the
# channel: the ticket enters a Closed state (creator loses write access,
# channel moves to the closed category, a moderator message with
# Re-Open / Delete / Transcript buttons is posted). Staff can re-open the
# ticket in place at any time.
# =============================================================================

async def apply_closed_ticket_state(channel: discord.TextChannel, ticket: Dict,
                                    panel: Dict, closed_by: discord.Member) -> None:
    """Apply the TicketTool-style Closed state to a two-step ticket channel."""
    guild = channel.guild
    creator = guild.get_member(int(ticket.get('creator_id') or 0))
    support_role_id = panel.get('support_role_id')

    # 1) Closed permission set: creator + any added members lose access,
    #    support team keeps read-only visibility (Ticket Tool default closed
    #    permissions).
    try:
        for target, _overwrite in list(channel.overwrites.items()):
            if isinstance(target, discord.Member) and target != guild.me:
                try:
                    await channel.set_permissions(
                        target, view_channel=False, send_messages=False,
                        reason="Ticket closed (two-step)",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
            elif isinstance(target, discord.Role) and support_role_id and target.id == int(support_role_id):
                try:
                    await channel.set_permissions(
                        target, view_channel=True, send_messages=False,
                        read_message_history=True,
                        reason="Ticket closed (two-step)",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
    except Exception as exc:
        logging.warning(f"[TwoStep] closed permission pass failed: {exc}")

    # 2) Move to the closed category when configured.
    try:
        settings = data_manager.load_ticket_settings(guild.id) or {}
        closed_category_id = settings.get('closed_category_id')
        if closed_category_id and channel.category_id != int(closed_category_id):
            closed_category = guild.get_channel(int(closed_category_id))
            if isinstance(closed_category, discord.CategoryChannel):
                await channel.edit(category=closed_category, reason="Ticket closed (two-step)")
    except Exception as exc:
        logging.warning(f"[TwoStep] closed category move failed: {exc}")

    # 3) Post the moderator message with Re-Open / Delete / Transcript buttons.
    view = TicketModeratorView()
    embed = discord.Embed(
        title="🔒 Ticket Closed",
        description=(
            f"This ticket was closed by {closed_by.mention}.\n"
            f"**Reason:** {ticket.get('close_reason') or 'No reason provided'}\n\n"
            "Staff can re-open, delete, or export a transcript with the buttons below."
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    creator_mention = creator.mention if creator else f"<@{ticket.get('creator_id')}>"
    embed.add_field(name="Creator", value=creator_mention, inline=True)
    embed.add_field(name="Ticket ID", value=f"`{ticket.get('ticket_id')}`", inline=True)
    embed.set_footer(text="Two-Step Ticket • Closed state")
    try:
        await channel.send(content=creator_mention, embed=embed, view=view)
    except Exception as exc:
        logging.warning(f"[TwoStep] moderator message failed: {exc}")


async def reopen_ticket_in_place(channel, reopened_by: discord.Member) -> Tuple[bool, str]:
    """Re-open a two-step closed ticket whose channel still exists.

    Restores the open permission set, moves the channel back to the open
    category, applies the open-name template (premium), resets the ticket row,
    fires the premium 'reopened' hook, and logs the event.
    """
    if not ticket_tool:
        return False, "Ticket system not initialized."
    ticket = await ticket_tool.data_manager.async_load_ticket_by_channel(channel.id)
    if not ticket:
        return False, "This is not a ticket channel."
    if ticket.get('status') != 'closed':
        return False, "This ticket is not closed."

    guild = channel.guild
    panel = None
    if ticket.get('panel_id'):
        panel = data_manager.load_ticket_panel(ticket['panel_id'])
    settings = data_manager.load_ticket_settings(guild.id) or {}
    creator = guild.get_member(int(ticket.get('creator_id') or 0))
    support_role_id = (
        (panel.get('support_role_id') if panel else None)
        or settings.get('support_role_id')
        or getattr(config.roles, 'ticket_support', None)
    )

    # 1) Restore open permissions: creator full access, support team writable.
    if creator is not None:
        try:
            await channel.set_permissions(
                creator, view_channel=True, send_messages=True,
                read_message_history=True, attach_files=True,
                reason="Ticket reopened",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[TwoStep] reopen creator perms failed: {exc}")
    if support_role_id:
        role = guild.get_role(int(support_role_id))
        if role is not None:
            try:
                await channel.set_permissions(
                    role, view_channel=True, send_messages=True,
                    read_message_history=True, attach_files=True,
                    reason="Ticket reopened",
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                logging.warning(f"[TwoStep] reopen role perms failed: {exc}")

    # 2) Move back to the open category when one is configured.
    open_category_id = (
        (panel.get('category_id') if panel else None)
        or settings.get('category_id')
        or getattr(config.channels, 'tickets', None)
    )
    if open_category_id and getattr(channel, 'category_id', None) != int(open_category_id):
        open_category = guild.get_channel(int(open_category_id))
        if isinstance(open_category, discord.CategoryChannel):
            try:
                await channel.edit(category=open_category, reason="Ticket reopened")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logging.warning(f"[TwoStep] reopen category move failed: {exc}")

    # 3) Reset the ticket row (claim is cleared, matching Ticket Tool's
    #    auto-unclaim-on-reopen behavior).
    ticket['status'] = 'open'
    ticket['closed_at'] = None
    ticket['closed_by'] = None
    ticket['close_reason'] = None
    ticket['claimed_by'] = None
    ticket['claimed_at'] = None
    data_manager.save_ticket(ticket)

    # 4) Apply the premium open-name template when configured.
    if PREMIUM_AVAILABLE:
        try:
            pdb = getattr(bot, 'premium_db', None)
            if pdb is not None and panel:
                new_name, _subject = TicketTool.naming.compute_open_name(
                    pdb, panel,
                    guild={'id': guild.id, 'name': guild.name},
                    ticket_id=ticket['ticket_id'],
                    creator={'id': reopened_by.id, 'name': creator.display_name if creator else 'user'},
                    ticket_count=None,
                )
                if new_name:
                    await channel.edit(name=new_name, reason="Ticket reopened (open-name template)")
        except Exception as exc:
            logging.debug(f"[TwoStep] reopen rename failed: {exc}")

    # 5) Post the reopen notice with the standard control buttons.
    creator_mention = creator.mention if creator else f"<@{ticket.get('creator_id')}>"
    try:
        await channel.send(
            embed=discord.Embed(
                title=f"🔓 Ticket Reopened — #{ticket['ticket_id']}",
                description=(
                    f"This ticket was reopened by {reopened_by.mention}.\n"
                    f"{creator_mention} your ticket has been reopened."
                ),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            ),
            view=TicketControlView(ticket['ticket_id']),
        )
    except Exception as exc:
        logging.warning(f"[TwoStep] reopen notice failed: {exc}")

    # 6) Fire the premium 'reopened' automation trigger.
    if PREMIUM_AVAILABLE:
        try:
            await TicketTool.wiring.on_ticket_reopen(
                bot=bot, ticket_tool=ticket_tool, ticket=ticket,
                panel=panel, guild=guild,
            )
        except Exception as exc:
            logging.warning(f"[Premium] on_ticket_reopen (in-place) failed: {exc}")

    # 7) Log the reopen.
    try:
        await log_ticket_event(guild, 'reopened', ticket, actor=reopened_by)
    except Exception:
        pass
    logging.info(f"[Tickets] {reopened_by} reopened ticket {ticket['ticket_id']} in place")
    return True, f"Ticket `{ticket['ticket_id']}` reopened: {channel.mention}"


class TicketModeratorView(View):
    """TicketTool-style moderator message buttons on two-step closed tickets.

    PERSISTENCE: a single generic instance is registered in setup_hook; every
    handler resolves the ticket from the channel id, so the view keeps working
    after restarts.
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def _resolve_ticket(self, interaction: discord.Interaction) -> Optional[Dict]:
        if not ticket_tool:
            return None
        return await ticket_tool.data_manager.async_load_ticket_by_channel(interaction.channel.id)

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, 'guild_permissions', None)
        return bool(perms and (perms.administrator or perms.manage_channels))

    @discord.ui.button(label="Re-Open Ticket", style=discord.ButtonStyle.success, emoji="🔓", custom_id="ticketmod_reopen")
    async def reopen_button(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_staff(interaction):
            await interaction.response.send_message("Only staff can re-open tickets.", ephemeral=True)
            return
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        if ticket.get('status') != 'closed':
            await interaction.response.send_message("This ticket is not closed.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        ok, message = await reopen_ticket_in_place(interaction.channel, interaction.user)
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Delete Ticket", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="ticketmod_delete")
    async def delete_button(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_staff(interaction):
            await interaction.response.send_message("Only staff can delete tickets.", ephemeral=True)
            return
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        # Log the deletion before the channel (and its context) disappears.
        try:
            await log_ticket_event(interaction.guild, 'deleted', ticket, actor=interaction.user)
        except Exception:
            pass
        try:
            await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
            await interaction.followup.send(f"Could not delete the ticket channel: {exc}", ephemeral=True)

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="ticketmod_transcript")
    async def transcript_button(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_staff(interaction):
            await interaction.response.send_message("Only staff can export transcripts.", ephemeral=True)
            return
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            transcript = await ticket_tool._generate_transcript(interaction.channel, ticket, interaction.user)
            settings = data_manager.load_ticket_settings(interaction.guild.id) or {}
            posted = False
            transcripts_channel_id = (
                settings.get('transcripts_channel_id') or getattr(config.channels, 'transcripts', None)
            )
            if transcripts_channel_id:
                transcripts_channel = interaction.guild.get_channel(int(transcripts_channel_id))
                if transcripts_channel:
                    await transcripts_channel.send(embed=transcript['embed'], file=transcript['file'])
                    posted = True
                    try:
                        await log_ticket_event(interaction.guild, 'transcript', ticket, actor=interaction.user)
                    except Exception:
                        pass
            await interaction.followup.send(
                "Transcript generated." + (" It has been posted to the transcripts channel." if posted else ""),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"Transcript generation failed: {exc}", ephemeral=True)


class CloseRequestView(View):
    """TicketTool-style close request: the ticket owner asks staff to close.

    Staff confirm the close with the reason; the requester (or staff) can
    cancel. Firing the premium `close_request` automation trigger is handled
    by the /closerequest command.
    """

    def __init__(self, ticket_id: str, requester_id: int, reason: str):
        super().__init__(timeout=300)
        self.ticket_id = ticket_id
        self.requester_id = requester_id
        self.reason = reason
        self.handled = False

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, 'guild_permissions', None)
        return bool(perms and (perms.administrator or perms.manage_channels))

    @discord.ui.button(label="✅ Close Ticket", style=discord.ButtonStyle.danger)
    async def confirm_close(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_staff(interaction):
            await interaction.response.send_message(
                "Only staff can action a close request. The requester can cancel it.",
                ephemeral=True,
            )
            return
        if self.handled:
            await interaction.response.defer()
            return
        self.handled = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Close request accepted by {interaction.user.mention}. Closing…",
            view=self,
        )
        if not ticket_tool:
            return
        await ticket_tool.close_ticket(interaction.channel, interaction.user, self.reason)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_request(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.requester_id and not self._is_staff(interaction):
            await interaction.response.send_message(
                "Only the requester or staff can cancel this close request.",
                ephemeral=True,
            )
            return
        if self.handled:
            await interaction.response.defer()
            return
        self.handled = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Close request cancelled.", view=self,
        )


# --- TICKET PANEL VIEW (The panel message with create button) ---
PANEL_BUTTON_PREFIX = "create_ticket:"
PANEL_SELECT_CUSTOM_ID = "ticket_panel_select"


async def process_panel_create_request(interaction: discord.Interaction, panel: Dict) -> None:
    """Shared ticket-creation entry point for ALL panel UI styles.

    Runs the full gate chain (system enabled → blacklist → per-panel limit
    with bypass roles → premium business-hours schedule → panel questions)
    and then creates the ticket + welcome message. Used by:
      * TicketPanelView.create_button  (single-button panels)
      * MultiPanelView._on_click       (TicketTool attached panels)
      * TicketPanelSelectView          (TicketTool dropdown-style panels)
    """
    if not ticket_tool:
        await interaction.response.send_message("Ticket system not initialized.", ephemeral=True)
        return

    # Owner Settings gate: refuse new ticket creation when the Tickets
    # System is disabled. Checked here (the user-facing entry point) AND
    # inside create_ticket() so both paths are covered.
    if not ows_get("enable_tickets"):
        await interaction.response.send_message(
            "The ticket system is currently disabled by the server owner. Please try again later.",
            ephemeral=True,
        )
        return

    if not panel:
        await interaction.response.send_message("This ticket panel no longer exists.", ephemeral=True)
        return

    if ows_get("ticket_blacklist"):
        blacklisted, reason = ticket_tool.data_manager.is_user_blacklisted(interaction.guild.id, interaction.user.id)
        if blacklisted:
            await interaction.response.send_message(f"You are blacklisted from creating tickets. Reason: {reason}", ephemeral=True)
            return

    # Per-panel ticket limit with TicketTool-style bypass roles. Counts
    # only THIS panel's active tickets.
    if not _member_has_limit_bypass(interaction.user, panel, None):
        ticket_limit = panel.get('ticket_limit', 3)
        if ticket_limit:
            panel_count = ticket_tool.data_manager.count_active_tickets_by_creator_and_panel(
                interaction.user.id, interaction.guild.id, panel['panel_id'],
            )
            if panel_count >= int(ticket_limit):
                await interaction.response.send_message(
                    f"You already have {panel_count} open ticket(s) in this panel. Close one before creating another.",
                    ephemeral=True
                )
                return

    # --- PREMIUM TIER 1: business-hours scheduling gate ---
    # Checks the panel's configured schedule. If the panel is currently
    # closed AND the user doesn't hold a bypass role, refuse creation with
    # the panel's unavailable_message (and tell them when it next opens).
    if PREMIUM_AVAILABLE:
        try:
            pdb = getattr(interaction.client, 'premium_db', None)
            if pdb is not None:
                member = interaction.user
                role_ids = [r.id for r in member.roles] if hasattr(member, 'roles') else []
                is_open, unavailable_msg = TicketTool.scheduling.is_panel_open_now(
                    pdb, panel, role_ids,
                )
                if not is_open:
                    next_open = TicketTool.scheduling.next_open_time(pdb, panel)
                    extra = f"\n\n*Opens {next_open}.*" if next_open else ''
                    await interaction.response.send_message(
                        f"{unavailable_msg}{extra}", ephemeral=True
                    )
                    return
        except Exception as exc:
            logging.debug(f"[Premium] scheduling gate failed: {exc}")

    questions = ticket_tool.data_manager.load_panel_questions(panel['panel_id'])

    if questions:
        await interaction.response.send_modal(TicketQuestionsModal(panel, questions))
    else:
        await interaction.response.defer(thinking=True, ephemeral=True)
        channel, ticket_id = await ticket_tool.create_ticket(
            interaction.guild, interaction.user, panel
        )
        if channel:
            await TicketPanelView(panel)._send_welcome_message(channel, interaction.user, panel)
            await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)
        else:
            await interaction.followup.send(f"Failed to create ticket: {ticket_id}", ephemeral=True)


def build_multi_panel_view(row: Dict, panels: List[Dict]):
    """Build the correct persistent view for a stored multi-panel row.

    style='buttons' → MultiPanelView (one button per panel)
    style='dropdown' → TicketPanelSelectView (select menu, one option per panel)
    """
    style = (row.get('style') or 'buttons').lower()
    if style == 'dropdown':
        return TicketPanelSelectView(panels, row.get('placeholder') or 'Select a ticket type…')
    return MultiPanelView(panels, per_row=int(row.get('per_row') or 5))


class MultiPanelView(View):
    """TicketTool-style multi-panel (Attached Panels): up to 25 panels
    combined into ONE message, each contributing its own create button.

    Buttons reuse the standard `create_ticket:{panel_id}` custom_ids, so
    persistence routing and the shared gate flow are identical to
    single-panel messages.
    """

    def __init__(self, panels: List[Dict], per_row: int = 5):
        super().__init__(timeout=None)
        self.panels = panels
        per_row = max(1, min(5, int(per_row or 5)))
        for i, panel in enumerate(panels[:25]):
            row = min(i // per_row, 4)
            btn = Button(
                style=discord.ButtonStyle(panel.get('button_style', 3)),
                label=(panel.get('button_label') or panel.get('name') or 'Create Ticket')[:80],
                emoji=panel.get('button_emoji') or None,
                custom_id=f"{PANEL_BUTTON_PREFIX}{panel['panel_id']}",
                row=row,
            )
            btn.callback = self._on_click
            self.add_item(btn)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        # For assigned callbacks discord.py does not pass the clicked item,
        # so read the custom_id from the interaction payload (attribute on
        # modern ComponentInteractionData, dict-style in older builds).
        data = getattr(interaction, 'data', None)
        custom_id = getattr(data, 'custom_id', None)
        if custom_id is None and isinstance(data, dict):
            custom_id = data.get('custom_id')
        if custom_id and custom_id.startswith(PANEL_BUTTON_PREFIX):
            panel_id = custom_id[len(PANEL_BUTTON_PREFIX):]
            panel = None
            if ticket_tool:
                panel = ticket_tool.data_manager.load_ticket_panel(panel_id)
            if panel:
                await process_panel_create_request(interaction, panel)
                return
            await interaction.response.send_message("This ticket panel no longer exists.", ephemeral=True)
            return
        await interaction.response.defer()


class TicketPanelSelectView(View):
    """TicketTool-style dropdown panel: a Discord select menu where each
    option routes to a different panel (per-option: label, description,
    emoji). Placeholder text is configurable.

    PERSISTENCE: the select uses the stable custom_id `ticket_panel_select`,
    so interactions after a restart are dispatched to the generic instance
    registered in setup_hook; the selected panel is re-resolved from the DB
    at click time, keeping option labels/descriptions fresh via /panelupdate.
    """

    def __init__(self, panels: List[Dict], placeholder: str = "Select a ticket type…"):
        super().__init__(timeout=None)
        self.panels = panels
        options = []
        for panel in panels[:25]:
            label = (panel.get('name') or panel.get('button_label') or 'Panel')[:100]
            description = (panel.get('embed_description') or '')[:100] or None
            options.append(discord.SelectOption(
                label=label,
                value=str(panel['panel_id']),
                description=description,
                emoji=panel.get('button_emoji') or None,
            ))
        if not options:
            # discord.py requires at least one option at construction; the
            # generic persistent instance uses a placeholder that is never
            # selectable in practice (real views always have panels).
            options.append(discord.SelectOption(label='Tickets', value='__none__'))
        self.select = Select(
            placeholder=(placeholder or 'Select a ticket type…')[:150],
            options=options,
            custom_id=PANEL_SELECT_CUSTOM_ID,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        selected = self.select.values[0] if self.select.values else None
        if not selected:
            await interaction.response.defer()
            return
        panel = None
        if ticket_tool:
            panel = ticket_tool.data_manager.load_ticket_panel(selected)
        if not panel:
            await interaction.response.send_message(
                "This ticket panel no longer exists. Use /panelupdate to refresh the menu.",
                ephemeral=True,
            )
            return
        await process_panel_create_request(interaction, panel)


class TicketPanelView(View):
    """The panel that users interact with to create tickets.

    PERSISTENCE FIX: each panel's button uses a UNIQUE custom_id of the form
    `create_ticket:{panel_id}`. Previously every panel used the same
    `create_ticket_button` custom_id, so discord.py could only keep ONE
    registered panel config after restart — all panel messages would then
    route to that single panel's config. With per-panel custom_ids, each
    panel message correctly resolves to its own panel even after restart.
    """

    def __init__(self, panel: Dict):
        super().__init__(timeout=None)
        self.panel = panel
        panel_id = panel.get('panel_id', 'unknown')

        # Configure button based on panel settings, AND give it a unique
        # custom_id so persistence routing is unambiguous.
        self.create_button.style = discord.ButtonStyle(panel.get('button_style', 3))
        self.create_button.label = panel.get('button_label', 'Create Ticket')
        if panel.get('button_emoji'):
            self.create_button.emoji = panel['button_emoji']
        self.create_button.custom_id = f"{PANEL_BUTTON_PREFIX}{panel_id}"

    @staticmethod
    def _panel_id_from_custom_id(custom_id: str) -> Optional[str]:
        if custom_id and custom_id.startswith(PANEL_BUTTON_PREFIX):
            return custom_id[len(PANEL_BUTTON_PREFIX):]
        return None

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.success, custom_id="create_ticket:placeholder")
    async def create_button(self, interaction: discord.Interaction, button: Button) -> None:
        panel_id = self._panel_id_from_custom_id(button.custom_id)
        panel = None
        if panel_id and ticket_tool:
            panel = ticket_tool.data_manager.load_ticket_panel(panel_id)
        if not panel:
            panel = self.panel
        await process_panel_create_request(interaction, panel)

    async def _send_welcome_message(self, channel: discord.TextChannel, user: discord.Member, panel: Dict) -> None:
        """Send the welcome message in the ticket channel."""
        ticket = await ticket_tool.data_manager.async_load_ticket_by_channel(channel.id)
        if not ticket:
            return

        view = TicketControlView(ticket['ticket_id'])

        # Show the available ticket commands FIRST, before the welcome message.
        try:
            await channel.send(embed=build_ticket_commands_embed(panel))
        except discord.DiscordException as e:
            logging.warning(f"[Tickets] Could not send ticket-commands embed: {e}")

        welcome_text = panel.get('welcome_message', "Support will be with you shortly.")

        # Type = the panel name (ticket type); Category = the internal
        # Ticket Category folder inherited from the panel (Uncategorized when
        # the panel has none / it was created before the feature).
        category_line = _resolve_ticket_category_for_display(channel.guild.id, ticket)
        embed = discord.Embed(
            title=f"Ticket #{ticket['ticket_id']}",
            description=(
                f"Welcome {user.mention}!\n\n{welcome_text}\n\n"
                f"**Type:** {panel.get('name', 'General')}\n"
                f"**Category:** {category_line}"
            ),
            color=discord.Color(panel.get('embed_color', 0x5865F2)),
            timestamp=datetime.now(timezone.utc)
        )

        if panel.get('embed_thumbnail'):
            embed.set_thumbnail(url=panel['embed_thumbnail'])
        if panel.get('embed_image'):
            embed.set_image(url=panel['embed_image'])

        embed.set_footer(text=f"Created by {user.display_name}")

        mention_text = ""
        guild_settings = None
        try:
            guild_settings = data_manager.load_ticket_settings(channel.guild.id)
        except Exception:
            guild_settings = None
        if ows_get("mention_support_on_create") and (guild_settings.get('mention_on_create', 1) if guild_settings else True):
            support_role_id = panel.get('support_role_id')
            if support_role_id:
                role = channel.guild.get_role(support_role_id)
                if role:
                    mention_text = f"{role.mention} "

        welcome_msg = await channel.send(f"{mention_text}{user.mention}", embed=embed, view=view)

        # TicketTool "Auto Pin Ticket": pin the ticket message so the control
        # buttons stay reachable. Gated by the OWS pin_ticket_message toggle.
        if ows_get("pin_ticket_message"):
            try:
                await welcome_msg.pin(reason="Ticket message pinned (control buttons)")
            except (discord.Forbidden, discord.HTTPException):
                pass


class TicketQuestionsModal(Modal, title="Create Ticket"):
    """Modal for ticket questions."""
    
    def __init__(self, panel: Dict, questions: List[Dict]):
        super().__init__()
        self.panel = panel
        self.questions = questions
        self.answers = {}
        
        for i, q in enumerate(questions[:5]):  # Discord limits to 5 items
            text_input = TextInput(
                label=q['question_text'][:45],
                placeholder=q.get('placeholder', ''),
                style=discord.TextStyle.paragraph if q.get('question_type') == 'paragraph' else discord.TextStyle.short,
                required=q.get('required', True),
                custom_id=f"question_{q['question_id']}"
            )
            self.add_item(text_input)
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Collect answers
        answers = {}
        for child in self.children:
            qid = child.custom_id.replace('question_', '')
            answers[qid] = child.value
        
        # Create ticket
        channel, ticket_id = await ticket_tool.create_ticket(
            interaction.guild, interaction.user, self.panel, answers=answers
        )
        
        if channel:
            # Send welcome message with answers
            await self._send_welcome_with_answers(channel, interaction.user, answers)
            await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)
        else:
            await interaction.followup.send(f"Failed to create ticket: {ticket_id}", ephemeral=True)
    
    async def _send_welcome_with_answers(self, channel: discord.TextChannel, user: discord.Member, answers: Dict) -> None:
        """Send welcome message with question answers."""
        ticket = ticket_tool.data_manager.load_ticket_by_channel(channel.id)
        if not ticket:
            return

        view = TicketControlView(ticket['ticket_id'])

        # Show the available ticket commands FIRST, before the welcome message.
        try:
            await channel.send(embed=build_ticket_commands_embed(self.panel))
        except discord.DiscordException as e:
            logging.warning(f"[Tickets] Could not send ticket-commands embed: {e}")

        welcome_text = self.panel.get('welcome_message', "Support will be with you shortly.")
        
        embed = discord.Embed(
            title=f"Ticket #{ticket['ticket_id']}",
            color=discord.Color(self.panel.get('embed_color', 0x5865F2)),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(name="Creator", value=user.mention, inline=True)
        embed.add_field(name="Type", value=self.panel.get('name', 'General'), inline=True)
        embed.add_field(
            name="Category",
            value=_resolve_ticket_category_for_display(channel.guild.id, ticket),
            inline=True,
        )
        
        # Add answers - keys are bare question IDs (prefix stripped in on_submit)
        for q in self.questions:
            answer = answers.get(q['question_id'], 'No answer')
            embed.add_field(name=q['question_text'][:256], value=answer[:1024], inline=False)
        
        embed.add_field(name="Info", value=welcome_text, inline=False)
        embed.set_footer(text=f"Created by {user.display_name}")
        
        # Mention gate now matches _send_welcome_message: the support-role ping
        # respects BOTH the OWS toggle and the ticket_settings column
        # (previously this variant pinged unconditionally).
        mention_text = ""
        if ows_get("mention_support_on_create"):
            settings = data_manager.load_ticket_settings(channel.guild.id)
            if settings is None or settings.get('mention_on_create', 1):
                support_role_id = self.panel.get('support_role_id')
                if support_role_id:
                    role = channel.guild.get_role(support_role_id)
                    if role:
                        mention_text = f"{role.mention} "

        welcome_msg = await channel.send(f"{mention_text}{user.mention}", embed=embed, view=view)

        # TicketTool "Auto Pin Ticket" (gated by the OWS toggle).
        if ows_get("pin_ticket_message"):
            try:
                await welcome_msg.pin(reason="Ticket message pinned (control buttons)")
            except (discord.Forbidden, discord.HTTPException):
                pass


# --- TICKET CONTROL VIEW (Inside ticket channels) ---
class TicketControlView(View):
    """Buttons inside a ticket channel for control.

    PERSISTENCE FIX: on startup a single `TicketControlView("")` is
    registered (see on_ready). After restart, `self.ticket_id` is the empty
    string, so any handler that used `self.ticket_id` directly (close,
    transcript, note, priority) would fail. Every handler now resolves the
    ticket from `interaction.channel.id` via the DB, which is always correct
    regardless of what the view instance was constructed with.
    """

    def __init__(self, ticket_id: str = ""):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id

    async def _resolve_ticket(self, interaction: discord.Interaction) -> Optional[Dict]:
        """Look up the ticket for the current channel (robust after restart)."""
        if not ticket_tool:
            return None
        ticket_id = self.ticket_id
        ticket = None
        if ticket_id:
            ticket = await ticket_tool.data_manager.async_load_ticket(ticket_id)
        if not ticket and interaction.channel:
            # Fallback: resolve by channel — always correct inside a ticket channel.
            ticket = await ticket_tool.data_manager.async_load_ticket_by_channel(interaction.channel.id)
        return ticket

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="🙋", custom_id="ticket_claim")
    async def claim_button(self, interaction: discord.Interaction, button: Button) -> None:
        if not ticket_tool:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("System Not Ready", "The ticket system is still starting up. Try again in a moment."),
                ephemeral=True,
            )
            return

        # Defer first so a slow DB write never surfaces as "interaction failed".
        await interaction.response.defer(thinking=True, ephemeral=True)

        success, message = await ticket_tool.claim_ticket(interaction.channel, interaction.user)

        if success:
            button.disabled = True
            # Show WHO claimed right on the button so it's obvious at a glance.
            claim_label = f"Claimed by {interaction.user.display_name}"
            if len(claim_label) > 80:
                claim_label = claim_label[:77] + "..."
            button.label = claim_label
            button.style = discord.ButtonStyle.secondary
            button.emoji = "✅"
            try:
                await interaction.message.edit(view=self)
            except (discord.HTTPException, AttributeError):
                pass
            await interaction.channel.send(
                embed=discord.Embed(
                    description=f"🙋 {interaction.user.mention} claimed this ticket.",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc),
                )
            )
            await interaction.followup.send(
                embed=EmbedBuilder.success("Ticket Claimed", f"You now own this ticket. Use `!unclaim` to release it."),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=EmbedBuilder.warning("Could Not Claim", message or "This ticket could not be claimed."),
                ephemeral=True,
            )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket_close")
    async def close_button(self, interaction: discord.Interaction, button: Button) -> None:
        # Resolve the real ticket_id from the channel so closing works even
        # when this view instance was the empty-string startup registration.
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Ticket Not Found", "This channel doesn't appear to be an active ticket."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(CloseTicketModal(ticket['ticket_id']))

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="ticket_transcript")
    async def transcript_button(self, interaction: discord.Interaction, button: Button) -> None:
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Ticket Not Found", "This channel doesn't appear to be an active ticket."),
                ephemeral=True,
            )
            return
        # Defer so the (potentially slow) transcript generation never surfaces
        # as an "interaction failed" error to the user.
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            transcript = await ticket_tool._generate_transcript(
                interaction.channel, ticket, interaction.user
            )
            await interaction.followup.send(
                embed=transcript['embed'],
                file=transcript['file'],
                ephemeral=True,
            )
        except Exception as exc:
            logging.exception("[Tickets] transcript button failed: %s", exc)
            await interaction.followup.send(
                embed=EmbedBuilder.error("Transcript Failed", f"Could not generate the transcript: `{exc}`"),
                ephemeral=True,
            )

    @discord.ui.button(label="Note", style=discord.ButtonStyle.secondary, emoji="📝", row=1, custom_id="ticket_note")
    async def note_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Add a staff note. See AddNoteModal — notes are now stored privately
        (ephemeral confirmation only, no public channel post)."""
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Ticket Not Found", "This channel doesn't appear to be an active ticket."),
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Staff Only", "Only staff with Manage Channels permission can add notes."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(AddNoteModal(ticket['ticket_id']))

    @discord.ui.button(label="Priority", style=discord.ButtonStyle.secondary, emoji="🚨", row=1, custom_id="ticket_priority")
    async def priority_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Set the priority of this ticket."""
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Staff Only", "Only staff with Manage Channels permission can set priority."),
                ephemeral=True,
            )
            return
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Ticket Not Found", "This channel doesn't appear to be an active ticket."),
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="🚨 Set Ticket Priority",
            description="Choose a priority level below. This updates the ticket status and notifies staff.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Ticket #{ticket['ticket_id']} • Requested by {interaction.user.display_name}")
        await interaction.response.send_message(
            embed=embed,
            view=PrioritySelectView(ticket['ticket_id']),
            ephemeral=True,
        )

    @discord.ui.button(label="Category", style=discord.ButtonStyle.secondary, emoji="📁", row=1, custom_id="ticket_category")
    async def category_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Set the Ticket Category (internal folder) of this ticket.

        Works on ANY ticket — including ones created before Ticket Categories
        existed (they start as Uncategorized). Changing the category only
        updates the ticket's DB association; the Discord channel, claim info,
        transcripts and all other metadata are untouched.
        """
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Staff Only", "Only staff with Manage Channels permission can set the ticket category."),
                ephemeral=True,
            )
            return
        ticket = await self._resolve_ticket(interaction)
        if not ticket:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Ticket Not Found", "This channel doesn't appear to be an active ticket."),
                ephemeral=True,
            )
            return
        categories = data_manager.load_ticket_categories(interaction.guild.id)
        if not categories:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning(
                    "No Categories Yet",
                    "No ticket categories exist yet — create one with `!tcategory` first.",
                ),
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="📁 Set Ticket Category",
            description="Select a category (or remove the current one) from the menu below.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Ticket #{ticket['ticket_id']} • Requested by {interaction.user.display_name}")
        await interaction.response.send_message(
            embed=embed,
            view=TicketCategorySelectView(ticket['ticket_id'], interaction.guild.id),
            ephemeral=True,
        )


class CloseTicketModal(Modal, title="🔒 Close Ticket"):
    reason_input = TextInput(
        label="Close Reason (optional)",
        placeholder="e.g. Issue resolved, user no longer needs help…",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )
    
    def __init__(self, ticket_id: str):
        super().__init__()
        self.ticket_id = ticket_id
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = self.reason_input.value.strip() or "No reason provided"
        
        ticket = ticket_tool.data_manager.load_ticket(self.ticket_id)
        is_creator = ticket and ticket.get('creator_id') == interaction.user.id
        
        if is_creator and ows_get("ticket_rating_prompt"):
            view = TicketRatingView(self.ticket_id, reason, interaction.channel, interaction.user)
            rating_embed = discord.Embed(
                title="⭐ Rate Your Support Experience",
                description=(
                    "Before we close your ticket, please rate the support you received.\n\n"
                    "Your feedback helps us improve and recognize great staff! 💛"
                ),
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc),
            )
            rating_embed.set_footer(text=f"Ticket #{self.ticket_id} • Closes automatically in 2 min")
            await interaction.response.send_message(
                embed=rating_embed,
                view=view,
                ephemeral=True
            )
        else:
            view = ConfirmCloseView(self.ticket_id, reason)
            confirm_embed = discord.Embed(
                title="🔒 Confirm Ticket Closure",
                description=(
                    f"You're about to close **Ticket #{self.ticket_id}**.\n\n"
                    f"**Reason:** {reason}"
                ),
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            confirm_embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            await interaction.response.send_message(
                embed=confirm_embed,
                view=view,
                ephemeral=True
            )


class ConfirmCloseView(View):
    """Confirmation view for staff closing tickets."""
    
    def __init__(self, ticket_id: str, reason: str):
        super().__init__(timeout=120)
        self.ticket_id = ticket_id
        self.reason = reason
    
    async def on_timeout(self) -> None:
        """Disable all buttons when the confirmation times out (no silent close)."""
        for child in self.children:
            child.disabled = True
    
    @discord.ui.button(label="✅ Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm_close(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Disable buttons immediately to prevent double-clicks.
        for child in self.children:
            child.disabled = True
        button.label = "Closing…"
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass
        
        # Now close the ticket
        success = await ticket_tool.close_ticket(interaction.channel, interaction.user, self.reason)
        if not success:
            await interaction.followup.send(
                embed=EmbedBuilder.error("Close Failed", "The ticket could not be closed. Check my permissions and try again."),
                ephemeral=True,
            )
    
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_close(self, interaction: discord.Interaction, button: Button) -> None:
        for child in self.children:
            child.disabled = True
        cancel_embed = EmbedBuilder.info("Close Cancelled", "The ticket was not closed. You can reopen this prompt anytime.")
        await interaction.response.edit_message(
            embed=cancel_embed,
            view=self
        )


class TicketRatingView(View):
    """Rating view BEFORE ticket close - gives user time to rate.

    Star buttons are color-graded so the rating scale reads at a glance:
    red (1-2 = poor), orange (3 = okay), green (4-5 = great). The 5-star
    option uses the success style to draw the eye to the "ideal" rating.
    """

    # Themed color per rating tier (used for the thank-you embed).
    _RATING_COLORS = {
        1: discord.Color.red(),
        2: discord.Color.red(),
        3: discord.Color.orange(),
        4: discord.Color.green(),
        5: discord.Color.green(),
    }
    _RATING_LABELS = {
        1: "Very Poor",
        2: "Poor",
        3: "Okay",
        4: "Good",
        5: "Excellent",
    }
    
    def __init__(self, ticket_id: str, reason: str, channel: discord.TextChannel, user: discord.Member):
        super().__init__(timeout=120)  # 2 minutes to rate
        self.ticket_id = ticket_id
        self.reason = reason
        self.channel = channel
        self.user = user
        self.rated = False
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the user who triggered the close may rate / skip.

        Previously the view was posted non-ephemerally with no user check, so
        ANY channel member could click the stars or force the close.
        """
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning(
                    "Not Your Prompt",
                    "Only the member closing this ticket can submit a rating.",
                ),
                ephemeral=True,
            )
            return False
        return True
    
    async def on_timeout(self) -> None:
        """Close ticket after timeout if not rated."""
        if not self.rated and self.channel:
            try:
                await ticket_tool.close_ticket(self.channel, self.user, self.reason)
            except Exception:
                pass  # Channel might already be deleted
    
    @discord.ui.button(label="⭐", style=discord.ButtonStyle.danger, row=0)
    async def rate_1(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit_rating(interaction, 1)
    
    @discord.ui.button(label="⭐⭐", style=discord.ButtonStyle.danger, row=0)
    async def rate_2(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit_rating(interaction, 2)
    
    @discord.ui.button(label="⭐⭐⭐", style=discord.ButtonStyle.secondary, row=0)
    async def rate_3(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit_rating(interaction, 3)
    
    @discord.ui.button(label="⭐⭐⭐⭐", style=discord.ButtonStyle.success, row=1)
    async def rate_4(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit_rating(interaction, 4)
    
    @discord.ui.button(label="⭐⭐⭐⭐⭐", style=discord.ButtonStyle.success, row=1)
    async def rate_5(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit_rating(interaction, 5)
    
    @discord.ui.button(label="⏭️ Skip & Close", style=discord.ButtonStyle.secondary, row=2)
    async def skip_rating(self, interaction: discord.Interaction, button: Button) -> None:
        self.rated = True
        await interaction.response.defer(thinking=True)
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(
                embed=EmbedBuilder.info("Closing Without Rating", "No rating recorded. Closing your ticket now…"),
                view=self
            )
        except discord.HTTPException:
            pass
        
        # Close the ticket
        success = await ticket_tool.close_ticket(self.channel, self.user, self.reason)
        if not success:
            await interaction.followup.send(
                embed=EmbedBuilder.error("Close Failed", "The ticket could not be closed. Check my permissions and try again."),
                ephemeral=True,
            )
    
    async def _submit_rating(self, interaction: discord.Interaction, rating: int) -> None:
        # Prevent double-submit (e.g. a second star click while closing).
        if self.rated:
            await interaction.response.defer(ephemeral=True)
            return
        self.rated = True
        
        # Save rating
        ticket = ticket_tool.data_manager.load_ticket(self.ticket_id)
        if ticket:
            ticket['rating'] = rating
            ticket_tool.data_manager.save_ticket(ticket)
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True
        
        color = self._RATING_COLORS.get(rating, discord.Color.gold())
        label = self._RATING_LABELS.get(rating, "Rated")
        stars = "⭐" * rating
        thanks = discord.Embed(
            title="💛 Thank You for Your Feedback!",
            description=(
                f"You rated your support: **{stars}**\n"
                f"_{label}_\n\n"
                f"Closing your ticket in 3 seconds…"
            ),
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        thanks.set_footer(text=f"Ticket #{self.ticket_id}")
        await interaction.response.edit_message(
            embed=thanks,
            view=self
        )
        
        # Wait a moment then close
        await asyncio.sleep(3)
        
        # Close the ticket
        try:
            await ticket_tool.close_ticket(self.channel, self.user, self.reason)
        except Exception:
            pass  # Channel might already be deleted


# --- ADD NOTE MODAL ---
class AddNoteModal(Modal, title="📝 Add Staff Note"):
    note_input = TextInput(
        label="Note (only staff can see this)",
        style=discord.TextStyle.paragraph,
        placeholder="Internal note visible only to staff...",
        max_length=1000,
        required=True
    )

    def __init__(self, ticket_id: str):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        note = {
            'note_id': str(_uuid.uuid4())[:8],
            'ticket_id': self.ticket_id,
            'guild_id': interaction.guild.id,
            'author_id': interaction.user.id,
            'content': self.note_input.value,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        await data_manager.async_save_ticket_note(note)

        confirm_embed = discord.Embed(
            title="📝 Staff Note Added",
            description="Your note has been saved privately and is visible to staff via `!notes`.\n\n"
                       f"```\n{self.note_input.value[:1800]}\n```",
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc)
        )
        confirm_embed.add_field(name="Ticket", value=f"#{self.ticket_id}", inline=True)
        confirm_embed.add_field(name="Note ID", value=note['note_id'], inline=True)
        confirm_embed.set_footer(text=f"Added by {interaction.user.display_name} • Note ID: {note['note_id']}")

        # PRIVACY FIX: the previous implementation ALSO posted the note content
        # to the ticket channel via `interaction.channel.send(embed=...)`.
        # Because the ticket creator can read that channel, the "private"
        # staff note was anything but private. We now ONLY send the ephemeral
        # confirmation to the staff member and persist the note to the DB,
        # where it can be reviewed with the `!notes` command (staff-only).
        # The transcript still records the note via the saved DB row when
        # generated for staff review.
        await interaction.response.send_message(embed=confirm_embed, ephemeral=True)

        # Optional: forward to a dedicated staff-notes channel if one is
        # configured in ticket settings (`notes_channel_id`). This keeps the
        # note out of the ticket channel the creator can read, while still
        # giving staff a shared place to see new notes.
        settings = data_manager.load_ticket_settings(interaction.guild.id)
        notes_channel_id = settings.get('notes_channel_id') if settings else None
        if notes_channel_id:
            notes_channel = interaction.guild.get_channel(notes_channel_id)
            if notes_channel:
                try:
                    staff_embed = discord.Embed(
                        title=f"📝 Staff Note • Ticket {self.ticket_id}",
                        description=self.note_input.value,
                        color=discord.Color.yellow(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    staff_embed.set_footer(text=f"By {interaction.user.display_name}")
                    await notes_channel.send(embed=staff_embed)
                except Exception as e:
                    logging.warning(f"[Tickets] Could not post note to staff notes channel: {e}")


# --- PRIORITY SELECT VIEW ---
PRIORITY_COLORS = {
    'low':    discord.Color.green(),
    'normal': discord.Color.blue(),
    'high':   discord.Color.orange(),
    'urgent': discord.Color.red(),
}
PRIORITY_EMOJIS = {'low': '🟢', 'normal': '🔵', 'high': '🟠', 'urgent': '🔴'}


class PrioritySelectView(View):
    def __init__(self, ticket_id: str):
        super().__init__(timeout=60)
        self.ticket_id = ticket_id

    @discord.ui.button(label="🟢 Low", style=discord.ButtonStyle.success)
    async def low(self, interaction: discord.Interaction, button: Button) -> None:
        await self._set_priority(interaction, 'low')

    @discord.ui.button(label="🔵 Normal", style=discord.ButtonStyle.primary)
    async def normal(self, interaction: discord.Interaction, button: Button) -> None:
        await self._set_priority(interaction, 'normal')

    @discord.ui.button(label="🟠 High", style=discord.ButtonStyle.secondary)
    async def high(self, interaction: discord.Interaction, button: Button) -> None:
        await self._set_priority(interaction, 'high')

    @discord.ui.button(label="🔴 Urgent", style=discord.ButtonStyle.danger)
    async def urgent(self, interaction: discord.Interaction, button: Button) -> None:
        await self._set_priority(interaction, 'urgent')

    async def _set_priority(self, interaction: discord.Interaction, priority: str) -> None:
        ticket = data_manager.load_ticket(self.ticket_id)
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        ticket['priority'] = priority
        data_manager.save_ticket(ticket)

        emoji = PRIORITY_EMOJIS.get(priority, '')
        color = PRIORITY_COLORS.get(priority, discord.Color.blue())
        try:
            await log_ticket_event(interaction.guild, 'priority', ticket, actor=interaction.user,
                                   detail=f"Priority set to **{priority.capitalize()}**")
        except Exception:
            pass
        embed = discord.Embed(
            title=f"{emoji} Priority Set: {priority.capitalize()}",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Set by {interaction.user.display_name}")
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        await interaction.channel.send(
            embed=discord.Embed(
                description=f"{emoji} Ticket priority set to **{priority.capitalize()}** by {interaction.user.mention}",
                color=color
            )
        )
        self.stop()


class TicketCategorySelectView(View):
    """Ephemeral select menu for changing a ticket's Ticket Category (folder).

    Shown from the ticket-channel 📁 Category button and from `!setcategory`.
    Includes a "None (Uncategorized)" option so the category can be removed
    or reverted. Works for tickets created before the feature — they simply
    have no category yet.
    """
    _NONE_VALUE = '__none__'

    def __init__(self, ticket_id: str, guild_id: int):
        super().__init__(timeout=120)
        self.ticket_id = ticket_id
        self.guild_id = guild_id
        self._build_select()

    def _build_select(self) -> None:
        self.clear_items()
        options = [discord.SelectOption(
            label='None (Uncategorized)',
            value=self._NONE_VALUE,
            description="Remove this ticket's category",
            emoji='📁',
        )]
        for cat in data_manager.load_ticket_categories(self.guild_id)[:24]:
            emoji = (cat.get('emoji') or '').strip()
            option = discord.SelectOption(
                label=str(cat.get('name', 'Category'))[:100],
                value=cat['category_id'],
                description=(str(cat.get('description'))[:100] or None) if cat.get('description') else None,
                emoji=emoji or None,
            )
            options.append(option)
        select = Select(
            placeholder='Choose a ticket category…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_category_selected
        self.add_item(select)

    async def on_category_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        raw = select.values[0]
        category_id = None if raw == self._NONE_VALUE else raw

        # Single-column atomic UPDATE — every other ticket field is preserved.
        updated = await data_manager.async_set_ticket_category(self.ticket_id, category_id)
        if not updated:
            await interaction.response.edit_message(
                content="Ticket not found — it may have been deleted.",
                view=None,
            )
            return

        label = _ticket_category_label(self.guild_id, category_id)
        ticket = await data_manager.async_load_ticket(self.ticket_id)
        if ticket is not None:
            try:
                await log_ticket_event(
                    interaction.guild, 'category', ticket,
                    actor=interaction.user,
                    detail=f"Ticket category set to **{label}**",
                )
            except Exception:
                pass

        embed = discord.Embed(
            title=f"📁 Ticket Category Set: {label}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Set by {interaction.user.display_name}")
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        # Public in-channel announcement (mirrors the priority flow).
        try:
            await interaction.channel.send(
                embed=discord.Embed(
                    description=(
                        f"📁 Ticket category set to **{label}** "
                        f"by {interaction.user.mention}"
                    ),
                    color=discord.Color.blurple(),
                )
            )
        except discord.DiscordException:
            pass
        self.stop()


# --- PANEL CREATOR VIEW (For creating/editing panels) ---
class PanelCreatorView(View):
    """Interactive panel creator."""
    
    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.user_id = user_id
        self.panel_data = {
            'panel_id': str(uuid.uuid4())[:8],
            'guild_id': guild_id,
            'name': 'New Panel',
            'embed_title': 'Support Tickets',
            'embed_description': 'Click the button below to create a ticket.',
            'embed_color': 0x5865F2,
            'button_label': 'Create Ticket',
            'button_style': 3,
            'ticket_limit': 3,
            'auto_close_hours': 24,
            'welcome_message': 'Support will be with you shortly.',
            'created_at': datetime.now(timezone.utc).isoformat()
        }
    
    def _create_preview_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.panel_data['embed_title'],
            description=self.panel_data['embed_description'],
            color=discord.Color(self.panel_data['embed_color'])
        )
        # Show which Ticket Category (internal folder) tickets from this
        # panel will be filed under.
        embed.add_field(
            name="Ticket Category",
            value=_ticket_category_label(self.guild_id, self.panel_data.get('ticket_category_id')),
            inline=False,
        )
        embed.set_footer(text=f"Panel: {self.panel_data['name']}")
        return embed
    
    @discord.ui.button(label="Set Name", style=discord.ButtonStyle.primary, emoji="🏷️", row=0)
    async def set_name(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        await interaction.response.send_modal(PanelNameModal(self))
    
    @discord.ui.button(label="Set Embed", style=discord.ButtonStyle.primary, emoji="🎨", row=0)
    async def set_embed(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        await interaction.response.send_modal(PanelEmbedModal(self))
    
    @discord.ui.button(label="Set Button", style=discord.ButtonStyle.secondary, emoji="🔘", row=0)
    async def set_button(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        await interaction.response.send_modal(PanelButtonModal(self))
    
    @discord.ui.button(label="Settings", style=discord.ButtonStyle.secondary, emoji="⚙️", row=1)
    async def settings(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        await interaction.response.send_modal(PanelSettingsModal(self))
    
    @discord.ui.button(label="Category & Role", style=discord.ButtonStyle.secondary, emoji="🎯", row=1)
    async def target(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        await interaction.response.send_modal(PanelTargetModal(self))
    
    @discord.ui.button(label="Preview", style=discord.ButtonStyle.success, emoji="👁️", row=1)
    async def preview(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        
        preview_view = View(timeout=30)
        preview_view.add_item(Button(
            label=self.panel_data['button_label'],
            style=discord.ButtonStyle(self.panel_data['button_style']),
            disabled=True
        ))
        
        await interaction.response.send_message(
            embed=self._create_preview_embed(),
            view=preview_view,
            ephemeral=True
        )
    
    @discord.ui.button(label="Ticket Category", style=discord.ButtonStyle.primary, emoji="📁", row=2)
    async def ticket_category(self, interaction: discord.Interaction, button: Button) -> None:
        """Pick the Ticket Category (internal folder) this panel's tickets
        will belong to. Uses an ephemeral select so no typing is needed."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        categories = data_manager.load_ticket_categories(self.guild_id)
        if not categories:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning(
                    "No Categories Yet",
                    "No ticket categories exist yet. Create one first with `!tcategory`, "
                    "then come back — tickets from this panel will currently be **Uncategorized**.",
                ),
                ephemeral=True,
            )
            return
        cat_embed = discord.Embed(
            title="📁 Choose Ticket Category",
            description="Select the Ticket Category for this panel. Tickets created from it will be grouped in that folder.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        cat_embed.set_footer(text=f"Panel Builder • {interaction.user.display_name}")
        await interaction.response.send_message(
            embed=cat_embed,
            view=PanelTicketCategorySelectView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="Create Panel", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def create_panel(self, interaction: discord.Interaction, button: Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=EmbedBuilder.warning("Not Your Builder", "Only the staff member who opened this panel builder can configure it."), ephemeral=True)
            return
        
        # Save panel
        self.panel_data['channel_id'] = interaction.channel.id
        data_manager.save_ticket_panel(self.panel_data)
        
        # Send the actual panel (multi-embed set when configured via
        # /panelembed, otherwise the classic single embed)
        view = TicketPanelView(self.panel_data)
        message = await interaction.channel.send(
            embeds=_build_panel_message_embeds(self.panel_data),
            view=view
        )
        
        # Update panel with message ID
        self.panel_data['message_id'] = message.id
        data_manager.save_ticket_panel(self.panel_data)
        
        # Register view for persistence
        interaction.client.add_view(view)

        category_note = ""
        if self.panel_data.get('ticket_category_id'):
            label = _ticket_category_label(self.guild_id, self.panel_data['ticket_category_id'])
            category_note = f"\n**Ticket Category:** {label}"
        await interaction.response.send_message(
            embed=EmbedBuilder.success(
                "Panel Created",
                f"Your ticket panel is live!\n**Panel ID:** `{self.panel_data['panel_id']}`{category_note}",
            ),
            ephemeral=True
        )
        self.stop()


class PanelNameModal(Modal, title="Panel Name"):
    name_input = TextInput(label="Panel Name", placeholder="e.g., Support Tickets", max_length=50)
    
    def __init__(self, view: PanelCreatorView):
        super().__init__()
        self.view = view
        self.name_input.default = view.panel_data.get('name', '')
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view.panel_data['name'] = self.name_input.value
        await interaction.response.send_message(f"Panel name set to: {self.name_input.value}", ephemeral=True)


class PanelEmbedModal(Modal, title="Embed Settings"):
    title_input = TextInput(label="Embed Title", max_length=100)
    desc_input = TextInput(label="Embed Description", style=discord.TextStyle.paragraph, max_length=1000, required=False)
    color_input = TextInput(label="Color (Hex)", max_length=7, placeholder="#5865F2", required=False)
    
    def __init__(self, view: PanelCreatorView):
        super().__init__()
        self.view = view
        self.title_input.default = view.panel_data.get('embed_title', '')
        self.desc_input.default = view.panel_data.get('embed_description', '')
        self.color_input.default = hex(view.panel_data.get('embed_color', 0x5865F2))[2:]
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view.panel_data['embed_title'] = self.title_input.value
        
        if self.desc_input.value:
            self.view.panel_data['embed_description'] = self.desc_input.value
        
        if self.color_input.value:
            try:
                color_hex = self.color_input.value.strip('#')
                self.view.panel_data['embed_color'] = int(color_hex, 16)
            except ValueError:
                pass
        
        await interaction.response.send_message("Embed settings updated!", ephemeral=True)


class PanelButtonModal(Modal, title="Button Settings"):
    label_input = TextInput(label="Button Label", max_length=80, placeholder="Create Ticket")
    emoji_input = TextInput(label="Button Emoji", max_length=50, required=False, placeholder="🎫")
    style_input = TextInput(label="Style (1-4)", max_length=1, placeholder="3")
    
    def __init__(self, view: PanelCreatorView):
        super().__init__()
        self.view = view
        self.label_input.default = view.panel_data.get('button_label', '')
        self.emoji_input.default = view.panel_data.get('button_emoji', '')
        self.style_input.default = str(view.panel_data.get('button_style', 3))
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view.panel_data['button_label'] = self.label_input.value
        
        if self.emoji_input.value:
            self.view.panel_data['button_emoji'] = self.emoji_input.value
        
        try:
            style = int(self.style_input.value)
            if 1 <= style <= 4:
                self.view.panel_data['button_style'] = style
        except ValueError:
            pass
        
        await interaction.response.send_message("Button settings updated!", ephemeral=True)


class PanelSettingsModal(Modal, title="Panel Settings"):
    limit_input = TextInput(label="Max Tickets Per User (panel)", max_length=2, placeholder="3")
    close_input = TextInput(label="Auto-Close Hours (0=off)", max_length=3, placeholder="24")
    twostep_input = TextInput(label="Two-Step Close (yes/no)", max_length=3, placeholder="no")
    welcome_input = TextInput(label="Welcome Message", style=discord.TextStyle.paragraph, max_length=500, required=False)
    
    def __init__(self, view: PanelCreatorView):
        super().__init__()
        self.view = view
        self.limit_input.default = str(view.panel_data.get('ticket_limit', 3))
        self.close_input.default = str(view.panel_data.get('auto_close_hours', 24))
        self.twostep_input.default = 'yes' if view.panel_data.get('two_step_ticket') else 'no'
        self.welcome_input.default = view.panel_data.get('welcome_message', '')
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self.view.panel_data['ticket_limit'] = int(self.limit_input.value)
        except ValueError:
            pass
        
        try:
            hours = int(self.close_input.value)
            self.view.panel_data['auto_close_hours'] = max(0, hours)
        except ValueError:
            pass

        # TicketTool "Two Step Ticket": closed tickets keep their channel with
        # a moderator message (Re-Open / Delete / Transcript) instead of being
        # deleted immediately.
        self.view.panel_data['two_step_ticket'] = 1 if (self.twostep_input.value or '').strip().lower().startswith('y') else 0
        
        if self.welcome_input.value:
            self.view.panel_data['welcome_message'] = self.welcome_input.value
        
        await interaction.response.send_message(
            "Settings updated! (auto-close runs when the `Auto-Close Idle Tickets` toggle is on)",
            ephemeral=True,
        )


class PanelTargetModal(Modal, title="Panel Category & Role"):
    """Set the panel's ticket category and support role (TicketTool per-panel
    overrides — previously configurable only by editing the database)."""
    category_input = TextInput(label="Category ID (blank = guild default)", max_length=20, required=False)
    role_input = TextInput(label="Support Role ID (blank = guild default)", max_length=20, required=False)

    def __init__(self, view: PanelCreatorView):
        super().__init__()
        self.view = view
        if view.panel_data.get('category_id'):
            self.category_input.default = str(view.panel_data['category_id'])
        if view.panel_data.get('support_role_id'):
            self.role_input.default = str(view.panel_data['support_role_id'])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cat_raw = (self.category_input.value or '').strip()
        role_raw = (self.role_input.value or '').strip()
        if cat_raw:
            try:
                category_id = int(cat_raw)
                category = interaction.guild.get_channel(category_id)
                if category and isinstance(category, discord.CategoryChannel):
                    self.view.panel_data['category_id'] = category_id
                else:
                    await interaction.response.send_message("Invalid category ID — not changed.", ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message("Category ID must be a number — not changed.", ephemeral=True)
                return
        else:
            self.view.panel_data['category_id'] = None

        if role_raw:
            try:
                role_id = int(role_raw)
                if interaction.guild.get_role(role_id):
                    self.view.panel_data['support_role_id'] = role_id
                else:
                    await interaction.response.send_message("Invalid role ID — not changed.", ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message("Role ID must be a number — not changed.", ephemeral=True)
                return
        else:
            self.view.panel_data['support_role_id'] = None

        await interaction.response.send_message("Panel category & support role updated!", ephemeral=True)


class PanelTicketCategorySelectView(View):
    """Ephemeral select for assigning a Ticket Category (internal folder) to
    the panel being built in PanelCreatorView. Includes a "None" option to
    keep the panel's tickets Uncategorized."""
    _NONE_VALUE = '__none__'

    def __init__(self, creator: PanelCreatorView):
        super().__init__(timeout=120)
        self.creator = creator
        self._build_select()

    def _build_select(self) -> None:
        self.clear_items()
        options = [discord.SelectOption(
            label='None (Uncategorized)',
            value=self._NONE_VALUE,
            description="Tickets from this panel stay Uncategorized",
            emoji='📁',
        )]
        for cat in data_manager.load_ticket_categories(self.creator.guild_id)[:24]:
            emoji = (cat.get('emoji') or '').strip()
            options.append(discord.SelectOption(
                label=str(cat.get('name', 'Category'))[:100],
                value=cat['category_id'],
                description=(str(cat.get('description'))[:100] or None) if cat.get('description') else None,
                emoji=emoji or None,
            ))
        select = Select(
            placeholder='Choose the ticket category…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_category_selected
        self.add_item(select)

    async def on_category_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        raw = select.values[0]
        category_id = None if raw == self._NONE_VALUE else raw
        self.creator.panel_data['ticket_category_id'] = category_id
        label = _ticket_category_label(self.creator.guild_id, category_id)
        await interaction.response.edit_message(
            content=(
                f"Ticket Category set to **{label}** for this panel.\n"
                f"Every ticket created from this panel will be filed under that category. "
                f"You can change or remove it any time before creating the panel."
            ),
            view=None,
        )
        self.stop()


# =============================================================================
# TICKET CATEGORY MANAGER (!tcategory)
# =============================================================================
# Interactive embed-based management UI for Ticket Categories — the internal
# "folders" that group related tickets (e.g. a "Staff" category holding the
# "Apply for Staff" and "Staff Training" ticket types/panels).
#
# Flow:  !tcategory → embed with buttons
#          ├─ Create Category  → modal (name / description / emoji)
#          ├─ Edit Category    → select → prefilled modal
#          ├─ Delete Category  → select → confirmation (safe fallback)
#          └─ Assign to Panel  → select panel → select category
#
# Permission model: the command itself is gated by the same
# manage_channels permission used by every other ticket-management command.
# =============================================================================

def _build_ticket_categories_embed(guild_id: int, guild_name: str = '') -> discord.Embed:
    """Embed listing every Ticket Category in the guild (the !tcategory home)."""
    categories = data_manager.load_ticket_categories(guild_id) if data_manager else []
    embed = discord.Embed(
        title="📁 Ticket Categories",
        description=(
            "Ticket Categories are internal **folders** that group related "
            "tickets together — they are separate from Discord channel "
            "categories. Assign one to a panel in `!panel` (Ticket Category "
            "button) and every ticket from that panel is filed under it.\n\n"
            "Use the buttons below to manage categories."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    if categories:
        lines = []
        for cat in categories[:25]:
            emoji = (cat.get('emoji') or '').strip()
            name = str(cat.get('name', 'Category'))[:50]
            desc = (cat.get('description') or '').strip()
            ticket_count = data_manager.count_tickets_in_category(guild_id, cat['category_id'])
            panel_count = data_manager.count_panels_in_category(guild_id, cat['category_id'])
            line = f"{emoji + ' ' if emoji else ''}**{name}** — {ticket_count} ticket(s), {panel_count} panel(s)"
            if desc:
                line += f"\n> {desc[:150]}"
            lines.append(line)
        embed.add_field(
            name=f"Categories ({len(categories)})",
            value='\n'.join(lines)[:1024],
            inline=False,
        )
        if len(categories) > 25:
            embed.set_footer(text=f"…and {len(categories) - 25} more")
    else:
        embed.add_field(
            name="No categories yet",
            value=(
                "Create your first category with **Create Category** — for "
                "example `Staff`, then assign panels like *Apply for Staff* "
                "and *Staff Training* to it."
            ),
            inline=False,
        )
    if guild_name:
        embed.set_author(name=guild_name)
    return embed


class TicketCategoryManagerView(View):
    """Main !tcategory management view (Create / Edit / Delete / Assign)."""
    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.user_id = user_id
        self.message: Optional[discord.Message] = None

    async def _refresh(self) -> None:
        """Re-render the category list after a change."""
        if self.message is None:
            return
        try:
            await self.message.edit(embed=_build_ticket_categories_embed(self.guild_id), view=self)
        except discord.DiscordException:
            pass

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Create Category", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def create_category(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not your category manager.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketCategoryCreateModal(self))

    @discord.ui.button(label="Edit Category", style=discord.ButtonStyle.primary, emoji="✏️", row=0)
    async def edit_category(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not your category manager.", ephemeral=True)
            return
        categories = data_manager.load_ticket_categories(self.guild_id)
        if not categories:
            await interaction.response.send_message("No categories to edit yet.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select a category to edit:",
            view=TicketCategoryEditSelectView(self, categories),
            ephemeral=True,
        )

    @discord.ui.button(label="Delete Category", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def delete_category(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not your category manager.", ephemeral=True)
            return
        categories = data_manager.load_ticket_categories(self.guild_id)
        if not categories:
            await interaction.response.send_message("No categories to delete.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select a category to delete:",
            view=TicketCategoryDeleteSelectView(self, categories),
            ephemeral=True,
        )

    @discord.ui.button(label="Assign to Panel", style=discord.ButtonStyle.secondary, emoji="🎫", row=1)
    async def assign_to_panel(self, interaction: discord.Interaction, button: Button) -> None:
        """Change the Ticket Category of an EXISTING panel (panels don't need
        to be recreated — use /panelupdate afterwards to refresh the panel
        message if desired)."""
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not your category manager.", ephemeral=True)
            return
        panels = data_manager.load_ticket_panels_by_guild(self.guild_id)
        if not panels:
            await interaction.response.send_message("No active ticket panels in this server.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select the panel whose Ticket Category you want to change:",
            view=PanelCategoryAssignSelectView(self, panels),
            ephemeral=True,
        )

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary, row=1)
    async def done(self, interaction: discord.Interaction, button: Button) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not your category manager.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=_build_ticket_categories_embed(self.guild_id),
            view=self,
        )
        self.stop()


class TicketCategoryCreateModal(Modal, title="Create Ticket Category"):
    name_input = TextInput(
        label="Category Name",
        placeholder="e.g., Staff",
        max_length=50,
        required=True,
    )
    description_input = TextInput(
        label="Description (optional)",
        style=discord.TextStyle.paragraph,
        max_length=200,
        required=False,
    )
    emoji_input = TextInput(
        label="Emoji / Icon (optional)",
        placeholder="🎫 or <:name:123456789012345678>",
        max_length=64,
        required=False,
    )

    def __init__(self, manager: TicketCategoryManagerView):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = (self.name_input.value or '').strip()
        if not name:
            await interaction.response.send_message("Category name cannot be empty.", ephemeral=True)
            return
        if len(name) > 50:
            await interaction.response.send_message("Category name must be 50 characters or fewer.", ephemeral=True)
            return
        # Duplicate check (case-insensitive).
        if data_manager.load_ticket_category_by_name(self.manager.guild_id, name):
            await interaction.response.send_message(
                f"A ticket category named **{name}** already exists. Pick a different name.",
                ephemeral=True,
            )
            return
        emoji_raw = (self.emoji_input.value or '').strip()
        emoji_ok, emoji_clean = _validate_category_emoji(emoji_raw)
        if not emoji_ok:
            await interaction.response.send_message(
                "Invalid emoji — use a single emoji (e.g. 🎫) or a full custom "
                "emoji like `<:name:123456789012345678>`.",
                ephemeral=True,
            )
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        data_manager.save_ticket_category({
            'category_id': str(uuid.uuid4())[:8],
            'guild_id': self.manager.guild_id,
            'name': name,
            'description': (self.description_input.value or '').strip() or None,
            'emoji': emoji_clean or None,
            'created_by': interaction.user.id,
            'created_at': now_iso,
            'updated_at': now_iso,
        })
        await interaction.response.send_message(
            f"📁 Ticket category **{name}** created! Assign it to a panel with "
            f"`!panel` (Ticket Category button) or **Assign to Panel** in `!tcategory`.",
            ephemeral=True,
        )
        await self.manager._refresh()


class TicketCategoryEditSelectView(View):
    """Ephemeral select listing categories for editing."""
    def __init__(self, manager: TicketCategoryManagerView, categories: List[Dict]):
        super().__init__(timeout=120)
        self.manager = manager
        options = []
        for cat in categories[:25]:
            emoji = (cat.get('emoji') or '').strip()
            options.append(discord.SelectOption(
                label=str(cat.get('name', 'Category'))[:100],
                value=cat['category_id'],
                description=(str(cat.get('description'))[:100] or None) if cat.get('description') else None,
                emoji=emoji or None,
            ))
        select = Select(
            placeholder='Choose a category to edit…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_category_selected
        self.add_item(select)

    async def on_category_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        category = data_manager.load_ticket_category(select.values[0])
        if not category:
            await interaction.response.edit_message(content="That category no longer exists.", view=None)
            return
        await interaction.response.send_modal(TicketCategoryEditModal(self.manager, category))
        self.stop()


class TicketCategoryEditModal(Modal, title="Edit Ticket Category"):
    name_input = TextInput(label="Category Name", max_length=50, required=True)
    description_input = TextInput(
        label="Description (optional)",
        style=discord.TextStyle.paragraph,
        max_length=200,
        required=False,
    )
    emoji_input = TextInput(
        label="Emoji / Icon (optional)",
        placeholder="🎫 or <:name:123456789012345678>",
        max_length=64,
        required=False,
    )

    def __init__(self, manager: TicketCategoryManagerView, category: Dict):
        super().__init__()
        self.manager = manager
        self.category = category
        self.name_input.default = str(category.get('name') or '')
        self.description_input.default = str(category.get('description') or '')
        self.emoji_input.default = str(category.get('emoji') or '')

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = (self.name_input.value or '').strip()
        if not name:
            await interaction.response.send_message("Category name cannot be empty.", ephemeral=True)
            return
        # Duplicate check must ignore the category being edited itself.
        existing = data_manager.load_ticket_category_by_name(self.manager.guild_id, name)
        if existing and existing['category_id'] != self.category['category_id']:
            await interaction.response.send_message(
                f"Another category named **{name}** already exists. Pick a different name.",
                ephemeral=True,
            )
            return
        emoji_raw = (self.emoji_input.value or '').strip()
        emoji_ok, emoji_clean = _validate_category_emoji(emoji_raw)
        if not emoji_ok:
            await interaction.response.send_message(
                "Invalid emoji — use a single emoji (e.g. 🎫) or a full custom "
                "emoji like `<:name:123456789012345678>`.",
                ephemeral=True,
            )
            return
        # Preserve identity + audit fields; renaming does NOT detach tickets
        # or panels (they reference the immutable category_id).
        self.category['name'] = name
        self.category['description'] = (self.description_input.value or '').strip() or None
        self.category['emoji'] = emoji_clean or None
        self.category['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_ticket_category(self.category)
        await interaction.response.send_message(
            f"📁 Ticket category **{name}** updated. Existing tickets and panels "
            f"keep their assignment (nothing was detached).",
            ephemeral=True,
        )
        await self.manager._refresh()


class TicketCategoryDeleteSelectView(View):
    """Ephemeral select listing categories for deletion."""
    def __init__(self, manager: TicketCategoryManagerView, categories: List[Dict]):
        super().__init__(timeout=120)
        self.manager = manager
        options = []
        for cat in categories[:25]:
            emoji = (cat.get('emoji') or '').strip()
            options.append(discord.SelectOption(
                label=str(cat.get('name', 'Category'))[:100],
                value=cat['category_id'],
                description=(str(cat.get('description'))[:100] or None) if cat.get('description') else None,
                emoji=emoji or None,
            ))
        select = Select(
            placeholder='Choose a category to delete…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_category_selected
        self.add_item(select)

    async def on_category_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        category = data_manager.load_ticket_category(select.values[0])
        if not category:
            await interaction.response.edit_message(content="That category no longer exists.", view=None)
            return
        guild_id = self.manager.guild_id
        ticket_count = data_manager.count_tickets_in_category(guild_id, category['category_id'])
        panel_count = data_manager.count_panels_in_category(guild_id, category['category_id'])
        emoji = (category.get('emoji') or '').strip()
        display = f"{emoji + ' ' if emoji else ''}{category['name']}"
        warn_lines = [
            f"You are about to delete the ticket category **{display}**.",
            "",
            f"• **{ticket_count}** ticket(s) currently use this category",
            f"• **{panel_count}** panel(s) currently assign this category",
            "",
            "Tickets are **not** deleted and their Discord channels are left "
            "untouched — they simply fall back to **Uncategorized**. Panels "
            "also fall back to no category.",
        ]
        embed = discord.Embed(
            title="🗑️ Delete Ticket Category?",
            description='\n'.join(warn_lines),
            color=discord.Color.orange(),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=TicketCategoryConfirmDeleteView(self.manager, category['category_id'], display),
        )
        self.stop()


class TicketCategoryConfirmDeleteView(View):
    """Final confirmation for deleting a ticket category."""
    def __init__(self, manager: TicketCategoryManagerView, category_id: str, display: str):
        super().__init__(timeout=120)
        self.manager = manager
        self.category_id = category_id
        self.display = display

    @discord.ui.button(label="Delete Category", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(self, interaction: discord.Interaction, button: Button) -> None:
        # delete_ticket_category NULLs ticket/panel references first, so
        # nothing points at a deleted row and no ticket data is lost.
        deleted = await asyncio.to_thread(
            data_manager.delete_ticket_category, self.category_id,
        )
        if deleted:
            await interaction.response.edit_message(
                content=(
                    f"🗑️ Ticket category **{self.display}** deleted.\n"
                    f"Its tickets now show as **Uncategorized** — ticket data, "
                    f"channels and transcripts were left fully intact."
                ),
                embed=None,
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content="That category no longer exists — nothing to delete.",
                embed=None,
                view=None,
            )
        await self.manager._refresh()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.edit_message(
            content="Deletion cancelled — the category is unchanged.",
            embed=None,
            view=None,
        )
        self.stop()


class PanelCategoryAssignSelectView(View):
    """Ephemeral select listing panels for category assignment."""
    def __init__(self, manager: TicketCategoryManagerView, panels: List[Dict]):
        super().__init__(timeout=120)
        self.manager = manager
        options = []
        for panel in panels[:25]:
            current_id = panel.get('ticket_category_id')
            current = (
                _ticket_category_label(panel.get('guild_id'), current_id)
                if current_id else UNCATEGORIZED_LABEL
            )
            options.append(discord.SelectOption(
                label=str(panel.get('name', 'Panel'))[:100],
                value=panel['panel_id'],
                description=f"Current category: {current}"[:100],
                emoji=str(panel.get('button_emoji') or '')[:1] or None,
            ))
        select = Select(
            placeholder='Choose a panel…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_panel_selected
        self.add_item(select)

    async def on_panel_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        panel = data_manager.load_ticket_panel(select.values[0])
        if not panel or panel.get('guild_id') != self.manager.guild_id:
            await interaction.response.edit_message(content="That panel no longer exists.", view=None)
            return
        categories = data_manager.load_ticket_categories(self.manager.guild_id)
        if not categories:
            await interaction.response.edit_message(
                content="No ticket categories exist yet — create one with **Create Category** first.",
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=(
                f"Panel: **{panel.get('name', 'Panel')}** — now choose its new "
                f"Ticket Category (tickets created from this panel are filed under it):"
            ),
            view=PanelCategoryTargetSelectView(self.manager, panel, categories),
        )
        self.stop()


class PanelCategoryTargetSelectView(View):
    """Ephemeral select choosing the category for a previously-picked panel."""
    _NONE_VALUE = '__none__'

    def __init__(self, manager: TicketCategoryManagerView, panel: Dict, categories: List[Dict]):
        super().__init__(timeout=120)
        self.manager = manager
        self.panel = panel
        options = [discord.SelectOption(
            label='None (Uncategorized)',
            value=self._NONE_VALUE,
            description="Tickets from this panel stay Uncategorized",
            emoji='📁',
        )]
        for cat in categories[:24]:
            emoji = (cat.get('emoji') or '').strip()
            options.append(discord.SelectOption(
                label=str(cat.get('name', 'Category'))[:100],
                value=cat['category_id'],
                description=(str(cat.get('description'))[:100] or None) if cat.get('description') else None,
                emoji=emoji or None,
            ))
        select = Select(
            placeholder='Choose the ticket category…',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_category_selected
        self.add_item(select)

    async def on_category_selected(self, interaction: discord.Interaction) -> None:
        select = next((c for c in self.children if isinstance(c, Select)), None)
        if select is None or not select.values:
            await interaction.response.defer()
            return
        raw = select.values[0]
        category_id = None if raw == self._NONE_VALUE else raw
        # Atomic single-column update — the panel row (embed settings, message
        # id, automations, …) is otherwise untouched.
        updated = await asyncio.to_thread(
            data_manager.set_panel_category, self.panel['panel_id'], category_id,
        )
        if not updated:
            await interaction.response.edit_message(
                content="That panel no longer exists — nothing was changed.",
                view=None,
            )
            return
        label = _ticket_category_label(self.manager.guild_id, category_id)
        await interaction.response.edit_message(
            content=(
                f"✅ Panel **{self.panel.get('name', 'Panel')}** now files its tickets "
                f"under **{label}**.\n"
                f"Existing tickets keep their current category — new tickets from "
                f"this panel will be filed under **{label}**. "
                f"Use `!panelupdate {self.panel['panel_id']}` if you want to refresh "
                f"the panel message."
            ),
            view=None,
        )
        self.stop()


import uuid





# --- BLACKLIST DATA PERSISTENCE ---
def load_blacklist_data() -> None:
    global blacklisted_keywords
    try:
        blacklisted_keywords = data_manager.load_blacklist()
        logging.info(f"[Blacklist] Loaded {len(blacklisted_keywords)} blacklisted keywords from SQLite")
    except Exception as e:
        logging.error(f"[Blacklist] Error loading data: {e}")
        blacklisted_keywords = set()


def load_warnings_data() -> None:
    """Hydrate the in-memory warnings cache from SQLite on startup.

    `warnings_data` is populated here so the `!warnings` list and the
    auto-ban threshold check see every active warning immediately after a
    restart. Previously the dict started empty on every boot, so the
    auto-ban threshold silently reset (a user with 3 prior warnings would
    need 3 MORE warnings before the ban fired).
    """
    global warnings_data
    try:
        loaded = data_manager.load_warnings()  # {user_id: [dict, ...]}
        warnings_data = {}
        for user_id, warn_list in loaded.items():
            for w in warn_list:
                gid = w.get('guild_id')
                if gid is None:
                    continue
                warnings_data.setdefault(gid, []).append(w)
        total = sum(len(v) for v in warnings_data.values())
        logging.info(f"[Warnings] Hydrated {total} active warning(s) from SQLite")
    except Exception as exc:
        logging.error(f"[Warnings] Could not load warnings: {exc}")
        warnings_data = {}


def save_blacklist_data() -> None:
    try:
        data_manager.save_blacklist(blacklisted_keywords)
    except Exception as e:
        logging.error(f"[Blacklist] Error saving data: {e}")


def save_all_data() -> None:
    """Save all persistent data to SQLite.

    Tickets persist in SQLite via data_manager (written on every ticket event),
    so there is no separate tickets save step here.
    """
    # If the database was never connected — e.g. the bot exited before
    # setup_hook ran (invalid token / login failure) — there is nothing to
    # persist and the in-memory caches were never hydrated. Skip cleanly
    # instead of crashing on a None-cursor access in the save helpers below.
    if not data_manager.is_connected():
        logging.info("[DataManager] Database not connected; skipping shutdown save.")
        return
    save_blacklist_data()
    logging.info("[DataManager] All persistent data saved to SQLite.")




def import_json_to_sqlite() -> None:
    """
    Import existing JSON files into SQLite database.
    This runs once on startup if JSON files are found.
    After import, JSON files are renamed to .bak to prevent re-import.
    """
    imported_something = False
    
    # === IMPORT INVITES ===
    if os.path.exists('invite_data.json'):
        try:
            with open('invite_data.json', 'r') as f:
                data = json.load(f)
            
            message_id = data.get('message_id')
            channel_id = data.get('channel_id')
            tracked_invites = data.get('tracked_invites', {})
            
            data_manager.save_invites(message_id, channel_id, tracked_invites)
            
            # Rename to .bak
            os.rename('invite_data.json', 'invite_data.json.bak')
            
            logging.info(f"[Import] Imported {len(tracked_invites)} invite(s) from invite_data.json")
            imported_something = True
        except Exception as e:
            logging.error(f"[Import] Error importing invites: {e}")
    
    # === IMPORT BLACKLIST ===
    if os.path.exists('blacklist_data.json'):
        try:
            with open('blacklist_data.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            keywords = set(data.get('blacklisted_keywords', []))
            data_manager.save_blacklist(keywords)
            
            # Rename to .bak
            os.rename('blacklist_data.json', 'blacklist_data.json.bak')
            
            logging.info(f"[Import] Imported {len(keywords)} blacklist keyword(s) from blacklist_data.json")
            imported_something = True
        except Exception as e:
            logging.error(f"[Import] Error importing blacklist: {e}")

    # === IMPORT BRANDING ===
    if os.path.exists(config.branding_file):
        try:
            with open(config.branding_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data_manager.set_config_value("branding", json.dumps(data))
            os.rename(config.branding_file, f"{config.branding_file}.bak")
            logging.info("[Import] Imported branding config to SQLite")
            imported_something = True
        except Exception as e:
            logging.error(f"[Import] Error importing branding: {e}")

    # === IMPORT CHANNEL CONFIG ===
    if os.path.exists(config.channel_config_file):
        try:
            with open(config.channel_config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data_manager.set_config_value("channels", json.dumps(data))
            os.rename(config.channel_config_file, f"{config.channel_config_file}.bak")
            logging.info("[Import] Imported channel config to SQLite")
            imported_something = True
        except Exception as e:
            logging.error(f"[Import] Error importing channel config: {e}")

    if imported_something:
        logging.info("[Import] JSON import complete! Old files renamed to .bak")
    else:
        logging.info("[Import] No JSON files found to import")


# --- BLACKLIST HELPERS ---
def check_text_for_keywords(text: str) -> Tuple[bool, Optional[str]]:
    if not text:
        return (False, None)
    text_lower = text.lower()
    for keyword in blacklisted_keywords:
        if keyword.lower() in text_lower:
            return (True, keyword)
    return (False, None)


async def check_user_profile_for_blacklist(member: discord.Member) -> Tuple[bool, Optional[str], Optional[str]]:
    if not blacklisted_keywords:
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
        log_channel = bot.get_channel(config.channels.log)
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
        
        log_channel = bot.get_channel(config.channels.log)
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


# --- BACKGROUND TASKS ---
@tasks.loop(minutes=config.timing.report_message_interval_minutes)
async def send_report_message() -> None:
    if not messages_enabled:
        return
    channel = bot.get_channel(config.channels.log)
    if channel is None:
        return

    rules_channel_id = getattr(config.channels, "rules", 0)
    reports_channel_id = getattr(config.channels, "reports", 0)
    rules_channel_mention = f"<#{rules_channel_id}>" if rules_channel_id else "#rules"
    reports_channel_mention = f"<#{reports_channel_id}>" if reports_channel_id else "#reports"

    template = random.choice(REPORT_TEMPLATES)
    try:
        text = brand_text(template).format(
            server=bot.user.name if bot.user else "the server",
            rules_channel=rules_channel_mention,
            reports_channel=reports_channel_mention,
        )
    except (KeyError, IndexError, ValueError) as exc:
        # A malformed template never crashes the loop — log once and move on.
        logging.warning(f"[Broadcast] template format failed, using raw text: {exc}")
        text = brand_text(template)

    try:
        await channel.send(text)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
        logging.warning(f"[Broadcast] could not send to log channel: {exc}")


@tasks.loop(hours=config.timing.auto_scan_interval_hours)
async def auto_blacklist_scan() -> None:
    if not blacklisted_keywords:
        return
    for guild in bot.guilds:
        log_channel = bot.get_channel(config.channels.auto_scan)
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
    await bot.wait_until_ready()


@tasks.loop(minutes=30)
async def check_sla_task() -> None:
    """Alert in ticket channel if SLA response time has been breached."""
    if not ticket_tool:
        return
    for guild in bot.guilds:
        # PERFORMANCE (Phase 2) — SLA engine dedup: the premium package runs
        # its own 5-minute SLA state machine (ok → warning → breached). When
        # a guild has ANY premium SLA target configured, that engine owns the
        # guild and this legacy 30-min loop must stand down, otherwise both
        # loops alert on the same breach.
        if PREMIUM_AVAILABLE:
            try:
                _pdb = getattr(bot, 'premium_db', None)
                if _pdb is not None:
                    _pcfg = TicketTool.sla.get_config(_pdb, guild.id)
                    if _pcfg.get('enabled') and (
                        _pcfg.get('first_response_hours')
                        or _pcfg.get('resolution_hours')
                        or _pcfg.get('urgent_first_response_hours')
                        or _pcfg.get('urgent_resolution_hours')
                    ):
                        continue  # premium SLA engine owns this guild
            except Exception:
                pass  # premium lookup failed — legacy loop keeps the guild
        settings = data_manager.load_ticket_settings(guild.id)
        sla_hours = settings.get('sla_hours', 0) if settings else 0
        if not sla_hours:
            continue
        open_tickets = data_manager.load_tickets_by_guild(guild.id, 'open')
        for ticket in open_tickets:
            if ticket.get('first_response_at'):
                continue  # Already had a staff response
            if ticket.get('sla_warned_at'):
                continue  # Already warned (without faking a response)
            try:
                created = datetime.fromisoformat(ticket['created_at'].replace('Z', '+00:00'))
                elapsed_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
                if elapsed_hours >= sla_hours:
                    channel = guild.get_channel(ticket['channel_id'])
                    if channel:
                        support_role_id = settings.get('support_role_id')
                        mention = f"<@&{support_role_id}>" if support_role_id else "@here"
                        try:
                            await channel.send(
                                f"⚠️ **SLA Breach** — {mention} This ticket has been open for "
                                f"`{elapsed_hours:.1f}h` with no staff response "
                                f"(SLA: {sla_hours}h). Please respond ASAP.",
                                allowed_mentions=discord.AllowedMentions(roles=True, everyone=True)
                            )
                            # Mark warned only — a bot alert is not a staff
                            # reply, so first_response_at stays clean.
                            data_manager.mark_ticket_sla_warned(ticket['ticket_id'])
                        except Exception:
                            pass
            except Exception as e:
                logging.warning(f"[SLA] Error checking ticket {ticket.get('ticket_id')}: {e}")


@check_sla_task.before_loop
async def before_check_sla() -> None:
    await bot.wait_until_ready()


@tasks.loop(minutes=15)
async def check_auto_close_task() -> None:
    """Auto-close tickets idle beyond the configured auto_close_hours.

    Enforces the auto-close setting that is stored per panel
    (ticket_panels.auto_close_hours) with a guild-wide fallback
    (ticket_settings.auto_close_hours). Activity = the newest backed-up
    message in ticket_messages, falling back to the ticket's created_at.

    PAUSED TICKETS (Ticket Tool /pause) are skipped entirely, and a ticket
    that was just resumed starts a fresh idle window from its resume time.

    Gated by the OWS toggle `auto_close_tickets` (Tickets category).
    """
    if not ticket_tool or not ows_get("auto_close_tickets"):
        return
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        try:
            settings = data_manager.load_ticket_settings(guild.id)
        except Exception:
            settings = None
        guild_hours = settings.get('auto_close_hours') if settings else None
        open_tickets = data_manager.load_tickets_by_guild(guild.id, 'open')
        for ticket in open_tickets:
            try:
                channel = guild.get_channel(ticket.get('channel_id') or 0)
                if channel is None:
                    continue
                # TicketTool /pause: paused tickets are excluded from ALL
                # automatic actions.
                if _ticket_automation_paused(ticket):
                    continue
                # Per-panel hours override the guild default; skip if neither.
                panel = None
                if ticket.get('panel_id'):
                    panel = data_manager.load_ticket_panel(ticket['panel_id'])
                hours = panel.get('auto_close_hours') if panel else None
                if not hours:
                    hours = guild_hours
                if not hours:
                    continue
                # Idle time = newest backed-up message, else ticket creation.
                # A resumed ticket's idle clock restarts at its resume time
                # (paused time does not count toward inactivity).
                idle_since_raw = data_manager.get_last_ticket_message_time(ticket['ticket_id'])
                if ticket.get('automation_resumed_at'):
                    idle_since_raw = max(
                        (idle_since_raw, ticket['automation_resumed_at']),
                        key=lambda ts: datetime.fromisoformat(str(ts).replace('Z', '+00:00')),
                    ) if idle_since_raw else ticket['automation_resumed_at']
                if not idle_since_raw:
                    idle_since_raw = ticket.get('created_at')
                if not idle_since_raw:
                    continue
                try:
                    idle_since = datetime.fromisoformat(str(idle_since_raw).replace('Z', '+00:00'))
                except ValueError:
                    continue
                idle_hours = (now - idle_since).total_seconds() / 3600
                if idle_hours < float(hours):
                    continue
                try:
                    await channel.send(embed=discord.Embed(
                        description=(
                            f"⏲️ This ticket has been inactive for `{idle_hours:.1f}h` "
                            f"(auto-close threshold: `{hours}h`) and is being closed "
                            f"automatically. A transcript has been saved."
                        ),
                        color=discord.Color.orange(),
                    ))
                except Exception:
                    pass
                await ticket_tool.close_ticket(
                    channel, guild.me,
                    f"Automatically closed after {idle_hours:.1f}h of inactivity (threshold: {hours}h)",
                )
                logging.info(
                    f"[AutoClose] Closed ticket {ticket.get('ticket_id')} in {guild.name} "
                    f"after {idle_hours:.1f}h of inactivity"
                )
            except Exception as e:
                logging.warning(f"[AutoClose] Error processing ticket {ticket.get('ticket_id')}: {e}")


@check_auto_close_task.before_loop
async def before_check_auto_close() -> None:
    await bot.wait_until_ready()


# --- UTILITY FUNCTIONS ---
def get_uptime() -> str:
    return str(timedelta(seconds=int(time.time() - start_time)))


def log_event(event_type: str, user: discord.User, details: Optional[str] = None) -> None:
    log_message = f"{event_type} - User: {user} (ID: {user.id})"
    if details:
        log_message += f" | Details: {details}"
    logging.info(log_message)


# --- BOT EVENTS ---
def signal_handler(sig, frame) -> None:
    """Handle SIGINT/SIGTERM: save everything and exit cleanly.

    Every step is wrapped so a failure in one (e.g. `data_manager.close()`
    while a task is mid-write) doesn't abort the remaining cleanup or
    leave the process in a half-dead state.
    """
    logging.info("Shutdown signal received. Saving data...")

    # Persist whatever we can. If the DB was never connected, save_all_data
    # skips cleanly (see its is_connected() guard).
    try:
        save_all_data()
    except Exception as exc:
        logging.error(f"[Shutdown] save_all_data failed: {exc}")

    # Give any in-flight async writes a moment to drain. We can't await
    # inside a signal handler, so this is a short synchronous sleep.
    try:
        time.sleep(0.5)
    except Exception:
        pass

    # Close the SQLite connection.
    try:
        data_manager.close()
    except Exception as exc:
        logging.error(f"[Shutdown] data_manager.close failed: {exc}")

    # Clear the busy-lock file so the next launch doesn't see a stale lock.
    try:
        process_manager.clear_lock_file()
    except Exception as exc:
        logging.error(f"[Shutdown] clear_lock_file failed: {exc}")

    logging.info("Data saved. Goodbye!")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ------------------------------------------------------------------
# FIRST-STARTUP OWNER TUTORIAL
# On the very first launch the bot DMs the application owner a complete
# setup tutorial. A flag file (data/.tutorial_sent) guards it so it only
# fires once. The !tutorial command re-sends it.
# ------------------------------------------------------------------
TUTORIAL_FLAG_FILE = os.path.join(config.data_dir, ".tutorial_sent")


def build_tutorial_embeds() -> List[discord.Embed]:
    """Build the multi-page setup tutorial sent to the owner."""
    gn = brand_text("[GANG NAME]")
    embeds: List[discord.Embed] = []

    # --- Page 1: Welcome & Prerequisites ---
    e1 = discord.Embed(
        title=f"🚀 Welcome to FactionBot — Setup Tutorial",
        description=(
            f"Hello! This is a **one-time** setup tutorial to get your bot fully running.\n\n"
            "FactionBot ships **six core systems**: Tickets, Verification, Polls, "
            "Invite Tracking, Leveling and AutoMod — plus this guide.\n\n"
            "💬 **Tip:** Run `!tutorial` at any time to see this guide again."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    e1.add_field(
        name="✅ Prerequisites (verify these first)",
        value=(
            "1. **Bot invited with the `bot` scope** (the bot is prefix-only — no `applications.commands` scope is needed)\n"
            "2. **Privileged Gateway Intents enabled** in the Discord Developer Portal:\n"
            "   • Server Members Intent  •  Message Content Intent  •  Presence Intent\n"
            "3. **Bot has Administrator** (or equivalent) permissions in your server."
        ),
        inline=False,
    )
    e1.add_field(
        name="📚 What this tutorial covers",
        value=(
            "`Step 1` Guided Setup (`!csetup`)  •  `Step 2` Channels & Roles  •  "
            "`Step 3` The Six Core Systems  •  `Step 4` Branding  •  `Step 5` Final Checks  •  "
            "`Page 7` New & Notable  •  `Page 8` Multi-Guild Licensing (FactionAccess)"
        ),
        inline=False,
    )
    e1.set_footer(text=f"Page 1/8 • First-time setup tutorial • FactionBot {EDITION} v{__version__}")
    embeds.append(e1)

    # --- Page 2: Guided Setup ---
    e2 = discord.Embed(
        title="🛠 Step 1 — Guided Setup with !csetup",
        description=(
            "**`!csetup`** is the front door to the whole bot. It shows a live status "
            "panel for every subsystem, with buttons that jump straight into each one's "
            "configuration.\n\n"
            "Run it first — it tells you exactly what is configured and what still needs setup."
        ),
        color=discord.Color.blurple(),
    )
    e2.add_field(
        name="Quick reference",
        value=(
            "• `!csetup` — status panel + per-system guidance (Manage Guild)\n"
            "• `!settings` — global channel/role/limit configuration summary\n"
            "• `!help` — dynamic, always-up-to-date command list\n"
            "• `!help tickets` / `!help automod` / … — per-category help"
        ),
        inline=False,
    )
    e2.set_footer(text="Page 2/8 • Guided Setup")
    embeds.append(e2)

    # --- Page 3: Channels & Roles ---
    e3 = discord.Embed(
        title="📋 Step 2 — Channels & Roles",
        description=(
            "Run **`!channelsetup`** and **`!rolesetup`** for interactive dropdown menus, or "
            "assign individually with **`!setchannel <type> #channel`**. Timing and limits "
            "have their own menus: **`!timingsetup`** / **`!limitssetup`**."
        ),
        color=discord.Color.blurple(),
    )
    e3.add_field(
        name="🟢 Member-facing channels",
        value=(
            "• **Welcome / New-Member Channel** — where new members are greeted\n"
            "• **Server Rules Display Channel** — linked from welcome embeds\n"
            "• **Verification Panel Channel** — default home for the verify button (the "
            "verification system stores its own per-server config via `!vsetup`)"
        ),
        inline=False,
    )
    e3.add_field(
        name="🟡 Staff / logging channels",
        value=(
            "• **User Reports Channel** — where `!report` submissions are sent\n"
            "• **Mod Actions & Broadcasts Log Channel** — logs auto-bans AND sends "
            "scheduled broadcasts (also the default AutoMod log channel)\n"
            "• **Auto-Blacklist Scan Log Channel** — logs periodic blacklist-scan results"
        ),
        inline=False,
    )
    e3.add_field(
        name="🔴 Tickets system",
        value=(
            "• **Tickets Category** — a Discord **CATEGORY** (not a text channel) where "
            "ticket channels are created\n"
            "• **Ticket Transcripts Archive Channel** — where closed-ticket transcripts are saved"
        ),
        inline=False,
    )
    e3.add_field(
        name="👥 Roles (`!rolesetup`)",
        value=(
            "• **Member / Verified roles** — handed out by verification\n"
            "• **Staff role** — ticket support access\n"
            "• **Invite Manager role** — who can manage the invite system"
        ),
        inline=False,
    )
    e3.set_footer(text="Page 3/8 • Channels & Roles")
    embeds.append(e3)

    # --- Page 4: The Six Core Systems ---
    e4 = discord.Embed(
        title="⚙️ Step 3 — The Six Core Systems",
        description="One command each to get started:",
        color=discord.Color.blurple(),
    )
    e4.add_field(
        name="🎫 Tickets",
        value=(
            "`!panel` — interactive panel builder (embed, button, category, support role).\n"
            "Also: `!multipanel`, `!dropdownpanel`, `!automate`, `!slaconfig`, `!transcriptconfig`."
        ),
        inline=False,
    )
    e4.add_field(
        name="✅ Verification",
        value=(
            "`!vsetup` — guided setup: unverified/verified roles, panel + button, "
            "account-age gate, timeout with optional kick.\n"
            "Members click **Verify** to get the verified role."
        ),
        inline=False,
    )
    e4.add_field(
        name="📊 Polls",
        value=(
            "`!poll create 60 Question | Option A | Option B` — native Discord polls "
            "with auto-ending, result charts and history.\n"
            "Quick yes/no: `!poll quick Question`."
        ),
        inline=False,
    )
    e4.add_field(
        name="📈 Invites",
        value=(
            "`!invites setup` — join attribution, `!invites` cards, leaderboard, "
            "fake-detection, bonus invites, join announcements."
        ),
        inline=False,
    )
    e4.add_field(
        name="⭐ Leveling",
        value=(
            "`!level config` — XP per message, cooldown, announcements.\n"
            "`!level reward add 10 @Role` — role rewards. `!rank`, `!leaderboard` for everyone."
        ),
        inline=False,
    )
    e4.add_field(
        name="🛡 AutoMod",
        value=(
            "`!automod add words delete badword1, badword*` — rules engine with 11 rule "
            "types and combinable actions.\n"
            "`!automod config log_channel #log` — where violations are logged."
        ),
        inline=False,
    )
    e4.set_footer(text="Page 4/8 • Core Systems")
    embeds.append(e4)

    # --- Page 5: Branding ---
    e5 = discord.Embed(
        title="🎨 Step 4 — Branding",
        description=(
            f"Everything the bot says is auto-rebranded to your configured **{gn}** name."
        ),
        color=discord.Color.blurple(),
    )
    e5.add_field(
        name="Set your community name",
        value='`!botbranding name "Brothers Till Death"`\nStops being `[GANG NAME]` once set.',
        inline=False,
    )
    e5.add_field(
        name="Set your abbreviation",
        value="`!abrev BTD`\nStops being `[GANG ABBR]` once set (auto-uppercased).",
        inline=False,
    )
    e5.add_field(
        name="Embed styling",
        value=(
            "`!botbranding footer <text>` / `color <hex>` / `thumbnail <url>` / "
            "`banner <url>` / `avatar <url>` / `view` / `clear`"
        ),
        inline=False,
    )
    e5.set_footer(text="Page 5/8 • Branding")
    embeds.append(e5)

    # --- Page 6: Final Steps ---
    e6 = discord.Embed(
        title="✅ Step 5 — You're All Set!",
        description="Final checks to confirm everything is running smoothly.",
        color=discord.Color.green(),
    )
    e6.add_field(
        name="Verify it worked",
        value="Type `!help` in your server — the bot should reply with the full command menu.",
        inline=False,
    )
    e6.add_field(
        name="See all commands",
        value="`!help`\nDynamic, always-current list of every command the bot offers.",
        inline=False,
    )
    e6.add_field(
        name="Need this tutorial again?",
        value="`!tutorial`\nRe-sends this guide to your DMs anytime.",
        inline=False,
    )
    e6.set_footer(text="Page 6/8 • You're all set! 🎉")
    embeds.append(e6)

    # --- Page 7: New & Notable ---
    e7 = discord.Embed(
        title="🆕 New & Notable — The FactionBot Conversion",
        description=(
            f"What this rebuild changed under the hood (FactionBot "
            f"{EDITION} v{__version__}):"
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    e7.add_field(
        name="🛡️ Multi-guild licensing — FactionAccess (v5.2.0)",
        value=(
            "• **`!license`** — approve / revoke / suspend allied factions, grant feature "
            "bundles (verification, tickets, moderation, engagement, leveling, …) per "
            "guild, set per-guild gang identity + bot nickname, expiry, audit trail.\n"
            "• **`!request`** — allied faction leaders ping you for access or more bundles.\n"
            "• **`!license invite <guild>`** — a pre-scoped OAuth invite link.\n"
            "• Everything else stays home-guild-only by default (default-deny)."
        ),
        inline=False,
    )
    e7.add_field(
        name="🏗️ SaaS-quality repository pass (v5.1.0)",
        value=(
            "• **Repo hygiene** — `.gitignore` added; secrets (`.env`), databases, logs "
            "and stale bytecode are no longer committed. `.env.example` is the new "
            "committed template (`cp .env.example .env`).\n"
            "• **Documentation** — `docs/FEATURES.md` (Short/Full parity matrix), "
            "`docs/CHANGELOG.md` and `docs/PERFORMANCE.md` now exist and are "
            "cross-referenced from the code.\n"
            "• **Premium package parity** — the tickettool / reactionroles packages "
            "are byte-identical across the Short and Full editions again.\n"
            "• **Version metadata** — the edition + version now show in the startup "
            "banner and the `!help` footer."
        ),
        inline=False,
    )
    e7.add_field(
        name="✅ Verification system (rebuilt)",
        value=(
            "Button-based verification with persistent panels that survive restarts, "
            "account-age gating, unverified role on join, timeout sweep with optional "
            "kick, and full logging. `!vsetup` configures everything."
        ),
        inline=False,
    )
    e7.add_field(
        name="📊 Native polls (upgraded)",
        value=(
            "Polls now use Discord's native poll API — auto-end, stored results, "
            "bar-chart announcements and `!poll list` history."
        ),
        inline=False,
    )
    e7.add_field(
        name="📈 Invite tracking (rebuilt)",
        value=(
            "Real join attribution: compares invite snapshots on every join, handles "
            "vanity URLs, rejoins, and fake joins. `!invites leaderboard`."
        ),
        inline=False,
    )
    e7.add_field(
        name="⭐ Leveling (rebuilt)",
        value=(
            "MEE6-style XP curve with anti-farming (cooldown + duplicate-message "
            "detection), level role rewards, and configurable announcements. Existing "
            "XP data was preserved."
        ),
        inline=False,
    )
    e7.add_field(
        name="🛡 AutoMod rules engine (new)",
        value=(
            "11 rule types (words, regex, invites, links, spam, duplicates, mentions, "
            "caps, emojis, repeated chars, newlines) × combinable actions "
            "(delete/warn/strike/timeout/kick/ban/notify) with exemptions, priorities, "
            "escalation and an optional XP penalty."
        ),
        inline=False,
    )
    e7.add_field(
        name="🧹 Removed",
        value=(
            "Giveaways, gang/server rules system and the old DM application flow are "
            "gone — the bot is now focused on the six core systems. All ticket-system "
            "features (automation, SLA, CSAT, transcripts, flows) are unchanged."
        ),
        inline=False,
    )
    e7.set_footer(text=f"Page 7/8 • FactionBot — focused, all-in-one.")
    embeds.append(e7)

    # --- Page 8: Multi-Guild Licensing (FactionAccess) ---
    e8 = discord.Embed(
        title="🛡️ FactionAccess — Running the Bot for Allied Factions",
        description=(
            "This server is the **home faction** and keeps every system. You can also "
            "license the SAME bot out to allied factions — they add it to their server "
            "and get only the systems you grant, while you keep license authority.\n\n"
            "Everything below is managed with `!license` subcommands."
        ),
        color=discord.Color.dark_teal(),
        timestamp=datetime.now(timezone.utc),
    )
    e8.add_field(
        name="1️⃣ Get the bot into their server",
        value=(
            "`!license invite <their guild id>` prints an OAuth link locked to that "
            "guild. Their leader (needs Manage Server) opens it and authorizes. The "
            "bot joins **pending** — nothing works there yet."
        ),
        inline=False,
    )
    e8.add_field(
        name="2️⃣ Approve + grant systems",
        value=(
            "You get a DM join request. Then:\n"
            "`!license approve <guild> verification`  ← grants only verification\n"
            "`!license grant <guild> tickets leveling` ← add more later\n"
            "`!license ungrant <guild> tickets`       ← take one away\n"
            "`!license catalog`                       ← every bundle + command count\n\n"
            "_Short's core systems are per-guild by design — an allied faction you "
            "grant `verification` to can run `!verification setup` IN THEIR OWN "
            "server and the whole flow works there independently._"
        ),
        inline=False,
    )
    e8.add_field(
        name="3️⃣ Give them their own identity",
        value=(
            "`!license identity <guild> tag ALLY name \"Ally Faction\" display \"ALLY Moderation\"`\n"
            "The bot's NICKNAME changes per server and embed footers rebrand there "
            "automatically. The global gang name and presence stay yours."
        ),
        inline=False,
    )
    e8.add_field(
        name="4️⃣ Keep control",
        value=(
            "• `!license suspend` / `resume` / `revoke` — instant off-switches\n"
            "• `!license expiry <guild> 30d` — time-limited licenses that auto-suspend\n"
            "• `!license authority add @user` — share license authority without giving "
            "up the bot (stored in settings, never hardcoded)\n"
            "• `!license audit` — every action on record\n"
            "• Allied leaders reach you with `!request` (rate-limited)\n\n"
            "_Default-deny: any command not in a granted bundle is home-only, and "
            "home-config automations (welcome messages, blacklist scans) never run "
            "in allied servers._"
        ),
        inline=False,
    )
    e8.set_footer(text=f"Page 8/8 • FactionAccess — you stay in control.")
    embeds.append(e8)

    return embeds


async def send_owner_tutorial(force: bool = False) -> None:
    """
    DM the bot application owner the full setup tutorial.

    Fires automatically once on first startup (guarded by a flag file).
    Pass force=True to re-send regardless of the flag (used by !tutorial).
    """
    try:
        if not force and os.path.exists(TUTORIAL_FLAG_FILE):
            return

        # Fetch the application owner from Discord.
        app_info = await bot.application_info()
        owner = app_info.owner
        if owner is None:
            logging.warning("[Tutorial] Could not resolve bot owner; skipping tutorial DM.")
            return

        embeds = build_tutorial_embeds()
        sent = 0
        for embed in embeds:
            try:
                await owner.send(embed=embed)
                sent += 1
            except discord.Forbidden:
                logging.warning("[Tutorial] Owner has DMs closed; cannot send tutorial page %d.", sent + 1)
                break
            except discord.HTTPException as exc:
                logging.warning("[Tutorial] Failed to send tutorial page %d: %s", sent + 1, exc)
                break

        if sent > 0:
            logging.info(f"[Tutorial] Sent {sent}/{len(embeds)} tutorial pages to owner {owner}.")
            print(f"[Tutorial] Sent {sent}/{len(embeds)} tutorial pages to owner {owner}.")
            # Mark as sent so it does not fire again on next startup.
            try:
                with open(TUTORIAL_FLAG_FILE, "w", encoding="utf-8") as fh:
                    fh.write(datetime.now(timezone.utc).isoformat())
            except Exception as exc:
                logging.warning(f"[Tutorial] Could not write flag file: {exc}")
        else:
            logging.warning("[Tutorial] No tutorial pages were delivered. Owner DMs may be closed.")
            print("[Tutorial] WARNING: Could not DM the owner. Run !tutorial in a server to retry.")
    except Exception as exc:
        logging.exception("[Tutorial] Failed to send owner tutorial: %s", exc)
        print(f"[Tutorial] ERROR: {exc}")


@bot.event
async def on_ready() -> None:
    global ticket_tool
    global _on_ready_initialized

    print(f'Logged in as {bot.user.name}')
    print(f'Bot started at: {time.strftime("%Y-%m-%d %H:%M:%S")}')
    logging.info(f'Bot started as {bot.user.name}')

    # Re-set presence on every connect — Discord clears the bot's presence
    # on a gateway reconnect, so this must run each time on_ready fires.
    # Respects the `use_bot_status` OWS toggle: when ON, show the custom
    # "Watching <bot_status>" activity; when OFF, clear the activity.
    await apply_bot_presence()

    # -----------------------------------------------------------------------
    # RECONNECT PATH: on_ready fires again on every gateway reconnect. The
    # one-time init (DB, caches, view registration, task starts)
    # is handled by setup_hook + the _on_ready_initialized guard below. On a
    # reconnect, we only need to re-scan invites (they may have changed while
    # we were disconnected).
    # -----------------------------------------------------------------------
    if _on_ready_initialized:
        logging.info("[on_ready] Reconnect: one-time init already done (setup_hook); skipping.")
        return

    # -----------------------------------------------------------------------
    # FIRST-CONNECT PATH: everything below runs exactly once.
    # DB connect + cache loads + generic persistent views are already done in
    # setup_hook (which runs before on_ready). This block handles the
    # guild-dependent work that needs bot.guilds to be fully cached.
    # -----------------------------------------------------------------------
    _on_ready_initialized = True

    # --- FACTIONACCESS ON_READY HOOK ---
    # Classifies every command into its feature bundle, adopts the home
    # guild when no explicit setting exists, registers pre-existing
    # guilds as pending, applies per-guild identity nicknames and starts
    # the license-expiry sweeper. Must run BEFORE any command can be
    # answered in an allied guild.
    try:
        FactionAccess.wiring.on_ready_hook(bot)
    except Exception as exc:
        logging.exception(f"[on_ready] FactionAccess.on_ready_hook failed: {exc}")

    if _is_lead_instance() and ows_get("first_startup_tutorial"):
        await send_owner_tutorial(force=False)
    else:
        logging.info("[Tutorial] First-startup tutorial disabled via OWS or handled by the lead bot; skipping")

    if _is_lead_instance():
        process_manager.clear_lock_file()
        if not send_report_message.is_running():
            send_report_message.start()

    # `ticket_tool` is created in setup_hook. This block just verifies it
    # exists — if setup_hook silently failed we log loudly rather than
    # letting ticket commands crash with AttributeError later.
    if ticket_tool is None:
        logging.error(
            "[on_ready] ticket_tool is None! setup_hook may have failed. "
            "Ticket commands will be unavailable until restart."
        )
    else:
        logging.info("[on_ready] Ticket tool system confirmed available")

    # --- PREMIUM TIER 1 ON_READY HOOK ---
    # Sets up automation-engine global refs + starts the delayed-automation /
    # SLA background timer loop + re-arms persisted delayed timers.
    # Domain split: the minute loop (timers + SLA + review deadlines) must run
    # on exactly ONE bot (the ticket bot) or actions would double-fire; the
    # global refs + review views are safe on every instance.
    if PREMIUM_AVAILABLE:
        try:
            TicketTool.wiring.on_ready_hook(
                data_manager, bot, ticket_tool,
                start_loop=instance_handles('ticket'),
            )
        except Exception as exc:
            logging.exception(f"[on_ready] TicketTool.on_ready_hook failed: {exc}")

    for guild in bot.guilds:
        guild_panels = data_manager.load_ticket_panels_by_guild(guild.id)
        for panel in guild_panels:
            view = TicketPanelView(panel)
            bot.add_view(view)
    logging.info(f"[TicketTool] Registered views for panels")

    # Warm the reaction-panel lookup cache (TicketTool reaction panels).
    for guild in bot.guilds:
        try:
            for row in data_manager.load_reaction_panels_by_guild(guild.id):
                try:
                    mapping = json.loads(row.get('mapping') or '{}')
                except (ValueError, TypeError):
                    continue
                if isinstance(mapping, dict) and mapping:
                    _cache_reaction_panel(row['message_id'], mapping)
        except Exception as exc:
            logging.warning(f"[ReactionPanel] cache warm failed in {guild.name}: {exc}")
    if _reaction_panel_cache:
        logging.info(f"[TicketTool] Warmed reaction-panel cache ({len(_reaction_panel_cache)} message(s))")

    # Re-register the persistent views for every stored multi-panel message
    # (TicketTool Attached Panels / Dropdown Style). Views are rebuilt from
    # the CURRENT panel rows so button labels / select options are fresh.
    for guild in bot.guilds:
        try:
            multi_rows = data_manager.load_multi_panels_by_guild(guild.id)
        except Exception as exc:
            logging.warning(f"[TicketTool] multi-panel load failed in {guild.name}: {exc}")
            continue
        for row in multi_rows:
            try:
                panel_ids = json.loads(row.get('panel_ids') or '[]')
                panels = [p for p in (data_manager.load_ticket_panel(pid) for pid in panel_ids) if p and p.get('is_active', 1)]
                if panels:
                    bot.add_view(build_multi_panel_view(row, panels))
                else:
                    logging.warning(f"[TicketTool] multi-panel message {row.get('message_id')} has no active panels; skipping view registration")
            except Exception as exc:
                logging.warning(f"[TicketTool] multi-panel view registration failed for {row.get('message_id')}: {exc}")
    logging.info("[TicketTool] Registered views for multi-panels")

    if instance_handles('ticket') and ows_get("startup_orphan_cleanup"):
        for guild in bot.guilds:
            cleaned = 0
            for status in ('open', 'pending', 'closing'):
                tickets = data_manager.load_tickets_by_guild(guild.id, status)
                for ticket in tickets:
                    channel_id = ticket.get('channel_id')
                    if channel_id is None or not guild.get_channel(channel_id):
                        ticket['status'] = 'closed'
                        ticket['close_reason'] = ticket.get('close_reason') or f'Reconciled on startup (was {status})'
                        if not ticket.get('closed_at'):
                            ticket['closed_at'] = datetime.now(timezone.utc).isoformat()
                        data_manager.save_ticket(ticket)
                        cleaned += 1
            if cleaned:
                logging.info(f"[TicketTool] Reconciled {cleaned} interrupted/orphaned ticket(s) in {guild.name}")
    else:
        logging.info("[TicketTool] Startup orphan cleanup disabled via OWS or another bot's domain; skipping")

    gar_refreshed = 0
    gar_pruned = 0
    if instance_handles('utility'):
        for guild in bot.guilds:
            tracked = data_manager.load_getallroles_messages(guild_id=guild.id)
            for row in tracked:
                channel = guild.get_channel(row['channel_id'])
                if channel is None:
                    data_manager.delete_getallroles_message(row['message_id'])
                    gar_pruned += 1
                    continue
                try:
                    message = await channel.fetch_message(row['message_id'])
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    data_manager.delete_getallroles_message(row['message_id'])
                    gar_pruned += 1
                    continue
                try:
                    embed = build_getallroles_embed(guild)
                    await message.edit(embed=embed, view=GetAllRolesView())
                    gar_refreshed += 1
                except (discord.HTTPException, discord.Forbidden) as e:
                    logging.warning(f"[GetAllRoles] Could not refresh message {row['message_id']} on startup: {e}")
        if gar_refreshed or gar_pruned:
            logging.info(f"[GetAllRoles] Startup refresh: {gar_refreshed} refreshed, {gar_pruned} pruned")

    if instance_handles('mod') and not auto_blacklist_scan.is_running():
        auto_blacklist_scan.start()
    logging.info("[Blacklist] Auto-scan task assigned to the moderation bot" if not instance_handles('mod') else "[Blacklist] Started scheduled auto-scan task")

    if instance_handles('ticket') and not check_sla_task.is_running():
        check_sla_task.start()
        logging.info("[SLA] Started SLA check task")

    if instance_handles('ticket') and ows_get("auto_close_tickets") and not check_auto_close_task.is_running():
        check_auto_close_task.start()
        logging.info("[AutoClose] Started idle-ticket auto-close task")

    if instance_handles('mod'):
        await restore_temp_mutes()
        if not check_temp_mutes_task.is_running():
            check_temp_mutes_task.start()
            logging.info("[TempMute] Started temp-mute background check task")

    try:
        if instance_handles('mod'):
            pruned = data_manager.prune_message_cache(keep_recent=5000)
            if pruned:
                logging.info(f"[MsgLog] Startup prune removed {pruned} cached message(s)")
            if not prune_message_cache_task.is_running():
                prune_message_cache_task.start()
                logging.info("[MsgLog] Started message-cache prune task")
    except Exception as exc:
        logging.warning(f"[MsgLog] Could not start prune task: {exc}")

    try:
        for g in bot.guilds:
            # RR count now comes from the extracted ReactionRolesDB accessor
            # (bot.reaction_roles_db). Falls back to 0 if the package isn't
            # loaded yet.
            _rr_db = getattr(bot, 'reaction_roles_db', None)
            rr_count = _rr_db.count_reaction_roles(g.id) if _rr_db else 0
            sticky_on = StickyRoleSystem.is_enabled(g.id)
            ml_cfg = MessageLogSystem.get_config(g.id)
            branding = data_manager.get_branding(g.id)
            logging.info(
                f"[Premium] {g.name}: RR={rr_count}/250 sticky={'on' if sticky_on else 'off'} "
                f"msglog={'on' if ml_cfg.get('enabled') else 'off'} "
                f"branding_footer={'set' if branding.get('embed_footer') else 'default'}"
            )
    except Exception as exc:
        logging.debug(f"[Premium] startup summary failed: {exc}")

    # =========================================================================
    # RESTORE ACTIVE OWS PANEL (so the owner doesn't have to type !ows again)
    # =========================================================================
    if not instance_handles('utility'):
        return
    ows_state = data_manager.load_ows_panel_state()
    if ows_state:
        try:
            channel = bot.get_channel(ows_state['channel_id'])
            if channel:
                message = await channel.fetch_message(ows_state['message_id'])
                view = OwnerSettingsView(ows_state['owner_id'])
                view.current_category = ows_state.get('current_category', OWS_CATEGORIES[0])
                view._build_components()
                view.message = message
                await message.edit(view=view)
                logging.info(f"[OWS] Restored active owner settings panel from previous session.")
        except discord.NotFound:
            data_manager.delete_ows_panel_state()
            logging.info("[OWS] Active panel message was deleted, cleared panel state.")
        except Exception as exc:
            logging.warning(f"[OWS] Could not restore active panel: {exc}")

@bot.event
async def on_member_join(member: discord.Member) -> None:
    # --- FACTIONACCESS AUTOMATION SCOPE ---
    # Automated join behaviors (blacklist auto-ban, welcome message,
    # sticky-role restore) are bound to the home faction's global
    # channel/role config, so they stay HOME-GUILD-ONLY in the
    # multi-guild model. Allied factions get the systems they were
    # granted via commands (which carry their own per-guild config),
    # never unsolicited home-config automation.
    _fa = getattr(bot, 'faction_access', None)
    if _fa is not None and not _fa.is_home(member.guild.id):
        return

    # Domain split: moderation bots handle the blacklist auto-ban; utility
    # bots handle welcome messages + sticky roles. (Full bot: both.)
    if instance_handles('mod'):
        try:
            banned = await auto_ban_if_blacklisted(member, source="member_join")
            if banned:
                return
        except Exception as e:
            logging.error(f"Error in blacklist join check: {str(e)}")

    if instance_handles('utility'):
        try:
            if ows_get("welcome_messages"):
                channel = bot.get_channel(config.channels.welcome)
                welcome_message = brand_text(random.choice(WELCOME_TEMPLATES)).format(mention=member.mention, server=member.guild.name)
                
                embed = discord.Embed(title=f"Welcome to {member.guild.name}!", description=welcome_message, color=discord.Color.green())
                embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
                embed.add_field(name="Member Count", value=member.guild.member_count)
                embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d"))
                
                rules_channel = bot.get_channel(config.channels.rules)
                if rules_channel:
                    embed.add_field(name="Server Rules", value=f"Make sure to follow the server rules {rules_channel.mention}", inline=False)
                
                embed.set_footer(text=f"Joined on {member.joined_at.strftime('%Y-%m-%d')}")
                
                welcome_msg = await channel.send(embed=embed)
                await welcome_msg.add_reaction('🔥')
                await welcome_msg.add_reaction('👋')
                await welcome_msg.add_reaction('💯')
                
                logging.info(f"Sent welcome message for {member}")
        except Exception as e:
            logging.error(f"Error in welcome system: {str(e)}")

    # --- Sticky Roles: re-apply saved roles on rejoin (Dyno premium clone) ---
    if instance_handles('utility'):
        try:
            restored = await StickyRoleSystem.restore_member_roles(member)
            if restored:
                logging.info(f"[Sticky] Restored {restored} role(s) to returning member {member}")
        except Exception as exc:
            logging.exception(f"[Sticky] restore on join failed: {exc}")


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member) -> None:
    if not instance_handles('mod'):
        return
    if after.bot:
        return
    # --- FACTIONACCESS AUTOMATION SCOPE ---
    # The blacklist presence scan uses the home faction's global keyword
    # list — it never extends into allied guilds.
    _fa = getattr(bot, 'faction_access', None)
    if _fa is not None and not _fa.is_home(after.guild.id):
        return
    if not blacklisted_keywords:
        return
    
    before_activities = set(str(a) for a in before.activities)
    after_activities = set(str(a) for a in after.activities)
    
    if before_activities == after_activities:
        return
    
    await auto_ban_if_blacklisted(after, source="presence_update")

# =============================================================================
# MULTI-COMMAND CHAINING
# Allows users to run multiple commands in one message, e.g.:
#   !setupinvites, !regenerateinvites
#   !kick @user, !ban @user
#
# Splits on ", !" (comma + optional whitespace + prefix) so commas inside
# command arguments (e.g. !warn @user reason, with comma) are NOT split.
# =============================================================================
MULTI_COMMAND_SPLIT_REGEX = re.compile(
    r'\s*,\s*(?=' + re.escape(config.command_prefix) + r')'
)
MAX_CHAINED_COMMANDS = 10  # Safety cap to prevent abuse

class CommandCleanupView(View):
    """Prompt to ask staff if they want to delete the original chained command message."""
    def __init__(self, original_message: discord.Message):
        super().__init__(timeout=30.0)
        self.original_message = original_message
        self.cleanup_msg = None

    @discord.ui.button(label="Yes, delete it", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def yes_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.original_message.author.id:
            await interaction.response.send_message("This isn't your command to clean up!", ephemeral=True)
            return
        try:
            await self.original_message.delete()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        # Instead of leaving a "deleted" status message behind, just delete
        # the cleanup prompt so the chat stays clean.
        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            # Fallback: if we can't delete it, at least clear the buttons.
            try:
                await interaction.response.edit_message(embed=None, view=None)
            except Exception:
                pass
        self.stop()

    @discord.ui.button(label="No, keep it", style=discord.ButtonStyle.secondary, emoji="✋")
    async def no_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.original_message.author.id:
            await interaction.response.send_message("This isn't your command to clean up!", ephemeral=True)
            return
        # Instead of leaving a "kept" status message behind, just delete the
        # cleanup prompt so the chat stays clean.
        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            # Fallback: if we can't delete it, at least clear the buttons.
            try:
                await interaction.response.edit_message(embed=None, view=None)
            except Exception:
                pass
        self.stop()

    async def on_timeout(self) -> None:
        # Instead of leaving an "expired" status message behind, just delete
        # the cleanup prompt so the chat stays clean.
        if self.cleanup_msg:
            try:
                await self.cleanup_msg.delete()
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass

# Global dictionary to temporarily hold page offsets and chain status for
# chained commands. Format: {message_id: (page_offset, is_chained)}.
# Bounded so a long-running bot cannot accumulate one entry per chained
# command ever run.
_chain_offsets: "OrderedDict[int, Tuple[int, bool]]" = OrderedDict()
MAX_CHAIN_OFFSETS = 2000


def _set_chain_offset(message_id: int, value: Tuple[int, bool]) -> None:
    """Insert or refresh a chain-offset entry, evicting the oldest when the
    cache exceeds MAX_CHAIN_OFFSETS."""
    _chain_offsets[message_id] = value
    _chain_offsets.move_to_end(message_id)
    while len(_chain_offsets) > MAX_CHAIN_OFFSETS:
        _chain_offsets.popitem(last=False)

# =========================================================================
# OWNER SETTINGS HELPERS
# =========================================================================
def get_owner_setting(key: str, default: bool = True) -> bool:
    """Read a boolean owner setting from the SQLite DB."""
    val = data_manager.get_config_value(f"ows_{key}")
    if val is None:
        return default
    return val == "1"

def set_owner_setting(key: str, value: bool) -> None:
    """Save a boolean owner setting to the SQLite DB."""
    data_manager.set_config_value(f"ows_{key}", "1" if value else "0")

# =========================================================================
# !OWS — OWNER SETTINGS (Complete Feature Toggle System)
# =========================================================================
# Discord-native interactive settings panel (NOT a web dashboard).
# Every toggle is persisted to SQLite bot_config via set_owner_setting()
# with the `ows_` prefix. In-memory config.enable_* flags are hydrated
# from the DB on startup via hydrate_ows_settings().
# =========================================================================

@dataclass
class OWSToggle:
    key: str
    label: str
    description: str
    category: str
    default: bool = True
    emoji: str = ""
    apply: Optional[Callable[[bool], None]] = None

OWS_CATEGORIES: List[str] = [
    "🧩 Core Systems",
    "🤖 Automation",
    "⚙️ Moderation",
    "🎫 Tickets",
    "⭐ Premium",
    "🧹 Logging",
    "🚀 Startup/Owner",
    "🛡️ Security",
    "📊 Limits",
    "💬 Branding",
    "🔧 Misc",
]

def _ows_apply_leveling(v: bool) -> None:
    config.enable_leveling = v

def _ows_apply_tickets(v: bool) -> None:
    config.enable_tickets = v

def _ows_apply_warnings(v: bool) -> None:
    config.enable_warnings = v

def _ows_apply_debug(v: bool) -> None:
    config.debug_mode = v

async def apply_bot_presence() -> None:
    """Apply the bot's Discord presence based on the `use_bot_status` toggle.

    - toggle ON  -> "Watching <bot_status>" (uses brand_text())
    - toggle OFF -> clear the activity entirely

    Safe to call before the gateway is ready (it's awaited from on_ready,
    which only fires after the gateway cache is populated) and on every
    reconnect, since Discord clears presence on reconnect.
    """
    try:
        if ows_get("use_bot_status"):
            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=brand_text(config.bot_status),
                )
            )
        else:
            await bot.change_presence(activity=None)
    except Exception as exc:
        # Don't let a presence failure abort on_ready.
        logging.warning(f"[Presence] Could not apply bot status: {exc}")


def _ows_apply_bot_status(v: bool) -> None:
    """OWS callback for the `use_bot_status` toggle.

    Fires immediately when the owner flips the toggle in the !ows panel so
    the presence updates live without a reconnect. We can't await inside a
    sync callback, so we schedule the coroutine on the running loop.
    """
    try:
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is not None:
            # Called from inside the event loop (e.g. hydrate during
            # setup_hook, or a sync command path). Scheduling without
            # blocking is the only safe option — run_coroutine_threadsafe
            # + result() here would deadlock the loop.
            asyncio.ensure_future(apply_bot_presence())
        else:
            # Called from another thread (OWS UI worker) — schedule on the
            # bot's loop and wait briefly for the presence call.
            fut = asyncio.run_coroutine_threadsafe(apply_bot_presence(), bot.loop)
            # Don't block the OWS UI thread for long; 5s is plenty for a presence call.
            fut.result(timeout=5)
    except Exception as exc:
        logging.warning(f"[OWS] use_bot_status apply error: {exc}")


def _ows_apply_periodic(v: bool) -> None:
    global messages_enabled
    messages_enabled = v
    try:
        if v and not send_report_message.is_running():
            send_report_message.start()
        elif not v and send_report_message.is_running():
            send_report_message.stop()
    except Exception as exc:
        logging.warning(f"[OWS] broadcast task toggle error: {exc}")

def _ows_apply_scheduled_scan(v: bool) -> None:
    try:
        if v and not auto_blacklist_scan.is_running():
            auto_blacklist_scan.start()
        elif not v and auto_blacklist_scan.is_running():
            auto_blacklist_scan.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] blacklist scan task toggle error: {exc}")

def _ows_apply_sla(v: bool) -> None:
    try:
        if v and not check_sla_task.is_running():
            check_sla_task.start()
        elif not v and check_sla_task.is_running():
            check_sla_task.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] SLA task toggle error: {exc}")

def _ows_apply_autoclose(v: bool) -> None:
    """Start/stop the idle-ticket auto-close loop when the OWS toggle flips."""
    try:
        if v and not check_auto_close_task.is_running():
            check_auto_close_task.start()
        elif not v and check_auto_close_task.is_running():
            check_auto_close_task.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] auto-close task toggle error: {exc}")

def _ows_apply_tempmute(v: bool) -> None:
    try:
        if v and not check_temp_mutes_task.is_running():
            check_temp_mutes_task.start()
        elif not v and check_temp_mutes_task.is_running():
            check_temp_mutes_task.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] temp-mute task toggle error: {exc}")

def _ows_apply_msgprune(v: bool) -> None:
    try:
        if v and not prune_message_cache_task.is_running():
            prune_message_cache_task.start()
        elif not v and prune_message_cache_task.is_running():
            prune_message_cache_task.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] msg-cache prune task toggle error: {exc}")

def _ows_apply_invitetask(v: bool) -> None:
    # Invite tracking is reactive (on_member_join / on_member_remove in
    # modules/engagement/invites.py) — there is no background @tasks.loop to start/stop.
    # The enable_invite_tracking OWS flag is checked at runtime via
    # ows_get("enable_invite_tracking") so the toggle takes effect
    # immediately without a callback. (Previously this referenced
    # `check_invites_task` which was never defined — a NameError logged on
    # every toggle flip.)
    pass

OWS_TOGGLES: List[OWSToggle] = [
    OWSToggle("enable_leveling",        "Leveling System",       "XP gain, level-ups, /level, /leaderboard",                    "🧩 Core Systems", True,  "📊", _ows_apply_leveling),
    OWSToggle("enable_tickets",         "Tickets System",        "Panels, creation, transcripts, claim/close",                 "🧩 Core Systems", True,  "🎫", _ows_apply_tickets),
    OWSToggle("enable_warnings",        "Warnings System",       "/warn, /warnings, auto-ban on threshold",                     "🧩 Core Systems", True,  "⚠️", _ows_apply_warnings),
    OWSToggle("enable_verification",    "Verification System",   "!verify flow (disable for maintenance windows)",              "🧩 Core Systems", True,  "🔐"),
    OWSToggle("enable_invite_tracking","Invite Tracking",       "!setupinvites, tracking panel, auto-regen task",             "🧩 Core Systems", True,  "📨", _ows_apply_invitetask),
    OWSToggle("enable_blacklist_engine","Blacklist Engine",      "Keyword storage + matching engine (core)",                    "🧩 Core Systems", True,  "🚫"),

    OWSToggle("auto_ban_profile",       "Auto-Ban (Profile)",   "Scan name/status/custom status on join & presence update",   "🤖 Automation", True,  "👤"),
    OWSToggle("auto_ban_message",       "Auto-Ban (Message)",   "Scan every guild message's content for keywords",             "🤖 Automation", True,  "💬"),
    OWSToggle("auto_scheduled_scan",   "Scheduled Blacklist Scan","Background scan of all members every 2h",                   "🤖 Automation", True,  "🔄", _ows_apply_scheduled_scan),
    OWSToggle("welcome_messages",       "Welcome Messages",     "Greet new members on join",                                   "🤖 Automation", True,  "👋"),
    OWSToggle("periodic_broadcasts",   "Periodic Broadcasts",  "Scheduled reminder/recruitment messages",                    "🤖 Automation", False, "📢", _ows_apply_periodic),
    OWSToggle("sla_breach_alerts",     "SLA Breach Alerts",    "Ping staff if a ticket has no first response",                "🤖 Automation", True,  "⏰", _ows_apply_sla),
    OWSToggle("temp_mute_loop",         "Temp-Mute Loop",       "Persistent unmute background safety net",                     "🤖 Automation", True,  "🔇", _ows_apply_tempmute),
    OWSToggle("msg_cache_prune",        "Message Cache Prune",  "Keep message-log cache under 5,000 rows",                    "🤖 Automation", True,  "🧹", _ows_apply_msgprune),
    OWSToggle("auto_invite_regen",      "Auto Invite Regen",    "Mint new batch when all invites expire",                      "🤖 Automation", True,  "♻️"),
    OWSToggle("startup_orphan_cleanup","Startup Orphan Cleanup","Auto-close tickets whose channels were deleted while offline","🤖 Automation", True,  "🧹"),

    OWSToggle("multi_command_chain",          "Multi-Command Chaining",   "Parse !cmd1, !cmd2, !cmd3 in one message",          "⚙️ Moderation", True,  "⛓️"),
    OWSToggle("restrict_purge_chain",         "Restrict Purge Chain",     "Block !purge from chaining with !setupinvites etc.", "⚙️ Moderation", True,  "🚫"),
    OWSToggle("command_cleanup_prompt",       "Command Cleanup Prompt",   "Yes/No buttons to delete original command msg",     "⚙️ Moderation", True,  "🧹"),
    OWSToggle("auto_delete_command_del",       "Auto-Delete on -del Flag","Delete command message when -del flag is used",     "⚙️ Moderation", True,  "🗑️"),
    OWSToggle("blacklist_in_tickets",         "Blacklist in Tickets",     "Scan messages inside open ticket channels",         "⚙️ Moderation", False, "🎫"),
    OWSToggle("blacklist_alert_only",         "Blacklist Alert Only",     "Log matches instead of auto-banning (review mode)", "⚙️ Moderation", False, "📢"),
    OWSToggle("enforce_account_age",          "Enforce Account Age",      "Block verification for accounts under 6 days",     "⚙️ Moderation", True,  "📅"),
    OWSToggle("verification_cooldown",        "Verification Cooldown",     "1-hour per-user cooldown on !verify",               "⚙️ Moderation", True,  "⏱️"),

    OWSToggle("require_claim_before_reply",   "Require Claim Before Reply","Staff must claim before responding",              "🎫 Tickets", False, "🙋"),
    OWSToggle("mention_support_on_create",   "Mention Support on Create", "Ping support role when a ticket opens",            "🎫 Tickets", True,  "📌"),
    OWSToggle("dm_transcript_on_close",       "DM Transcript on Close",   "Send transcript DM to creator on close",           "🎫 Tickets", True,  "📬"),
    OWSToggle("auto_close_tickets",           "Auto-Close Idle Tickets", "Close tickets after X hours of inactivity",        "🎫 Tickets", False, "⏲️", _ows_apply_autoclose),
    OWSToggle("ticket_rating_prompt",         "Ticket Rating Prompt",    "Star rating before close",                         "🎫 Tickets", True,  "⭐"),
    OWSToggle("private_staff_notes",          "Private Staff Notes",     "/note and /notes commands (staff-only)",           "🎫 Tickets", True,  "📝"),
    OWSToggle("ticket_priority_system",       "Ticket Priority System",  "low / normal / high / urgent priority levels",     "🎫 Tickets", True,  "🚨"),
    OWSToggle("ticket_reopen",                "Ticket Reopen",           "Allow reopening closed tickets",                   "🎫 Tickets", True,  "🔓"),
    OWSToggle("ticket_blacklist",              "Ticket Blacklist",       "Block users from creating tickets",                "🎫 Tickets", True,  "🚫"),
    OWSToggle("pin_ticket_message",           "Pin Ticket Message",      "Pin the ticket welcome message (buttons always visible)", "🎫 Tickets", False, "📌"),

    OWSToggle("reaction_roles",         "Reaction Roles",        "Up to 250 per guild (Carl-bot clone)",            "⭐ Premium", True,  "🎨"),
    OWSToggle("sticky_roles",           "Sticky Roles",          "Re-apply roles on rejoin (Dyno clone)",           "⭐ Premium", False, "📌"),
    OWSToggle("full_message_logging",   "Full Message Logging",  "Edit + delete content snapshots",                  "⭐ Premium", False, "📦"),
    OWSToggle("custom_bot_branding",    "Custom Bot Branding",   "Footer / color / thumbnail / image overrides",     "⭐ Premium", True,  "🎨"),

    OWSToggle("log_message_edits",      "Log Message Edits",     "Snapshot edited message content",                 "🧹 Logging", True,  "✏️"),
    OWSToggle("log_message_deletes",    "Log Message Deletes",   "Snapshot deleted message content",                "🧹 Logging", True,  "🗑️"),
    OWSToggle("log_ignore_bots",         "Ignore Bots in Log",    "Skip bot messages in message log",                "🧹 Logging", True,  "🤖"),
    OWSToggle("log_mod_actions",        "Log Mod Actions",       "Bans, kicks, mutes, warns to log channel",        "🧹 Logging", True,  "🔨"),
    OWSToggle("log_auto_bans",           "Log Auto-Bans",         "Log every blacklist auto-ban",                    "🧹 Logging", True,  "⚡"),
    OWSToggle("log_temp_mute_expiry",   "Log Temp-Mute Expiry",  "Log when a temp-mute ends",                       "🧹 Logging", True,  "🔓"),

    OWSToggle("first_startup_tutorial",  "First-Startup Tutorial","One-time setup DM to owner",                     "🚀 Startup/Owner", True,  "📩"),
    OWSToggle("debug_mode",              "Debug Mode",            "Enable verbose debug logging",                   "🚀 Startup/Owner", False, "🐞", _ows_apply_debug),
    OWSToggle("force_utf8_output",       "Force UTF-8 Output",    "Reconfigure stdout/stderr to UTF-8 (Windows)",   "🚀 Startup/Owner", True,  "🔤"),

    OWSToggle("account_age_check",          "Account Age Check",       "Min 6 days for verification",                "🛡️ Security", True,  "📅"),
    OWSToggle("security_flags_display",     "Security Flags Display",  "Show 'no avatar', 'short name', etc.",       "🛡️ Security", True,  "🚩"),
    OWSToggle("profile_risk_assessment",    "Profile Risk Assessment", "/securitycheck scoring",                    "🛡️ Security", True,  "📊"),
    OWSToggle("blacklist_ban_threshold",    "Warn-Then-Ban Mode",      "Warn N times then ban (vs instant ban)",     "🛡️ Security", False, "⚠️"),
    OWSToggle("warnings_auto_ban",           "Warnings Auto-Ban",      "Auto-ban when warnings exceed threshold",    "🛡️ Security", True,  "🔨"),

    OWSToggle("enforce_max_tickets",           "Enforce Max Tickets/User",    "Limit tickets per user (config.limits.max_tickets_per_user)",   "📊 Limits", True, "🎫"),
    OWSToggle("enforce_max_poll_options",      "Enforce Max Poll Options",   "Cap poll options (config.limits.max_poll_options)",              "📊 Limits", True, "📊"),
    OWSToggle("enforce_min_blacklist_len",     "Enforce Min Keyword Length", "Min length for blacklist keywords",                              "📊 Limits", True, "📏"),
    OWSToggle("enforce_verification_timeout",  "Enforce Verification Timeout","Timeout on verification session",                                "📊 Limits", True, "⏱️"),
    OWSToggle("enforce_auto_scan_interval",   "Enforce Auto-Scan Interval",  "2h between scheduled blacklist scans",                           "📊 Limits", True, "🔄"),
    OWSToggle("enforce_invite_check_interval","Enforce Invite Check Interval","5min between invite status checks",                              "📊 Limits", True, "📨"),

    OWSToggle("use_gang_name",              "Use Gang Name",            "Apply gang name in brand_text()",            "💬 Branding", True,  "🏷️"),
    OWSToggle("use_gang_abbreviation",     "Use Gang Abbreviation",    "Apply abbreviation in brand_text()",         "💬 Branding", True,  "🏷️"),
    OWSToggle("use_bot_status",             "Use Custom Bot Status",   "Show custom 'Watching' status",              "💬 Branding", True,  "👀", _ows_apply_bot_status),
    OWSToggle("use_embed_footer_override", "Embed Footer Override",   "Use per-guild custom embed footer",          "💬 Branding", False, "📄"),
    OWSToggle("use_embed_color_override",  "Embed Color Override",    "Use per-guild custom embed color",           "💬 Branding", False, "🎨"),
    OWSToggle("use_embed_thumbnail_override","Embed Thumbnail Override","Use per-guild custom embed thumbnail",     "💬 Branding", False, "🖼️"),

    OWSToggle("process_lock_file",           "Process Lock File",       "Prevent concurrent bot instances (bot_busy.lock)", "🔧 Misc", True,  "🔒"),
    OWSToggle("persistent_views",            "Persistent Views",        "Re-attach buttons to old messages on restart",     "🔧 Misc", True,  "🔁"),
    OWSToggle("json_to_sqlite_import",       "JSON → SQLite Import",   "One-time import on first launch",                   "🔧 Misc", True,  "📦"),
    OWSToggle("multi_command_max_chain",     "Max Chain Length (10)",  "Enforce MAX_CHAINED_COMMANDS cap",                 "🔧 Misc", True,  "⛓️"),
]

_OWS_TOGGLE_MAP: Dict[str, OWSToggle] = {t.key: t for t in OWS_TOGGLES}

def ows_get(key: str) -> bool:
    """Read an OWS toggle from the DB, applying the toggle's default if unset."""
    t = _OWS_TOGGLE_MAP.get(key)
    default = t.default if t else True
    return get_owner_setting(key, default)

def ows_set(key: str, value: bool) -> None:
    """Persist an OWS toggle to the DB and fire its apply callback immediately."""
    set_owner_setting(key, value)
    t = _OWS_TOGGLE_MAP.get(key)
    if t and t.apply:
        try:
            t.apply(value)
        except Exception as exc:
            logging.warning(f"[OWS] apply callback for '{key}' failed: {exc}")

def hydrate_ows_settings() -> None:
    """Load every OWS toggle from the DB and apply to in-memory state."""
    for t in OWS_TOGGLES:
        val = get_owner_setting(t.key, t.default)
        if t.apply:
            try:
                t.apply(val)
            except Exception as exc:
                logging.warning(f"[OWS] hydrate '{t.key}' failed: {exc}")
    total = len(OWS_TOGGLES)
    enabled = sum(1 for t in OWS_TOGGLES if get_owner_setting(t.key, t.default))
    logging.info(f"[OWS] Hydrated {total} owner settings — {enabled}/{total} enabled")

def ows_bulk_set_category(category: str, value: bool) -> int:
    """Enable or disable every toggle in a category. Returns count changed."""
    changed = 0
    for t in OWS_TOGGLES:
        if t.category == category:
            ows_set(t.key, value)
            changed += 1
    return changed

class OwnerSettingsView(View):
    MAX_TOGGLE_BUTTONS = 15

    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.current_category: str = OWS_CATEGORIES[0]
        self.message: Optional[discord.Message] = None
        self._build_components()

    def _build_components(self) -> None:
        self.clear_items()
        cat_select = discord.ui.Select(
            placeholder="📋 Select a category to configure…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=cat,
                    value=cat,
                    description=f"{sum(1 for t in OWS_TOGGLES if t.category == cat)} toggle(s)",
                )
                for cat in OWS_CATEGORIES
            ],
            row=0,
        )
        cat_select.callback = self._on_category_selected
        self.add_item(cat_select)

        toggles = self._toggles_for_category()
        for idx, t in enumerate(toggles[:self.MAX_TOGGLE_BUTTONS]):
            row = 1 + (idx // 5)
            if row > 3:
                break
            enabled = ows_get(t.key)
            icon = "✅" if enabled else "❌"
            label = f"{icon} {t.label}"[:80]
            style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
            btn = Button(label=label, style=style, row=row)
            btn.callback = self._make_toggle_callback(t.key)
            self.add_item(btn)

        enable_all = Button(label="Enable All", style=discord.ButtonStyle.success, row=4, emoji="🔓")
        enable_all.callback = self._on_enable_all
        self.add_item(enable_all)

        disable_all = Button(label="Disable All", style=discord.ButtonStyle.secondary, row=4, emoji="🔒")
        disable_all.callback = self._on_disable_all
        self.add_item(disable_all)

        close_btn = Button(label="Close", style=discord.ButtonStyle.danger, row=4, emoji="✖")
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    def _toggles_for_category(self) -> List[OWSToggle]:
        return [t for t in OWS_TOGGLES if t.category == self.current_category]

    def _make_toggle_callback(self, key: str):
        async def _callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("❌ This isn't your settings panel!", ephemeral=True)
                return
            new_val = not ows_get(key)
            ows_set(key, new_val)
            logging.info(f"[OWS] {interaction.user} toggled '{key}' → {'ON' if new_val else 'OFF'}")
            self._build_components()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        return _callback

    async def _on_category_selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your settings panel!", ephemeral=True)
            return
        self.current_category = self.category_select_placeholder_values(interaction)
        self._build_components()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)
        # Save the new category so it survives a restart
        data_manager.save_ows_panel_state(self.message.id, self.message.channel.id, self.author_id, self.current_category)

    def category_select_placeholder_values(self, interaction: discord.Interaction) -> str:
        for child in self.children:
            if isinstance(child, discord.ui.Select):
                if child.values:
                    return child.values[0]
        return self.current_category

    async def _on_enable_all(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your settings panel!", ephemeral=True)
            return
        count = ows_bulk_set_category(self.current_category, True)
        logging.info(f"[OWS] {interaction.user} enabled all {count} toggles in '{self.current_category}'")
        self._build_components()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_disable_all(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your settings panel!", ephemeral=True)
            return
        count = ows_bulk_set_category(self.current_category, False)
        logging.info(f"[OWS] {interaction.user} disabled all {count} toggles in '{self.current_category}'")
        self._build_components()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_close(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your settings panel!", ephemeral=True)
            return
        self.stop()
        data_manager.delete_ows_panel_state() # Clean up DB on close
        try:
            await interaction.message.delete()
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            for child in self.children:
                child.disabled = True
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                pass

    def _build_embed(self) -> discord.Embed:
        toggles = self._toggles_for_category()
        enabled_count = sum(1 for t in toggles if ows_get(t.key))
        total = len(toggles)

        embed = discord.Embed(
            title=f"⚙️ Owner Settings — {self.current_category}",
            description=(
                f"**{enabled_count}/{total}** features enabled in this category.\n"
                f"Click a button to flip it. Every change is saved to the database instantly.\n"
            ),
            color=discord.Color.blurple() if enabled_count >= total // 2 else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        lines: List[str] = []
        for t in toggles:
            val = ows_get(t.key)
            icon = "🟢" if val else "🔴"
            lines.append(f"{icon} **{t.label}**\n   ↳ {t.description}")

        chunk_size = 4
        for i in range(0, len(lines), chunk_size):
            chunk = lines[i:i + chunk_size]
            field_num = (i // chunk_size) + 1
            field_name = "Features" if field_num == 1 else f"Features (cont. {i + 1}–{i + len(chunk)})"
            embed.add_field(name=field_name, value="\n".join(chunk), inline=False)

        pct = (enabled_count / total * 100) if total else 0
        filled = int(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)
        embed.add_field(
            name="📊 Category Progress",
            value=f"`{bar}` **{pct:.0f}%** ({enabled_count}/{total})",
            inline=False,
        )

        all_enabled = sum(1 for t in OWS_TOGGLES if ows_get(t.key))
        embed.set_footer(
            text=f"Overall: {all_enabled}/{len(OWS_TOGGLES)} features enabled • "
                 f"Use the dropdown to switch categories • Owner-only"
        )
        return embed

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        data_manager.delete_ows_panel_state() # Clean up DB on timeout
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

@bot.command(name="ows", aliases=["OWS", "Ows", "ownerws", "ownersettings"])
@commands.is_owner()
async def ows_cmd(ctx: commands.Context) -> None:
    """Open the Owner Settings panel — toggle any bot feature on or off."""
    view = OwnerSettingsView(ctx.author.id)
    embed = view._build_embed()
    view.message = await ctx.send(embed=embed, view=view)
    # Save the panel state to the database
    data_manager.save_ows_panel_state(view.message.id, ctx.channel.id, ctx.author.id, view.current_category)
    logging.info(f"[OWS] Owner settings panel opened by {ctx.author}")

async def process_potential_multi_command(message: discord.Message) -> None:
    content = message.content
    prefix = config.command_prefix

    if content.startswith(f"{prefix}kick ") or content.startswith(f"{prefix}ban "):
        parts_initial = content.split(" ", 1)
        if len(parts_initial) > 1:
            parts_initial[1] = parts_initial[1].replace(",", " ")
            content = " ".join(parts_initial)
            message.content = content

    if not content.startswith(prefix):
        await bot.process_commands(message)
        return

    parts = MULTI_COMMAND_SPLIT_REGEX.split(content)

    if len(parts) <= 1 or not ows_get("multi_command_chain"):
        await bot.process_commands(message)
        return

    if len(parts) > MAX_CHAINED_COMMANDS:
        try:
            await message.reply(
                f"⚠️ Too many commands chained (max {MAX_CHAINED_COMMANDS}). "
                f"Please split them into separate messages.",
                delete_after=10,
            )
        except discord.HTTPException:
            pass
        return

    normalized_content = content.strip()
    if ' -del' in normalized_content and not normalized_content.lower().endswith('-del'):
        try:
            await message.reply(
                "⚠️ **Invalid Command Format**\n"
                "You cannot use `-del` in the middle of a multi-command chain.\n"
                "If you want to delete the command message, please put `-del` at the **very end** of the entire chain.\n\n"
                "**Correct Format:** `!purgeall, !cmds, !cmds, !cmds, !cmds, !cmds, !setupinvites, !regenerateinvites -del`",
                delete_after=30
            )
        except discord.HTTPException:
            pass
        return

    global_del_flag = False
    if parts[-1].strip().lower().endswith('-del'):
        global_del_flag = True
        parts[-1] = parts[-1].strip()[:-4].strip()

    cmd_names = []
    for part in parts:
        part = part.strip()
        if part.endswith(','):
            part = part[:-1].strip()
        if part.startswith(prefix):
            cmd_names.append(part[len(prefix):].split(" ")[0].lower())
            
    restrict_purge = get_owner_setting("restrict_purge_chain", True)
    if restrict_purge:
        has_purge = any(name in ['purge', 'purgeall'] for name in cmd_names)
        has_restricted = any(name in ['cmds', 'setupinvites', 'regenerateinvites'] for name in cmd_names)
        
        if has_purge and has_restricted:
            try:
                await message.reply(
                    "⚠️ **Invalid Command Format**\n"
                    "You cannot chain `!purge` or `!purgeall` with `!cmds`, `!setupinvites`, or `!regenerateinvites`. \n"
                    "Please run it separately, or ask the owner to disable this restriction with `!ows`.",
                    delete_after=30
                )
            except discord.HTTPException:
                pass
            return

    chain_command_counts = {}
    is_chained = len(cmd_names) > 1
    handled_here = 0  # commands in this chain that THIS instance owns

    for idx, part in enumerate(parts, 1):
        part = part.strip()
        
        if part.endswith(','):
            part = part[:-1].strip()
            
        if not part or not part.startswith(prefix):
            continue

        msg_copy = copy.copy(message)
        msg_copy.content = part

        cmd_name = part[len(prefix):].split(" ")[0].lower()
        chain_command_counts[cmd_name] = chain_command_counts.get(cmd_name, 0) + 1
        _set_chain_offset(msg_copy.id, (chain_command_counts[cmd_name] - 1, is_chained))

        if bot.get_command(cmd_name) is not None:
            handled_here += 1

        try:
            await bot.process_commands(msg_copy)
        except Exception as exc:
            logging.error(f"[MultiCommand] Error running command {idx}/{len(parts)} ('{part}'): {exc}")
            try:
                await message.reply(f"⚠️ Command `{part}` failed: `{exc}`", delete_after=15)
            except discord.HTTPException:
                pass

    # Domain split: only an instance that actually executed at least one
    # command from this chain may post the cleanup prompt (otherwise every
    # domain bot would prompt for the same message).
    if handled_here == 0:
        return

    is_staff = False
    if message.guild:
        if message.author.guild_permissions.manage_messages or message.author.guild_permissions.administrator:
            is_staff = True

    if global_del_flag and ows_get("auto_delete_command_del"):
        try:
            await message.delete()
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass
        return

    command_message_still_exists = True
    try:
        if message.channel is not None:
            try:
                await message.channel.fetch_message(message.id)
            except (discord.NotFound, discord.HTTPException):
                command_message_still_exists = False
    except Exception:
        pass

    if is_staff and command_message_still_exists and ows_get("command_cleanup_prompt"):
        cleanup_embed = discord.Embed(
            title="🧹 Cleanup Command Message?",
            description="Do you want to delete the original command message to keep chat clean?",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        original_content_preview = message.content[:1024]
        cleanup_embed.add_field(name="Original Message", value=f"> {original_content_preview}", inline=False)
        
        view = CommandCleanupView(message)
        view.cleanup_msg = await message.channel.send(embed=cleanup_embed, view=view)

@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    # --- FACTIONACCESS AUTOMATION SCOPE ---
    # Non-command automations must respect multi-guild licensing:
    #   * home-guild-only automations (blacklist scans, message-log
    #     caching, premium custom-command dispatch) — they read the
    #     home faction's global config;
    #   * bundle-following automations (leveling XP, handled inside the
    #     leveling cog's own listener) — the data is per-guild, so they
    #     run wherever the bundle is granted.
    # A missing service (pre-setup_hook) keeps the legacy behavior.
    _fa = getattr(bot, 'faction_access', None)
    _fa_home = (_fa is None or message.guild is None
                or _fa.automation_allowed(message.guild.id, None))
    _fa_tickets = (_fa is None or message.guild is None
                   or _fa.automation_allowed(message.guild.id, 'tickets'))

    # --- VERIFICATION CHANNEL AUTO-PURGE HOOK ---
    # Any human message in the verification channel counts as activity: it
    # cancels any in-progress purge countdown (during the 2-minute warning
    # window) and re-arms a fresh 3-minute idle watcher. This is what makes
    # "if it becomes active again during those 2 minutes, cancel the purge"
    # work, AND what triggers the initial idle check after the first message.
    # (Domain split: verification belongs to the moderation bot.)
    try:
        if instance_handles('mod') and message.guild is not None and is_verification_channel(message.channel.id, message.channel):
            mgr = get_auto_purge_manager(message.channel.id)
            asyncio.create_task(mgr.record_activity())
    except Exception as exc:
        logging.debug(f"[AutoPurge] record_activity failed: {exc}")

    open_ticket = None
    any_ticket = None
    if message.guild and ticket_tool:
        try:
            ticket = data_manager.load_ticket_by_channel(message.channel.id)
            if ticket:
                any_ticket = ticket
                if ticket.get('status') == 'open':
                    open_ticket = ticket
        except Exception as e:
            logging.warning(f"[on_message] Ticket lookup failed: {e}")

    # Blacklist message scanning. Ticket channels are exempt UNLESS the OWS
    # `blacklist_in_tickets` toggle is enabled (previously the toggle existed
    # but was never read and ticket channels were unconditionally exempt).
    # The exemption now covers ANY ticket channel — including two-step closed
    # channels, where staff would otherwise be auto-banned for discussing a
    # blacklisted keyword.
    _ticket_exempt = any_ticket is not None and not ows_get("blacklist_in_tickets")
    if (instance_handles('mod') and message.guild and blacklisted_keywords
            and not _ticket_exempt and ows_get("auto_ban_message") and _fa_home):
        content = message.content or ""
        if not content.startswith(config.command_prefix):
            found, keyword = check_text_for_keywords(content)
            if found:
                author = message.author
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    pass

                log_channel = bot.get_channel(config.channels.log)
                snippet = content if len(content) <= 900 else (content[:900] + "...")
                if log_channel:
                    try:
                        embed = discord.Embed(
                            title="🚨 Blacklisted Keyword in Message",
                            description=(
                                f"**User:** {author.mention} (`{author.name}` / `{author.id}`)\n"
                                f"**Channel:** {message.channel.mention}\n"
                                f"**Matched Keyword:** `{keyword}`"
                            ),
                            color=discord.Color.red(),
                            timestamp=datetime.now(timezone.utc),
                        )
                        embed.add_field(name="Message Content", value=snippet, inline=False)
                        await log_channel.send(embed=embed)
                    except Exception as e:
                        logging.warning(f"[Blacklist] Could not send message blacklist log: {e}")

                logging.info(
                    f"[Blacklist] Deleted message from {author} (ID: {author.id}) "
                    f"in #{getattr(message.channel, 'name', '?')} containing '{keyword}'"
                )

                if not ows_get("blacklist_alert_only"):
                    if isinstance(author, discord.Member):
                        try:
                            await author.ban(
                                reason=f"Auto-banned: Blacklisted keyword '{keyword}' in message",
                            )
                        except discord.Forbidden:
                            logging.warning(f"[Blacklist] No permission to ban {author}.")
                        except discord.HTTPException as e:
                            logging.error(f"[Blacklist] HTTP error banning {author}: {e}")
                return


    try:
        if instance_handles('mod') and message.guild is not None and _fa_home:
            MessageLogSystem.cache(message)
    except Exception as exc:
        logging.debug(f"[MsgLog] on_message cache failed: {exc}")

    if instance_handles('ticket') and open_ticket is not None:
        is_staff_msg = message.author.id != open_ticket.get('creator_id')
        if is_staff_msg and ows_get("require_claim_before_reply"):
            # Exempt command messages (e.g. !claim, !close, !unclaim) so staff
            # can still MANAGE the ticket — the gate applies to conversational
            # replies only, matching the toggle's "Staff must claim before
            # responding" description.
            is_command = (message.content or '').startswith(config.command_prefix)
            if not is_command:
                claimed_by = open_ticket.get('claimed_by')
                if not claimed_by:
                    # Ticket is not claimed — staff must claim before replying.
                    try:
                        await message.delete()
                    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                        pass
                    try:
                        await message.channel.send(
                            f"{message.author.mention} You must claim this ticket before replying. "
                            f"Click the **Claim** button (or use `{config.command_prefix}claim`).",
                            delete_after=15,
                        )
                    except (discord.HTTPException, discord.Forbidden):
                        pass
                    return
                if claimed_by != message.author.id:
                    # Claimed by another staff member — only the claimer may reply.
                    try:
                        await message.delete()
                    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                        pass
                    try:
                        await message.channel.send(
                            f"{message.author.mention} This ticket is claimed by <@{claimed_by}>. "
                            f"Only the claimer may reply. Ask them to `{config.command_prefix}unclaim` for a hand-off.",
                            delete_after=15,
                        )
                    except (discord.HTTPException, discord.Forbidden):
                        pass
                    return
        if is_staff_msg:
            # Only the FIRST staff reply needs a write. The ticket dict in
            # hand already tells us whether first_response_at is set, so we
            # skip the thread hop + UPDATE + COMMIT for every subsequent
            # message (the conditional UPDATE remains as a second defense).
            if not open_ticket.get('first_response_at'):
                asyncio.create_task(asyncio.to_thread(
                    data_manager.update_ticket_first_response, open_ticket['ticket_id']
                ))

            # --- PREMIUM TIER 1: on_ticket_message hook ---
            # Records the first staff response in the SLA state row, sets
            # staff_responded_at on the ticket, and cancels any pending
            # 'no_response' automation timer for this ticket.
            if PREMIUM_AVAILABLE:
                try:
                    await TicketTool.wiring.on_ticket_message(
                        bot=bot, ticket_tool=ticket_tool,
                        message=message, ticket=open_ticket, is_staff=is_staff_msg,
                    )
                except Exception as exc:
                    logging.debug(f"[Premium] on_ticket_message failed: {exc}")

        # Persist this message to the ticket_messages table so it serves as a
        # transcript BACKUP. The transcript generator still reads Discord
        # channel history as the primary source, but falls back to this table
        # if the channel history is empty/unavailable (e.g. messages were
        # bulk-deleted, or the channel was partially lost before close).
        #
        # Fire-and-forget: `async_save_ticket_message` already runs the write
        # in a worker thread, but awaiting it here would serialise every
        # ticket message behind the previous write. Scheduling it as a task
        # lets multiple writes pipeline.
        try:
            payload = {
                'message_id': message.id,
                'ticket_id': open_ticket['ticket_id'],
                'author_id': message.author.id,
                'author_name': message.author.display_name,
                'author_avatar': str(message.author.avatar.url) if message.author.avatar else str(message.author.default_avatar.url),
                'content': message.content or '',
                'attachments': json.dumps([att.url for att in message.attachments]),
                'created_at': message.created_at.isoformat() if message.created_at else datetime.now(timezone.utc).isoformat(),
            }
            asyncio.create_task(data_manager.async_save_ticket_message(payload))
        except Exception as exc:
            logging.debug(f"[TicketMsg] could not schedule persistence for {message.id}: {exc}")

    # --- PREMIUM TIER 2: custom command prefix dispatch ---
    # If the message is a !-prefixed command that isn't a built-in, check if
    # it's a custom command. If it ran, we're done (skip multi-command parsing).
    if instance_handles('ticket') and PREMIUM_AVAILABLE and message.guild and message.content and _fa_tickets:
        try:
            content = message.content.strip()
            if content.startswith(config.command_prefix):
                # Extract the command name (first token after the prefix).
                rest = content[len(config.command_prefix):]
                if rest:
                    cmd_name = rest.split()[0].lower()
                    # Don't shadow built-in hybrid commands — let discord.py
                    # handle those. Only intercept if it's NOT a known command.
                    if not bot.get_command(cmd_name):
                        ran = await TicketTool.wiring.on_prefix_command(
                            bot=bot, message=message, command_name=cmd_name,
                        )
                        if ran:
                            return  # custom command handled it
        except Exception as exc:
            logging.debug(f"[Premium] custom command dispatch failed: {exc}")

    await process_potential_multi_command(message)


# --- OWNER BRANDING AND CHANNEL SETUP COMMANDS ---
@bot.command(name="abrev", aliases=["Abrev", "ABREV"])
@commands.is_owner()
async def abbrev_cmd(ctx: commands.Context, abbreviation: str) -> None:
    value = abbreviation.strip()
    if not value:
        await ctx.send("Usage: `!abrev BTD`")
        return

    config.gang_abbreviation = value.upper()
    config.save_branding_settings()
    await ctx.send(embed=EmbedBuilder.success("Gang Abbreviation Updated", f"Abbreviation set to **{config.gang_abbreviation}**"))


@bot.command(name="setchannel")
@commands.is_owner()
async def setchannel_cmd(ctx: commands.Context, channel_type: str, channel: discord.TextChannel) -> None:
    valid_types = {
        "welcome": "welcome",
        "rules": "rules",
        "verification": "verification_main",
        "verify": "verification_main",
        "reports": "reports",
        "tickets": "tickets",
        "log": "log",
        "logs": "log",
        "auto_scan": "auto_scan",
    }
    key = valid_types.get(channel_type.lower())
    if not key:
        await ctx.send("Usage: `!setchannel <welcome|rules|verification|reports|tickets|log> #channel`")
        return

    setattr(config.channels, key, channel.id)
    config.save_channel_settings()
    await ctx.send(embed=EmbedBuilder.success("Channel Updated", f"Set **{channel_type.lower()}** to {channel.mention}"))


# ===========================================================================
# GENERIC INTERACTIVE SETUP FRAMEWORK
# ===========================================================================
# One reusable engine powering four commands:
#   !channelsetup  -> ChannelConfig   (manage_channels)
#   !rolesetup      -> RoleConfig      (manage_roles)
#   !timingsetup    -> TimingConfig    (manage_guild)
#   !limitssetup    -> LimitsConfig    (manage_guild)
#
# Each config field is described by a SetupSlot. The UI is identical for every
# area: a dropdown lists the slots (step 1); picking one opens either a
# paginated picker (channels / roles / servers) or a number-input modal
# (timing / limits). All values persist to the bot_config SQLite table via
# the Config.save_*_settings() methods and reload on startup via the matching
# load_*_settings() calls in setup_hook().
# ===========================================================================

# --- Slot "kinds" ---
KIND_CHANNEL = "channel"
KIND_ROLE    = "role"
KIND_SERVER  = "server"
KIND_INTEGER = "integer"


@dataclass
class SetupSlot:
    """Describes one configurable field on a Config sub-object.

    target        -> "channels" | "roles" | "servers" | "timing" | "limits"
    attr          -> attribute name on the sub-config (e.g. "welcome", "member")
    label         -> human-readable label shown in the dropdown + embed
    kind          -> one of KIND_CHANNEL / KIND_ROLE / KIND_SERVER / KIND_INTEGER
    channel_types -> (KIND_CHANNEL only) which discord.ChannelType values to list
    guild_source  -> (KIND_CHANNEL only) "current" | "gang" | "server" — which
                     guild's channels to list when assigning
    min_val       -> (KIND_INTEGER only) inclusive lower bound
    max_val       -> (KIND_INTEGER only) inclusive upper bound
    unit          -> (KIND_INTEGER only) suffix shown after the value
                     ("minutes", "seconds", "days", ...)
    restart_note  -> optional note shown in the confirmation message when a
                     restart is required for the change to take full effect
    """
    target: str
    attr: str
    label: str
    kind: str
    channel_types: Tuple[discord.ChannelType, ...] = ()
    guild_source: str = "current"
    min_val: int = 0
    max_val: int = 2_147_483_647
    unit: str = ""
    restart_note: str = ""


# --- Slot definitions per config area ---------------------------------------

CHANNEL_SETUP_SLOTS: List[SetupSlot] = [
    SetupSlot("channels", "invite", "Invite Tracker Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "welcome", "Welcome / New-Member Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "rules", "Server Rules Display Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "log", "Mod Actions & Broadcasts Log Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "auto_scan", "Auto-Blacklist Scan Log Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "verification_main", "Verification Panel Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "reports", "User Reports Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
    SetupSlot("channels", "tickets", "Tickets Category (Category, NOT a Channel)", KIND_CHANNEL,
              channel_types=(discord.ChannelType.category,), guild_source="current"),
    SetupSlot("channels", "transcripts", "Ticket Transcripts Archive Channel", KIND_CHANNEL,
              channel_types=(discord.ChannelType.text,), guild_source="current"),
]

ROLE_SETUP_SLOTS: List[SetupSlot] = [
    SetupSlot("roles", "invite_manager", "Invite Manager Role", KIND_ROLE),
    SetupSlot("roles", "member", "Member Role", KIND_ROLE),
    SetupSlot("roles", "staff", "Staff / Moderator Role", KIND_ROLE),
    SetupSlot("roles", "verified", "Verified Role", KIND_ROLE),
    SetupSlot("roles", "verification_ping", "Verification Ping Role", KIND_ROLE),
    SetupSlot("roles", "muted", "Muted Role", KIND_ROLE),
    SetupSlot("roles", "ticket_support", "Ticket Support Role", KIND_ROLE),
    SetupSlot("roles", "server_tester", "Server Tester Role", KIND_ROLE),
]

TIMING_SETUP_SLOTS: List[SetupSlot] = [
    SetupSlot("timing", "auto_scan_interval_hours", "Auto-Blacklist Scan Interval", KIND_INTEGER,
              min_val=1, max_val=168, unit="hours",
              restart_note="Loop intervals apply on next bot restart."),
    SetupSlot("timing", "report_message_interval_minutes", "Periodic Broadcast Interval", KIND_INTEGER,
              min_val=1, max_val=1440, unit="minutes",
              restart_note="Loop intervals apply on next bot restart."),
    SetupSlot("timing", "report_timeout_seconds", "Report Submission Timeout", KIND_INTEGER,
              min_val=60, max_val=600, unit="seconds"),
]

LIMITS_SETUP_SLOTS: List[SetupSlot] = [
    SetupSlot("limits", "min_account_age_days", "Min Account Age (Verification)", KIND_INTEGER,
              min_val=0, max_val=365, unit="days"),
    SetupSlot("limits", "min_blacklist_keyword_length", "Min Blacklist Keyword Length", KIND_INTEGER,
              min_val=1, max_val=50, unit="characters"),
    SetupSlot("limits", "min_poll_options", "Min Poll Options", KIND_INTEGER,
              min_val=2, max_val=10, unit="options"),
    SetupSlot("limits", "max_poll_options", "Max Poll Options", KIND_INTEGER,
              min_val=2, max_val=10, unit="options"),
    SetupSlot("limits", "min_poll_duration", "Min Poll Duration", KIND_INTEGER,
              min_val=10, max_val=86400, unit="seconds"),
    SetupSlot("limits", "max_poll_duration", "Max Poll Duration", KIND_INTEGER,
              min_val=60, max_val=604800, unit="seconds"),
    SetupSlot("limits", "max_warnings_before_ban", "Max Warnings Before Ban", KIND_INTEGER,
              min_val=1, max_val=50, unit="warnings"),
    SetupSlot("limits", "max_tickets_per_user", "Max Tickets Per User", KIND_INTEGER,
              min_val=1, max_val=50, unit="tickets"),
]

# Map each setup area to (title, permission-label) for the main embed.
_SETUP_AREA_META: Dict[str, Tuple[str, str]] = {
    "channels": ("🔧 Channel Setup",       "manage_channels"),
    "roles":    ("👥 Role Setup",          "manage_roles"),
    "timing":   ("⏱️ Timing Setup",        "manage_guild"),
    "limits":   ("📊 Limits Setup",        "manage_guild"),
}


# --- Helpers ---------------------------------------------------------------

def _setup_target(area: str):
    """Return the live Config sub-object for an area (config.channels, ...)."""
    if area == "channels":
        return config.channels
    if area == "roles":
        return config.roles
    if area == "timing":
        return config.timing
    if area == "limits":
        return config.limits
    raise ValueError(f"Unknown setup area: {area}")


def _setup_save(area: str) -> None:
    """Persist the sub-config for an area to its bot_config key."""
    if area == "channels":
        config.save_channel_settings()
    elif area == "roles":
        config.save_role_settings()
    elif area == "timing":
        config.save_timing_settings()
    elif area == "limits":
        config.save_limits_settings()


def _setup_get_value(slot: SetupSlot) -> int:
    """Return the current int value held by a slot."""
    return int(getattr(_setup_target(slot.target), slot.attr, 0))


def _setup_set_value(slot: SetupSlot, value: int) -> None:
    """Write a new int value to a slot and persist it immediately."""
    setattr(_setup_target(slot.target), slot.attr, value)
    _setup_save(slot.target)


def _setup_guild_for(slot: SetupSlot, ctx_guild: Optional[discord.Guild]) -> Optional[discord.Guild]:
    """Resolve which guild's channels/roles to list for a slot.

    For KIND_CHANNEL the slot's guild_source drives the choice (current /
    gang / server). For KIND_ROLE the current guild is always used. Other
    kinds don't use a guild.
    """
    if slot.kind != KIND_CHANNEL:
        return ctx_guild
    if slot.guild_source == "current":
        return ctx_guild
    if slot.guild_source in ("gang", "server"):
        # This Config has no `servers` block (single-guild build); all live
        # slots use guild_source="current". Guarded so a future slot using
        # one of these sources degrades to "no guild" instead of crashing.
        servers = getattr(config, "servers", None)
        if servers is None:
            return None
        wanted = servers.gang_server_id if slot.guild_source == "gang" else servers.server_server_id
        return bot.get_guild(wanted)
    return ctx_guild


def _setup_display_value(slot: SetupSlot, ctx_guild: Optional[discord.Guild]) -> str:
    """Markdown display string for a slot's current value, WITH validation.

    - 0 / missing                 -> `Not set`
    - non-zero, resolves          -> clickable mention / formatted value
    - non-zero, unresolvable      -> `Not set` *(stale ID `cid`)*
    """
    val = _setup_get_value(slot)

    if slot.kind == KIND_CHANNEL:
        if not val:
            return "`Not set`"
        target_guild = _setup_guild_for(slot, ctx_guild)
        if target_guild is not None and target_guild.get_channel(val) is None:
            return f"`Not set` *(stale ID `{val}`)*"
        return f"<#{val}>"

    if slot.kind == KIND_ROLE:
        if not val:
            return "`Not set`"
        if ctx_guild is not None:
            if ctx_guild.get_role(val) is None:
                return f"`Not set` *(stale ID `{val}`)*"
        return f"<@&{val}>"

    if slot.kind == KIND_SERVER:
        if not val:
            return "`Not set`"
        g = bot.get_guild(val)
        if g is None:
            return f"`Not set` *(stale ID `{val}`)*"
        return f"**{g.name}** (`{val}`)"

    if slot.kind == KIND_INTEGER:
        unit = f" {slot.unit}" if slot.unit else ""
        return f"**{val}**{unit}"

    return "`Not set`"


def _setup_current_brief(slot: SetupSlot, ctx_guild: Optional[discord.Guild]) -> str:
    """Plain-text summary for select-option descriptions (no markdown)."""
    val = _setup_get_value(slot)

    if slot.kind == KIND_CHANNEL:
        if not val:
            return "Not set"
        target_guild = _setup_guild_for(slot, ctx_guild)
        if target_guild is not None:
            ch = target_guild.get_channel(val)
            if ch is not None:
                return f"Current: #{ch.name}"
            return f"Current: {val} (stale)"
        return f"Current: {val}"

    if slot.kind == KIND_ROLE:
        if not val:
            return "Not set"
        if ctx_guild is not None:
            role = ctx_guild.get_role(val)
            if role is not None:
                return f"Current: @{role.name}"
            return f"Current: {val} (stale)"
        return f"Current: {val}"

    if slot.kind == KIND_SERVER:
        if not val:
            return "Not set"
        g = bot.get_guild(val)
        if g is not None:
            return f"Current: {g.name}"
        return f"Current: {val} (stale)"

    if slot.kind == KIND_INTEGER:
        unit = f" {slot.unit}" if slot.unit else ""
        return f"Current: {val}{unit}"

    return "Not set"


def _setup_main_embed(area: str, slots: List[SetupSlot],
                      ctx_guild: Optional[discord.Guild]) -> discord.Embed:
    """Build the main setup embed showing every slot's current value."""
    title, perm = _SETUP_AREA_META[area]
    embed = discord.Embed(
        title=title,
        description=(
            "Use the dropdown below to configure each setting.\n"
            "1) Pick a setting to configure.\n"
            "2) Choose the value to assign (or type a number for timing/limits).\n"
            "Settings are saved automatically to the database."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    lines = [f"**{s.label}:** {_setup_display_value(s, ctx_guild)}" for s in slots]
    embed.add_field(name="Current Values", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Requires {perm} • Only the starter can interact.")
    return embed


# --- Views -----------------------------------------------------------------

class SetupMainView(View):
    """Step 1: dropdown listing every configurable slot for one config area.

    Picking a slot dispatches to either SetupAssignView (channels / roles /
    servers) or SetupIntegerModal (timing / limits).
    """

    def __init__(self, ctx: commands.Context, area: str, slots: List[SetupSlot]):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.area = area
        self.slots = slots
        self.author_id = ctx.author.id
        self.message: Optional[discord.Message] = None

        options = []
        for slot in slots:
            current_str = _setup_current_brief(slot, ctx.guild)
            options.append(discord.SelectOption(
                label=slot.label[:100],
                value=slot.attr,
                description=current_str[:100],
            ))

        self.slot_select = discord.ui.Select(
            placeholder="Select a setting to configure…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.slot_select.callback = self._on_slot_selected
        self.add_item(self.slot_select)

    def _refresh_main_embed(self) -> discord.Embed:
        return _setup_main_embed(self.area, self.slots, self.ctx.guild)

    def _build_items(self, slot: SetupSlot) -> Tuple[Optional[List[Any]], str, Optional[discord.Guild]]:
        """Build the pickable item list for a channel/role/server slot.

        Returns (items, guild_name, validation_guild). items is None when the
        target guild can't be resolved.
        """
        if slot.kind == KIND_CHANNEL:
            guild = _setup_guild_for(slot, self.ctx.guild)
            if guild is None:
                return None, "", None
            items = sorted(
                [c for c in guild.channels if c.type in slot.channel_types],
                key=lambda c: c.name.lower(),
            )
            return items, guild.name, guild

        if slot.kind == KIND_ROLE:
            guild = self.ctx.guild
            if guild is None:
                return None, "", None
            # Exclude @everyone, bot-managed, and integration roles — they
            # can't/shouldn't be assigned by hand.
            items = sorted(
                [r for r in guild.roles
                 if not r.is_bot_managed() and not r.is_integration() and r != guild.default_role],
                key=lambda r: r.position,
                reverse=True,
            )
            return items, guild.name, guild

        if slot.kind == KIND_SERVER:
            items = sorted(bot.guilds, key=lambda g: g.name.lower())
            return items, "All Servers", None

        return None, "", None

    async def _on_slot_selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return

        attr = self.slot_select.values[0]
        slot = next((s for s in self.slots if s.attr == attr), None)
        if not slot:
            await interaction.response.send_message("Invalid selection.", ephemeral=True)
            return

        # Integer slots open a modal instead of a picker.
        if slot.kind == KIND_INTEGER:
            modal = SetupIntegerModal(slot, self)
            await interaction.response.send_modal(modal)
            return

        # Channel / role / server -> paginated picker.
        items, guild_name, validation_guild = self._build_items(slot)
        if items is None:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Guild Not Found", "Could not find the target guild for this slot."),
                ephemeral=True,
            )
            return
        if not items:
            kind_word = {"channel": "channels", "role": "roles", "server": "servers"}.get(slot.kind, "items")
            await interaction.response.send_message(
                embed=EmbedBuilder.error("None Found", f"No matching {kind_word} found for **{slot.label}**."),
                ephemeral=True,
            )
            return

        assign_view = SetupAssignView(slot, self, items, guild_name, validation_guild)
        await interaction.response.edit_message(embed=assign_view.render_embed(), view=assign_view)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        try:
            if getattr(self, "message", None) is not None:
                await self.message.edit(view=self)
        except Exception:
            pass


class SetupAssignView(View):
    """Step 2: paginated dropdown of real Discord channels/roles/servers to
    assign to the chosen slot.

    Discord caps each select menu at 25 options, so this view pages through the
    full list using Prev / Next buttons. The select is rebuilt in place on
    each page navigation.
    """

    PAGE_SIZE = 25  # Discord hard limit for select options

    def __init__(self, slot: SetupSlot, main_view: SetupMainView,
                 items: List[Any], guild_name: str,
                 validation_guild: Optional[discord.Guild] = None):
        super().__init__(timeout=300)
        self.author_id = main_view.author_id
        self.slot = slot
        self.main_view = main_view
        self.guild_name = guild_name
        self.validation_guild = validation_guild
        self.all_items = items
        self.total_pages = max(1, (len(items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = 0

        self.item_select = discord.ui.Select(
            placeholder=f"Choose a value for: {slot.label}"[:150],
            min_values=1,
            max_values=1,
            options=self._page_options(),
        )
        self.item_select.callback = self._on_item_selected
        self.add_item(self.item_select)

        self.prev_btn = Button(label="⬅ Prev", style=discord.ButtonStyle.secondary)
        self.prev_btn.callback = self._on_prev
        self.add_item(self.prev_btn)

        self.next_btn = Button(label="Next ➡", style=discord.ButtonStyle.secondary)
        self.next_btn.callback = self._on_next
        self.add_item(self.next_btn)

        self.back_btn = Button(label="⬅ Back", style=discord.ButtonStyle.secondary)
        self.back_btn.callback = self._on_back
        self.add_item(self.back_btn)

        self.clear_btn = Button(label="Clear", style=discord.ButtonStyle.danger)
        self.clear_btn.callback = self._on_clear
        self.add_item(self.clear_btn)

        self._sync_nav_state()

    # --- pagination helpers ---
    def _page_options(self) -> List[discord.SelectOption]:
        start = self.page * self.PAGE_SIZE
        page_items = self.all_items[start:start + self.PAGE_SIZE]
        options: List[discord.SelectOption] = []
        for it in page_items:
            if self.slot.kind == KIND_CHANNEL:
                label = f"#{it.name}"
            else:
                label = it.name  # role or guild name
            options.append(discord.SelectOption(
                label=label[:100],
                value=str(it.id),
                description=f"ID: {it.id}"[:100],
            ))
        return options

    def _sync_nav_state(self) -> None:
        multi = self.total_pages > 1
        self.prev_btn.disabled = (not multi) or self.page <= 0
        self.next_btn.disabled = (not multi) or self.page >= self.total_pages - 1

    def render_embed(self) -> discord.Embed:
        current_str = _setup_display_value(self.slot, self.validation_guild)
        kind_word = {"channel": "channels", "role": "roles", "server": "servers"}.get(self.slot.kind, "items")
        embed = discord.Embed(
            title=f"🔧 Configure: {self.slot.label}",
            description=(
                f"Choose the value to assign to **{self.slot.label}**.\n"
                f"**Source:** {self.guild_name}\n"
                f"**Current value:** {current_str}\n"
                f"**Available {kind_word}:** {len(self.all_items)}"
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        if self.total_pages > 1:
            start = self.page * self.PAGE_SIZE
            end = min(start + self.PAGE_SIZE, len(self.all_items))
            embed.set_footer(
                text=f"Page {self.page + 1}/{self.total_pages} • {start + 1}–{end} of {len(self.all_items)} (A–Z) • Use Prev/Next"
            )
        return embed

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return
        if self.page <= 0:
            await interaction.response.defer()
            return
        self.page -= 1
        self.item_select.options = self._page_options()
        self._sync_nav_state()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return
        if self.page >= self.total_pages - 1:
            await interaction.response.defer()
            return
        self.page += 1
        self.item_select.options = self._page_options()
        self._sync_nav_state()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=self.main_view._refresh_main_embed(), view=self.main_view)

    async def _on_clear(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return
        _setup_set_value(self.slot, 0)
        await interaction.response.edit_message(embed=self.main_view._refresh_main_embed(), view=self.main_view)
        await interaction.followup.send(
            embed=EmbedBuilder.success("Setting Cleared", f"**{self.slot.label}** has been cleared."),
            ephemeral=True,
        )

    async def _on_item_selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return
        value_id = int(self.item_select.values[0])
        _setup_set_value(self.slot, value_id)

        # Build a friendly confirmation string for the chosen value.
        if self.slot.kind == KIND_CHANNEL:
            display = f"<#{value_id}> (`{value_id}`)"
        elif self.slot.kind == KIND_ROLE:
            display = f"<@&{value_id}> (`{value_id}`)"
        else:  # KIND_SERVER
            g = bot.get_guild(value_id)
            display = f"**{g.name}** (`{value_id}`)" if g else f"`{value_id}`"

        await interaction.response.edit_message(embed=self.main_view._refresh_main_embed(), view=self.main_view)
        await interaction.followup.send(
            embed=EmbedBuilder.success("Setting Updated", f"**{self.slot.label}** set to {display}."),
            ephemeral=True,
        )


class SetupIntegerModal(Modal):
    """Number-input modal for KIND_INTEGER slots (timing / limits).

    Opened via interaction.response.send_modal(). On submit it validates the
    value, writes it, then returns the user to the main setup view with a
    refreshed embed showing the new value.
    """

    def __init__(self, slot: SetupSlot, main_view: SetupMainView):
        super().__init__(title=f"Set: {slot.label}"[:45], timeout=300)
        self.slot = slot
        self.main_view = main_view

        current = _setup_get_value(slot)
        unit_hint = f" ({slot.unit})" if slot.unit else ""
        self.input = TextInput(
            label=slot.label[:45],
            placeholder=f"Enter a whole number{unit_hint} ({slot.min_val}–{slot.max_val})",
            default_value=str(current) if current else "",
            required=True,
            max_length=12,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.main_view.author_id:
            await interaction.response.send_message("This setup menu is not for you.", ephemeral=True)
            return

        raw = self.input.value.strip()
        try:
            val = int(raw)
        except ValueError:
            await interaction.response.send_message(
                embed=EmbedBuilder.error("Invalid Number", f"`{raw}` is not a valid whole number."),
                ephemeral=True,
            )
            return

        if val < self.slot.min_val or val > self.slot.max_val:
            await interaction.response.send_message(
                embed=EmbedBuilder.error(
                    "Out of Range",
                    f"Value must be between **{self.slot.min_val}** and **{self.slot.max_val}**.",
                ),
                ephemeral=True,
            )
            return

        _setup_set_value(self.slot, val)

        # If the slot belongs to the `timing` config, push the new value
        # into the running loops immediately — no restart required.
        if self.slot.target == "timing":
            try:
                config.apply_timing_to_loops()
            except Exception as exc:
                logging.debug(f"[Setup] Could not apply timing to loops: {exc}")

        # Return to the main view with a refreshed embed.
        await interaction.response.edit_message(
            embed=self.main_view._refresh_main_embed(),
            view=self.main_view,
        )

        unit = f" {self.slot.unit}" if self.slot.unit else ""
        note = f"\n\nℹ️ {self.slot.restart_note}" if self.slot.restart_note else ""
        await interaction.followup.send(
            embed=EmbedBuilder.success(
                "Setting Updated",
                f"**{self.slot.label}** set to **{val}**{unit}.{note}",
            ),
            ephemeral=True,
        )


# ===========================================================================
# SETUP COMMANDS — one per config area
# All use the same SetupMainView engine; only the slot list + permission differ.
# ===========================================================================

@bot.command(name="channelsetup",
                    aliases=["ChannelSetup", "CHANNELSETUP"])
# NOTE: the legacy "csetup"/"CSetup"/"CSETUP" aliases were removed — the
# `!csetup` name now belongs to the guided setup panel in modules/administration/setup.py
# (SetupCog). Task 8 loads that module via load_extension("modules.administration.setup").
@commands.has_permissions(manage_channels=True)
@commands.guild_only()
async def channelsetup_cmd(ctx: commands.Context) -> None:
    """Interactive channel setup via dropdown menus."""
    view = SetupMainView(ctx, "channels", CHANNEL_SETUP_SLOTS)
    view.message = await ctx.send(embed=_setup_main_embed("channels", CHANNEL_SETUP_SLOTS, ctx.guild), view=view)


@bot.command(name="rolesetup",
                    aliases=["RoleSetup", "ROLESETUP", "rsetup", "RSetup", "RSETUP"])
@commands.has_permissions(manage_roles=True)
@commands.guild_only()
async def rolesetup_cmd(ctx: commands.Context) -> None:
    """Interactive role setup via dropdown menus."""
    view = SetupMainView(ctx, "roles", ROLE_SETUP_SLOTS)
    view.message = await ctx.send(embed=_setup_main_embed("roles", ROLE_SETUP_SLOTS, ctx.guild), view=view)


@bot.command(name="timingsetup",
                    aliases=["TimingSetup", "TIMINGSETUP", "tsetup", "TSetup", "TSETUP"])
@commands.has_permissions(manage_guild=True)
@commands.guild_only()
async def timingsetup_cmd(ctx: commands.Context) -> None:
    """Interactive timing setup via dropdown menus + number input."""
    view = SetupMainView(ctx, "timing", TIMING_SETUP_SLOTS)
    view.message = await ctx.send(embed=_setup_main_embed("timing", TIMING_SETUP_SLOTS, ctx.guild), view=view)


@bot.command(name="limitssetup",
                    aliases=["LimitsSetup", "LIMITSSETUP", "lsetup", "LSetup", "LSETUP"])
@commands.has_permissions(manage_guild=True)
@commands.guild_only()
async def limitssetup_cmd(ctx: commands.Context) -> None:
    """Interactive limits setup via dropdown menus + number input."""
    view = SetupMainView(ctx, "limits", LIMITS_SETUP_SLOTS)
    view.message = await ctx.send(embed=_setup_main_embed("limits", LIMITS_SETUP_SLOTS, ctx.guild), view=view)


# --- MODERATION COMMANDS ---
@bot.command()
@commands.has_permissions(kick_members=True)
@app_commands.describe(member="Member to kick", reason="Reason for kick")
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided") -> None:
    """Kick a member. Usage: !kick @user [reason]"""
    members = [member]
    kicked_list = []
    failed_list = []

    for m in members:
        if m.top_role >= ctx.guild.me.top_role or m.id == ctx.guild.owner_id:
            failed_list.append(f"{m.mention} (Hierarchy/Owner)")
            continue
        try:
            await m.kick(reason=f"Kicked by {ctx.author}: {reason}")
            kicked_list.append(m.mention)
        except discord.Forbidden:
            failed_list.append(f"{m.mention} (Missing Perms)")
        except discord.HTTPException:
            failed_list.append(f"{m.mention} (API Error)")

    embed = EmbedBuilder.success("Member Kicked", "")
    if kicked_list:
        embed.add_field(name="✅ Successfully Kicked", value="\n".join(kicked_list), inline=False)
    if failed_list:
        embed.add_field(name="❌ Failed to Kick", value="\n".join(failed_list), inline=False)
    
    embed.description = f"**Reason:** {reason}"
    await ctx.send(embed=embed)
    logging.info(f'User(s) {kicked_list} were kicked by {ctx.author} for: {reason}')


@bot.command()
@commands.has_permissions(ban_members=True)
@app_commands.describe(member="Member to ban", reason="Reason for ban")
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided") -> None:
    """Ban a member. Usage: !ban @user [reason]"""
    members = [member]
    banned_list = []
    failed_list = []

    for m in members:
        if m.top_role >= ctx.guild.me.top_role or m.id == ctx.guild.owner_id:
            failed_list.append(f"{m.mention} (Hierarchy/Owner)")
            continue
        try:
            await m.ban(reason=f"Banned by {ctx.author}: {reason}", delete_message_days=0)
            banned_list.append(m.mention)
        except discord.Forbidden:
            failed_list.append(f"{m.mention} (Missing Perms)")
        except discord.HTTPException:
            failed_list.append(f"{m.mention} (API Error)")

    embed = EmbedBuilder.success("Member Banned", "")
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
    global blacklisted_keywords
    
    keyword = keyword.strip()
    if not keyword:
        await ctx.send("Please provide a keyword to blacklist.")
        return
    
    if len(keyword) < config.limits.min_blacklist_keyword_length:
        await ctx.send(f"Keyword must be at least {config.limits.min_blacklist_keyword_length} characters long.")
        return
    
    for existing in blacklisted_keywords:
        if existing.lower() == keyword.lower():
            await ctx.send(f"Keyword `{keyword}` is already blacklisted.")
            return
    
    blacklisted_keywords.add(keyword)
    save_blacklist_data()
    
    embed = EmbedBuilder.warning(
        "Keyword Blacklisted",
        f"Added `{keyword}` to blacklist.\nTotal keywords: {len(blacklisted_keywords)}\n\n"
        f"Any user with this keyword in their profile will be auto-banned."
    )
    
    await ctx.send(embed=embed)
    logging.info(f"[Blacklist] Keyword '{keyword}' added by {ctx.author}")


@bot.command(name="unblacklist", description="Remove a keyword from the blacklist")
@commands.has_permissions(administrator=True)
@app_commands.describe(keyword="The keyword to remove from the blacklist")
async def unblacklist_cmd(ctx: commands.Context, *, keyword: str) -> None:
    global blacklisted_keywords
    
    keyword = keyword.strip()
    found_keyword = next((k for k in blacklisted_keywords if k.lower() == keyword.lower()), None)
    
    if not found_keyword:
        await ctx.send(f"Keyword `{keyword}` is not in the blacklist.")
        return
    
    blacklisted_keywords.discard(found_keyword)
    save_blacklist_data()
    
    await ctx.send(embed=EmbedBuilder.success("Keyword Removed", f"Removed `{found_keyword}` from blacklist."))
    logging.info(f"[Blacklist] Keyword '{found_keyword}' removed by {ctx.author}")


@bot.command(name="blacklistscan", description="Scan all members for blacklisted keywords")
@commands.has_permissions(administrator=True)
async def blacklistscan_cmd(ctx: commands.Context) -> None:
    if not blacklisted_keywords:
        await ctx.send("No keywords are currently blacklisted. Use `!blacklist <keyword>` to add some.")
        return
    
    status_msg = await ctx.send(f"Scanning **{ctx.guild.member_count}** members for **{len(blacklisted_keywords)}** blacklisted keyword(s)...")
    
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
    if not blacklisted_keywords:
        await ctx.send("**No keywords are currently blacklisted.**")
        return
    
    keywords_list = list(blacklisted_keywords)
    embed = discord.Embed(title="Blacklisted Keywords", color=discord.Color.red())
    
    chunks = [keywords_list[i:i+20] for i in range(0, len(keywords_list), 20)]
    for i, chunk in enumerate(chunks[:5]):
        field_name = "Keywords" if i == 0 else "Keywords (continued)"
        embed.add_field(name=field_name, value="\n".join(f"- `{kw}`" for kw in chunk), inline=False)
    
    if len(chunks) > 5:
        embed.add_field(name="...", value=f"And {len(keywords_list) - 100} more keywords", inline=False)
    
    embed.set_footer(text=f"Total: {len(blacklisted_keywords)} keyword(s)")
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
        embed.add_field(name="Keywords Checked", value=str(len(blacklisted_keywords)), inline=True)
    
    await ctx.send(embed=embed)


class AutoPurgeManager:
    """Manages the verification-channel auto-purge lifecycle.

    One instance per verification channel. The manager is stateful:
      - last_activity_ts:   updated on every human message in the channel
      - warning_task:        a background asyncio task that waits 3 min, posts
                             the warning, waits 2 more min, then purges
      - warning_message_id:  the warning message's id (so we can delete it)
      - armed:               whether a purge cycle is currently in progress

    Activity in the channel cancels the current cycle (if armed) and
    re-evaluates. The re-evaluation happens via a 3-minute idle wait, so we
    don't hammer the channel.
    """

    THRESHOLD_MESSAGES = 12   # 12+ messages -> eligible for purge
    IDLE_BEFORE_WARNING = 180  # 3 minutes of inactivity before the warning
    WARNING_WINDOW = 120       # 2 minutes after the warning before deletion

    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        self.last_activity_ts: float = time.time()
        self.warning_task: Optional[asyncio.Task] = None
        self.warning_message_id: Optional[int] = None
        self.armed: bool = False
        self._lock = asyncio.Lock()

    async def record_activity(self) -> None:
        """Call on EVERY message in the verification channel.

        Cancels an in-progress warning cycle (if any), then re-arms a fresh
        one after the idle window — but only if the channel is over the
        message threshold. Cancelling on activity is what satisfies the
        "if it becomes active again during those 2 minutes, cancel the purge"
        requirement.
        """
        self.last_activity_ts = time.time()
        async with self._lock:
            await self._cancel_warning_task()
        # (Re)arm a fresh idle watcher.
        await self._schedule_idle_check()

    async def _schedule_idle_check(self) -> None:
        """Spawn (or replace) the idle-watcher background task."""
        # Cancel any existing watcher first — only one idle check at a time.
        if self.warning_task and not self.warning_task.done():
            self.warning_task.cancel()
            try:
                await self.warning_task
            except (asyncio.CancelledError, Exception):
                pass
        self.warning_task = asyncio.create_task(self._idle_watch_loop())

    async def _cancel_warning_task(self) -> None:
        """Cancel any running purge cycle and clean up the warning message."""
        self.armed = False
        if self.warning_task and not self.warning_task.done():
            self.warning_task.cancel()
            try:
                await self.warning_task
            except (asyncio.CancelledError, Exception):
                pass
            self.warning_task = None
        # Delete the warning message so it doesn't linger / count toward
        # the next cycle's message count.
        if self.warning_message_id is not None:
            channel = bot.get_channel(self.channel_id)
            if channel is not None:
                try:
                    msg = await channel.fetch_message(self.warning_message_id)
                    await msg.delete()
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    pass
            self.warning_message_id = None

    async def _idle_watch_loop(self) -> None:
        """Wait IDLE_BEFORE_WARNING seconds; if still idle, arm the purge."""
        try:
            await asyncio.sleep(self.IDLE_BEFORE_WARNING)
        except asyncio.CancelledError:
            return  # Activity happened — cancelled, fresh cycle started elsewhere.
        # Did activity happen while we were sleeping? If so, bail; the new
        # cycle (started by record_activity) owns the next idle window.
        if time.time() - self.last_activity_ts < self.IDLE_BEFORE_WARNING:
            return
        # Threshold check: only purge if the channel has 12+ messages.
        channel = bot.get_channel(self.channel_id)
        if channel is None:
            return
        try:
            recent = [m async for m in channel.history(limit=self.THRESHOLD_MESSAGES)]
        except (discord.HTTPException, discord.Forbidden):
            return
        if len(recent) < self.THRESHOLD_MESSAGES:
            return  # Not enough messages yet — nothing to do.
        # Enough messages + idle -> post the warning and arm the purge.
        await self._post_warning_and_arm(channel)

    async def _post_warning_and_arm(self, channel: discord.TextChannel) -> None:
        """Post the warning embed and start the 2-minute countdown."""
        self.armed = True
        warning_embed = discord.Embed(
            title="⚠️ Auto-Purge Warning",
            description=(
                f"This channel has been inactive for **{self.IDLE_BEFORE_WARNING // 60} minutes** "
                f"with **{self.THRESHOLD_MESSAGES}+** messages.\n\n"
                f"The messages will be **automatically deleted in {self.WARNING_WINDOW // 60} minutes** "
                f"if the channel remains inactive.\n\n"
                f"Send a message to **cancel** the purge. Messages marked with "
                f"`!nopurge` are always preserved."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        warning_embed.set_footer(text="Verification Auto-Purge • Inactivity cleanup")
        try:
            warning_msg = await channel.send(embed=warning_embed)
            self.warning_message_id = warning_msg.id
        except (discord.HTTPException, discord.Forbidden):
            self.armed = False
            return
        logging.info(
            f"[AutoPurge] Warning posted in verification channel {channel.id} "
            f"({len(await self._count_deletable(channel))} deletable messages)"
        )
        # Wait the warning window. If activity arrives, record_activity()
        # cancels this task and we never reach the purge.
        try:
            await asyncio.sleep(self.WARNING_WINDOW)
        except asyncio.CancelledError:
            # Cancelled by new activity -> purge cancelled.
            return
        # If we got here without being cancelled, the channel stayed quiet.
        if not self.armed:
            return
        await self._execute_purge(channel)

    async def _count_deletable(self, channel: discord.TextChannel) -> List[discord.Message]:
        """Return the list of messages that WOULD be deleted (excludes no-purge + the warning)."""
        deletable: List[discord.Message] = []
        async for m in channel.history(limit=200):
            # Never delete no-purge messages.
            if data_manager.is_no_purge(m.id):
                continue
            # Never delete our own warning message via this path.
            if self.warning_message_id is not None and m.id == self.warning_message_id:
                continue
            deletable.append(m)
        return deletable

    async def _execute_purge(self, channel: discord.TextChannel) -> None:
        """Delete all non-protected messages in the channel."""
        deletable = await self._count_deletable(channel)
        if not deletable:
            # Nothing to delete (all protected). Still clean up the warning.
            await self._cancel_warning_task()
            return
        # Log to the message-log system first (bulk-delete doesn't fire on_delete).
        try:
            await MessageLogSystem.log_bulk_delete(
                deletable, bot.user, channel,
                reason="verification auto-purge (inactivity)",
            )
        except Exception as e:
            logging.warning(f"[AutoPurge] msglog sync failed (non-fatal): {e}")
        # Bulk-delete in chunks of 100 (Discord hard limit).
        deleted_count = 0
        for i in range(0, len(deletable), 100):
            batch = deletable[i:i + 100]
            try:
                await channel.delete_messages(batch)
                deleted_count += len(batch)
            except discord.HTTPException:
                # Fallback: delete one at a time (too old for bulk).
                for m in batch:
                    try:
                        await m.delete()
                        deleted_count += 1
                    except (discord.HTTPException, discord.NotFound):
                        pass
            except (discord.Forbidden, discord.NotFound):
                pass
            if i + 100 < len(deletable):
                await asyncio.sleep(1)  # be nice to the rate limiter
        logging.info(f"[AutoPurge] Deleted {deleted_count} messages from verification channel {channel.id}")
        # Clean up the warning message.
        await self._cancel_warning_task()
        self.armed = False
        # Post a brief completion notice (auto-deletes itself).
        try:
            done_embed = discord.Embed(
                title="🧞️ Auto-Purge Complete",
                description=f"Deleted **{deleted_count}** inactive messages. "
                            f"No-purge-protected messages were preserved.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            done_embed.set_footer(text="Verification Auto-Purge")
            done_msg = await channel.send(embed=done_embed, delete_after=15)
        except (discord.HTTPException, discord.Forbidden):
            pass


# Per-channel auto-purge managers (keyed by verification channel id).
# Bounded so a bot in many servers cannot accumulate managers for deleted
# channels indefinitely.
auto_purge_managers: "OrderedDict[int, AutoPurgeManager]" = OrderedDict()
MAX_AUTO_PURGE_MANAGERS = 500


def get_auto_purge_manager(channel_id: int) -> AutoPurgeManager:
    """Get (or create) the AutoPurgeManager for a verification channel.

    The returned manager is bumped to the most-recently-used position;
    the oldest entry is evicted when the pool exceeds MAX_AUTO_PURGE_MANAGERS.
    """
    mgr = auto_purge_managers.get(channel_id)
    if mgr is None:
        mgr = AutoPurgeManager(channel_id)
        auto_purge_managers[channel_id] = mgr
    auto_purge_managers.move_to_end(channel_id)
    while len(auto_purge_managers) > MAX_AUTO_PURGE_MANAGERS:
        auto_purge_managers.popitem(last=False)
    return mgr


def is_verification_channel(channel_id: int, channel=None) -> bool:
    """True if the given channel is the verification channel.

    Checks in this order:
      1. Exact match against config.channels.verification_main (if set/nonzero).
      2. Name-based fallback: the channel's cleaned name is "verify" or
         "verification" (after stripping emoji/decorations).

    The name fallback is STRICT — it only matches channels whose name IS
    "verify"/"verification" (after cleaning), NOT sub-channels like
    "verification-responses" or "verification-help". This prevents the
    auto-purge from arming in the wrong channel.

    Pass `channel=` (the discord channel object) when available to avoid a
    bot.get_channel() cache lookup that can return None if the channel isn't
    cached yet (which was causing !nopurge to be rejected in the verification
    channel even when the user was standing in it).
    """
    # 1. Config-based exact match.
    if channel_id == config.channels.verification_main:
        return True
    # 2. Name-based fallback: use the provided channel object, or look it up.
    ch = channel if channel is not None else bot.get_channel(channel_id)
    if ch is not None and hasattr(ch, 'name') and ch.name:
        # Strip everything except a-z0-9 so emoji/decorations are removed.
        # "✅・Verification" -> "verification" -> matches.
        # "verification-responses" -> "verificationresponses" -> does NOT match.
        cleaned = re.sub(r'[^a-z0-9]', '', ch.name.lower())
        return cleaned in ('verify', 'verification')
    return False


@bot.command(name="nopurge", description="Protect message(s) from auto-purge/purge/purgeall (verification channel only)")
@commands.has_permissions(manage_messages=True)
@app_commands.describe(message_ids="One or more message IDs, separated by commas or spaces (right-click message -> Copy Message ID)")
async def nopurge_cmd(ctx: commands.Context, *, message_ids: str) -> None:
    """Mark one or more messages as permanently excluded from all purge systems.

    - Works ONLY in the verification channel.
    - Supports MULTIPLE IDs, separated by commas and/or spaces:
        !nopurge 123456789, 987654321, 111222333
        !nopurge 123456789 987654321 111222333
        !nopurge 123456789,987654321
    - Each valid message ID is persisted to SQLite, so it survives restarts.
    - Protected messages are never deleted by the auto-purge system, !purge,
    or !purgeall — all three check the same exclusion set.
    """
    # Enforce verification-channel-only usage.
    if not is_verification_channel(ctx.channel.id, ctx.channel):
        await ctx.send(
            embed=EmbedBuilder.warning(
                "Verification Channel Only",
                "`!nopurge` can only be used in the verification channel.",
            ),
            delete_after=15,
        )
        return

    # Parse the message IDs. Accept commas, spaces, or newlines as separators.
    # This handles all of: "123, 456, 789", "123 456 789", "123,456,789".
    raw_tokens = re.split(r'[,\s]+', message_ids.strip())
    parsed_ids: List[int] = []
    invalid_tokens: List[str] = []
    for token in raw_tokens:
        token = token.strip()
        if not token:
            continue
        try:
            parsed_ids.append(int(token))
        except ValueError:
            invalid_tokens.append(token)

    if not parsed_ids:
        await ctx.send(
            embed=EmbedBuilder.error(
                "No Valid Message IDs",
                "Please provide at least one valid message ID.\n"
                "**Usage:** `!nopurge 123456789` or `!nopurge 123, 456, 789`",
            ),
            delete_after=15,
        )
        return

    # Deduplicate while preserving order.
    seen: Set[int] = set()
    unique_ids: List[int] = []
    for mid in parsed_ids:
        if mid not in seen:
            seen.add(mid)
            unique_ids.append(mid)

    guild_id = ctx.guild.id if ctx.guild else 0

    # Process each message ID: validate it exists in this channel, then persist.
    protected: List[int] = []
    already_protected: List[int] = []
    not_found: List[int] = []
    errors: List[str] = []

    for msg_id in unique_ids:
        # Skip if already protected (don't waste API calls).
        if data_manager.is_no_purge(msg_id):
            already_protected.append(msg_id)
            continue
        # Validate the message actually exists in this channel.
        try:
            target = await ctx.channel.fetch_message(msg_id)
        except discord.NotFound:
            not_found.append(msg_id)
            continue
        except (discord.Forbidden, discord.HTTPException) as exc:
            errors.append(f"`{msg_id}`: {exc}")
            continue
        # Persist to SQLite + update the in-memory cache.
        data_manager.save_no_purge_message(msg_id, ctx.channel.id, guild_id, ctx.author.id)
        protected.append(msg_id)
        logging.info(f"[NoPurge] {ctx.author} protected message {msg_id} in verification channel {ctx.channel.id}")

    # Build the result embed.
    status_lines: List[str] = []
    if protected:
        status_lines.append(f"✅ **Protected ({len(protected)}):** {', '.join(f'`{m}`' for m in protected)}")
    if already_protected:
        status_lines.append(f"⏳ **Already protected ({len(already_protected)}):** {', '.join(f'`{m}`' for m in already_protected)}")
    if not_found:
        status_lines.append(f"❌ **Not found ({len(not_found)}):** {', '.join(f'`{m}`' for m in not_found)}")
    if invalid_tokens:
        status_lines.append(f"⚠️ **Invalid IDs:** {', '.join(f'`{t}`' for t in invalid_tokens)}")
    if errors:
        status_lines.append(f"⚠️ **Errors:** {len(errors)} message(s) could not be fetched")

    if not protected and not already_protected:
        # Nothing was protected — report the errors.
        embed = EmbedBuilder.error(
            "No Messages Protected",
            "\n".join(status_lines) or "No valid message IDs were found.",
        )
        await ctx.send(embed=embed, delete_after=30)
        return

    embed = discord.Embed(
        title="🛡️ Messages Protected",
        description=(
            "\n".join(status_lines) +
            f"\n\nProtected messages are excluded from:\n"
            f"• The verification **auto-purge** system\n"
            f"• `!purge`\n"
            f"• `!purgeall`\n\n"
            f"This protection is **permanent** and survives bot restarts."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Protected by {ctx.author.display_name}")
    await ctx.send(embed=embed)


# --- PURGE / CHANNEL COMMANDS ---
@bot.command(name="purge", aliases=["purgeall"], description="Purge a specific amount of messages or the entire channel")
@commands.has_permissions(manage_messages=True)
@app_commands.describe(
    amount="Number of messages to delete. Leave empty to delete the whole channel.",
    flags="Optional flags. Use '-del' to also delete the !purge command message and skip the confirmation reply.",
)
async def purge_cmd(
    ctx: commands.Context,
    amount: Optional[int] = None,
    *,
    flags: str = "",
) -> None:
    """Purges a specific amount of messages, or the entire channel if no amount is given.

    NEW FLAGS / FIXES:
      - `-del` flag (e.g. `!purge 2 -del`):
          * Also deletes the `!purge` command message itself after purging.
          * Skips the "Deleted N messages." confirmation reply entirely.
          * The cleanup-command prompt (for chained commands) is also skipped,
            since the command message is gone.
      - Message-log sync:
          * discord.py's `TextChannel.purge()` uses the bulk-delete endpoint,
            which does NOT fire `on_message_delete` for individual messages.
            So previously, purged messages never appeared in the msg log.
          * We now fetch the target messages first, log them via
            `MessageLogSystem.log_bulk_delete`, THEN bulk-delete. The log
            channel gets a single compact summary embed (not one per msg).
      - `before=ctx.message`:
          * Keeps the command message out of the fetch set entirely (the old
            `check=` approach wasted a `limit` slot on the command message).
    """
    # `flags` is a keyword-only string that absorbs any trailing tokens after
    # `amount` for prefix invocations (e.g. `!purge 2 -del` -> amount=2,
    # flags="-del"). The user passes flags="-del" explicitly.
    delete_command = False
    # Check both the raw message content (most reliable for prefix) and the
    # parsed `flags` kwarg (covers edge cases).
    if ctx.message is not None and ctx.message.content:
        content_lower = ctx.message.content.lower()
        if ' -del' in content_lower or content_lower.endswith('-del'):
            delete_command = True
    if flags and '-del' in flags.lower():
        delete_command = True

    before_obj = ctx.message
    cmd_id = ctx.message.id if ctx.message else None

    def _skip_command(m: discord.Message) -> bool:
        return cmd_id is None or m.id != cmd_id

    # Counter for messages SKIPPED because they are !nopurge-protected.
    # Reported back to the user so they know protection worked.
    protected_skipped = 0

    # Helper that fetches a batch of messages, logs them to the msg log,
    # then bulk-deletes them. Returns the list of deleted messages.
    # We do the fetch+log+delete ourselves (instead of ctx.channel.purge)
    # so we can log each message before it's gone — purge()'s bulk delete
    # never fires on_message_delete, which is why purged msgs were missing
    # from the msg log.
    #
    # NO-PURGE EXCLUSION: messages marked with !nopurge (persisted in the
    # no_purge_messages table) are NEVER deleted here — they're filtered out
    # of the batch before logging/deletion. This is the SAME exclusion set the
    # auto-purge system and !purgeall use, so protection is consistent across
    # all three purge paths.
    async def _fetch_log_delete(channel: discord.TextChannel, limit: int, before) -> List[discord.Message]:
        nonlocal protected_skipped
        batch: List[discord.Message] = []
        async for m in channel.history(limit=limit, before=before, oldest_first=False):
            # Skip the command message itself AND any !nopurge-protected msg.
            if not _skip_command(m):
                continue
            if data_manager.is_no_purge(m.id):
                protected_skipped += 1
                continue
            batch.append(m)
        if not batch:
            return []
        # Log to the msg-log channel BEFORE deleting.
        try:
            await MessageLogSystem.log_bulk_delete(
                batch, ctx.author, channel,
                reason=f"purge {limit}" if amount else "purge all",
            )
        except Exception as e:
            logging.warning(f"[Purge] msglog sync failed (non-fatal): {e}")
        # Bulk-delete. delete_messages accepts up to 100 messages at once.
        try:
            await channel.delete_messages(batch)
        except discord.HTTPException:
            # Fallback: delete one at a time (some are too old for bulk).
            for m in batch:
                try:
                    await m.delete()
                except (discord.HTTPException, discord.NotFound):
                    pass
        return batch

    if amount is None:
        # --- PURGE ALL LOGIC ---
        deleted_messages: List[discord.Message] = []
        while True:
            try:
                batch = await _fetch_log_delete(ctx.channel, 100, before_obj)
                deleted_messages.extend(batch)
                if len(batch) < 100:
                    break
                await asyncio.sleep(1)
            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = int(e.response.headers.get('Retry-After', 1)) if hasattr(e.response, 'headers') else 1
                    await asyncio.sleep(retry_after)
                else:
                    break

        deleted_count = len(deleted_messages)
        logging.info(f'{ctx.author} purged all ({deleted_count}) messages in {ctx.channel}')

        if not delete_command:
            summary = f'Deleted {deleted_count} messages.'
            if protected_skipped:
                summary += f' ({protected_skipped} protected by !nopurge)'
            message = await ctx.send(summary)
            await asyncio.sleep(2)
            try:
                await message.delete()
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
        else:
            # -del: delete the command message itself. The cleanup prompt for
            # chained commands is also skipped (see process_potential_multi_command).
            if cmd_id is not None:
                try:
                    await ctx.message.delete()
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    pass

    else:
        # --- PURGE SPECIFIC AMOUNT LOGIC ---
        if amount <= 0:
            await ctx.send("Amount must be a positive number.", ephemeral=True)
            return

        if before_obj is not None:
            deleted_messages = await _fetch_log_delete(ctx.channel, amount, before_obj)
        else:
            # Fallback: fetch one extra to compensate for the skipped
            # command message, then filter it out before logging/deleting.
            # Also filters out !nopurge-protected messages (same exclusion set
            # as purge-all and the auto-purge system).
            batch: List[discord.Message] = []
            async for m in ctx.channel.history(limit=amount + 1, oldest_first=False):
                if not _skip_command(m):
                    continue
                if data_manager.is_no_purge(m.id):
                    protected_skipped += 1
                    continue
                batch.append(m)
            if batch:
                try:
                    await MessageLogSystem.log_bulk_delete(
                        batch, ctx.author, ctx.channel,
                        reason=f"purge {amount}",
                    )
                except Exception as e:
                    logging.warning(f"[Purge] msglog sync failed (non-fatal): {e}")
                try:
                    await ctx.channel.delete_messages(batch)
                except discord.HTTPException:
                    for m in batch:
                        try:
                            await m.delete()
                        except (discord.HTTPException, discord.NotFound):
                            pass
            deleted_messages = batch

        deleted_count = len(deleted_messages)
        logging.info(f'{ctx.author} purged {deleted_count} messages in {ctx.channel}')

        if not delete_command:
            summary = f'Deleted {deleted_count} messages.'
            if protected_skipped:
                summary += f' ({protected_skipped} protected by !nopurge)'
            message = await ctx.send(summary)
            await asyncio.sleep(2)
            try:
                await message.delete()
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
        else:
            # -del: also delete the original command message.
            if cmd_id is not None:
                try:
                    await ctx.message.delete()
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    pass


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

    guild = bot.get_guild(mute['guild_id'])
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

    log_channel = bot.get_channel(config.channels.log)
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
    await bot.wait_until_ready()


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
    await bot.wait_until_ready()


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

    warning_id = str(_uuid.uuid4())[:8]
    warning = {
        'warning_id': warning_id,
        'user_id': member.id,
        'guild_id': ctx.guild.id,
        'moderator_id': ctx.author.id,
        'warning_type': WarningType.CUSTOM.value,
        'reason': reason,
        'points': 1,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'expires_at': None,
        'is_active': True,
    }

    # Persist to SQLite FIRST. If the write fails we abort — a warning that
    # only exists in memory would be silently lost on the next restart and
    # could let a repeat-offender slip past the auto-ban threshold.
    try:
        data_manager.save_warning(warning)
    except Exception as exc:
        logging.error(f"[Warnings] Could not persist warning for {member.id}: {exc}")
        await ctx.send(embed=EmbedBuilder.error(
            "Warning Failed",
            "Could not save the warning to the database — please try again.",
        ))
        return

    if ctx.guild.id not in warnings_data:
        warnings_data[ctx.guild.id] = []
    warnings_data[ctx.guild.id].append(warning)

    total_points = sum(
        1 for w in warnings_data[ctx.guild.id]
        if w['user_id'] == member.id and w.get('is_active')
    )

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
            await ctx.send(embed=EmbedBuilder.error(
                "Auto-Ban",
                f"{member.mention} has been auto-banned for exceeding the warning limit "
                f"({total_points}/{config.limits.max_warnings_before_ban})."
            ))
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
    guild_warnings = warnings_data.get(ctx.guild.id, [])
    if not guild_warnings:
        await ctx.send(f"{member.mention} has no warnings.")
        return

    count = 0
    for w in guild_warnings:
        if w['user_id'] == member.id and w.get('is_active'):
            w['is_active'] = False
            try:
                data_manager.delete_warning(w['warning_id'])
            except Exception as exc:
                logging.warning(
                    f"[Warnings] Could not deactivate {w['warning_id']}: {exc}"
                )
            count += 1

    if count == 0:
        await ctx.send(f"{member.mention} has no active warnings.")
        return

    await ctx.send(embed=EmbedBuilder.success(
        "Warnings Cleared",
        f"Cleared {count} active warning(s) for {member.mention}."
    ))
    logging.info(f"[Warnings] {ctx.author} cleared {count} warnings for {member}")


# =============================================================================
# --- TICKET TOOL COMMANDS (Full Ticket Tool Clone) ---
@bot.command(name="panel", description="Create a new ticket panel")
@commands.has_permissions(manage_channels=True)
async def create_panel(ctx: commands.Context) -> None:
    """Open the interactive panel creator."""
    view = PanelCreatorView(ctx.guild.id, ctx.author.id)
    embed = discord.Embed(
        title="Panel Creator",
        description="Use the buttons below to configure your ticket panel.\n\n"
                    "**Steps:**\n"
                    "1. Set Name - Give your panel a name\n"
                    "2. Set Embed - Customize the embed appearance\n"
                    "3. Set Button - Customize the create button\n"
                    "4. Settings - Configure ticket limits and more\n"
                    "5. Ticket Category - Folder tickets from this panel\n"
                    "6. Preview - See how it will look\n"
                    "7. Create Panel - Send the panel to this channel",
        color=discord.Color.blurple()
    )
    await ctx.send(embed=embed, view=view)


@bot.command(name="tcategory", description="Manage ticket categories (internal ticket folders)")
@commands.has_permissions(manage_channels=True)
async def ticket_category_cmd(ctx: commands.Context) -> None:
    """Open the interactive Ticket Category manager.

    Ticket Categories are internal folders that group related tickets
    (e.g. "Staff" containing "Apply for Staff" + "Staff Training"). They are
    NOT Discord channel categories — tickets in the same folder can still
    live in the same Discord channel category.
    """
    view = TicketCategoryManagerView(ctx.guild.id, ctx.author.id)
    embed = _build_ticket_categories_embed(ctx.guild.id, ctx.guild.name)
    message = await ctx.send(embed=embed, view=view)
    view.message = message


@bot.command(name="setcategory", description="Change the ticket category of this ticket (staff)")
@commands.has_permissions(manage_channels=True)
async def set_ticket_category_cmd(ctx: commands.Context) -> None:
    """Change this ticket's Ticket Category (internal folder).

    Works on any ticket — including ones created before Ticket Categories
    existed (they start as Uncategorized). The ticket's channel, claim,
    transcript and all other data are untouched; only the category
    association changes. Also available via the 📁 Category button inside
    the ticket.
    """
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await ctx.send("This is not a ticket channel.")
        return
    categories = data_manager.load_ticket_categories(ctx.guild.id)
    if not categories:
        await ctx.send("No ticket categories exist yet — create one with `!tcategory` first.")
        return
    current = _ticket_category_label(ctx.guild.id, ticket.get('ticket_category_id'))
    await ctx.send(
        f"This ticket is currently in **{current}**. Select a new category "
        f"(or remove it):",
        view=TicketCategorySelectView(ticket['ticket_id'], ctx.guild.id),
        ephemeral=True,
    )


@bot.command(name="panels", description="List all ticket panels")
@commands.has_permissions(manage_channels=True)
async def list_panels(ctx: commands.Context) -> None:
    """List all ticket panels in this server."""
    panels = data_manager.load_ticket_panels_by_guild(ctx.guild.id)
    
    if not panels:
        await ctx.send("No ticket panels found. Use `!panel` to create one.")
        return
    
    embed = discord.Embed(
        title="Ticket Panels",
        description=f"Found **{len(panels)}** panel(s) in this server:",
        color=discord.Color.blurple()
    )
    
    for panel in panels[:10]:
        channel = ctx.guild.get_channel(panel.get('channel_id'))
        channel_name = channel.mention if channel else "Unknown"
        embed.add_field(
            name=f"{panel.get('name', 'Unnamed')} (ID: {panel['panel_id']})",
            value=f"Channel: {channel_name}\nButton: {panel.get('button_label', 'Create Ticket')}",
            inline=False
        )
    
    await ctx.send(embed=embed)


@bot.command(name="deletepanel", description="Delete a ticket panel")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(panel_id="The panel ID to delete")
async def delete_panel(ctx: commands.Context, panel_id: str) -> None:
    """Delete a ticket panel."""
    panel = data_manager.load_ticket_panel(panel_id)
    
    if not panel or panel['guild_id'] != ctx.guild.id:
        await ctx.send("Panel not found in this server.")
        return
    
    if panel.get('channel_id') and panel.get('message_id'):
        try:
            channel = ctx.guild.get_channel(panel['channel_id'])
            if channel:
                message = await channel.fetch_message(panel['message_id'])
                await message.delete()
        except:
            pass
    
    data_manager.delete_ticket_panel(panel_id)
    await ctx.send(f"Panel `{panel_id}` has been deleted.")


@bot.command(name="claim", description="Claim the current ticket")
async def claim_ticket_cmd(ctx: commands.Context) -> None:
    """Claim a ticket."""
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    
    success, message = await ticket_tool.claim_ticket(ctx.channel, ctx.author)
    
    if success:
        await ctx.send(embed=discord.Embed(title="Ticket Claimed", description=message, color=discord.Color.green()))
    else:
        await ctx.send(message)


@bot.command(name="unclaim", description="Release your claim on this ticket")
async def unclaim_ticket_cmd(ctx: commands.Context) -> None:
    """Release a ticket claim."""
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    
    success, message = await ticket_tool.unclaim_ticket(ctx.channel, ctx.author)
    
    if success:
        await ctx.send(embed=discord.Embed(title="Ticket Unclaimed", description=message, color=discord.Color.orange()))
    else:
        await ctx.send(message)


@bot.command(name="close", description="Close the current ticket")
@app_commands.describe(reason="Reason for closing")
async def close_ticket_cmd(ctx: commands.Context, *, reason: str = "No reason provided") -> None:
    """Close a ticket with optional reason.

    Mirrors the Close-button flow: the ticket creator gets the star-rating
    prompt (when enabled via OWS), everyone else gets a simple confirmation.
    Previously this command ALWAYS showed the rating view to anyone — ignoring
    the ticket_rating_prompt toggle — and posted it non-ephemerally so any
    channel member could click the stars and force the close.
    """
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await ctx.send("This is not a ticket channel.")
        return
    
    is_creator = ticket.get('creator_id') == ctx.author.id
    if is_creator and ows_get("ticket_rating_prompt"):
        view = TicketRatingView(ticket['ticket_id'], reason, ctx.channel, ctx.author)
        message = "⭐ **Please rate your support experience before closing:**"
    else:
        view = ConfirmCloseView(ticket['ticket_id'], reason)
        message = "Are you sure you want to close this ticket?"
    
    if ctx.interaction is not None:
        await ctx.send(message, view=view, ephemeral=True)
    else:
        await ctx.send(message, view=view)


@bot.command(name="closerequest", aliases=["ca", "closereq"], description="Request staff to close this ticket (TicketTool-style)")
@app_commands.describe(reason="Why should this ticket be closed?")
async def close_request_cmd(ctx: commands.Context, *, reason: str = "No reason provided") -> None:
    """Post a TicketTool-style close request for staff to action.

    The ticket owner (or any member) asks for the ticket to be closed; staff
    confirm via the button. Also fires the premium `close_request` automation
    trigger, which previously existed in the automation engine but was never
    fired anywhere.
    """
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await ctx.send("This is not a ticket channel.")
        return
    if ticket.get('status') != 'open':
        await ctx.send("This ticket is not open.")
        return

    embed = discord.Embed(
        title="🙋 Close Request",
        description=(
            f"{ctx.author.mention} is requesting that this ticket be closed.\n"
            f"**Reason:** {reason}\n\n"
            "A staff member can confirm the close, or the requester can cancel."
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Ticket {ticket['ticket_id']}")
    await ctx.send(embed=embed, view=CloseRequestView(ticket['ticket_id'], ctx.author.id, reason))

    # Fire the premium 'close_request' automation trigger.
    if PREMIUM_AVAILABLE:
        try:
            pdb = getattr(bot, 'premium_db', None)
            if pdb is not None:
                panel = data_manager.load_ticket_panel(ticket['panel_id']) if ticket.get('panel_id') else None
                event = TicketTool.automations.AutomationEvent(
                    trigger='close_request', ticket=ticket, panel=panel or {},
                    guild=ctx.guild, bot=bot,
                    actor={'id': ctx.author.id, 'name': ctx.author.display_name},
                )
                await TicketTool.automations.fire_event(bot, pdb, event)
        except Exception as exc:
            logging.debug(f"[Premium] close_request trigger failed: {exc}")


# =============================================================================
# TICKET AUTOMATION PAUSE / RESUME (Ticket Tool /pause + /resume)
# =============================================================================

@bot.command(name="pause", description="Pause ALL automations for this ticket")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(duration="How long: 30m, 1h, 2d, 1w — omit for an indefinite pause")
async def pause_ticket_cmd(ctx: commands.Context, duration: Optional[str] = None) -> None:
    """TicketTool-style per-ticket automation kill switch.

    While paused the ticket is excluded from ALL automatic actions:
    event-driven automations, delayed/no-response timers, SLA breach pings,
    and idle auto-close. Timed pauses auto-resume (lazy) and the paused time
    is subtracted from pending timers on resume.
    """
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await ctx.send("This is not a ticket channel.")
        return
    if ticket.get('status') != 'open':
        await ctx.send("Only open tickets can be paused.")
        return
    if _ticket_automation_paused(ticket):
        until = ticket.get('automation_paused_until')
        await ctx.send(
            "This ticket is already paused."
            + (f" Auto-resumes <t:{int(datetime.fromisoformat(str(until).replace('Z', '+00:00')).timestamp())}:R>." if until else " Use `!resume` to lift the pause.")
        )
        return

    seconds, error = parse_pause_duration(duration)
    if error:
        await ctx.send(error)
        return

    now = datetime.now(timezone.utc)
    ticket['automation_paused'] = 1
    ticket['automation_paused_at'] = now.isoformat()
    ticket['automation_paused_until'] = (
        (now + timedelta(seconds=seconds)).isoformat() if seconds else None
    )
    data_manager.save_ticket(ticket)

    if seconds:
        until_ts = int((now + timedelta(seconds=seconds)).timestamp())
        desc = (
            f"All automations for this ticket are paused until <t:{until_ts}:F> "
            f"(<t:{until_ts}:R>).\nPaused by {ctx.author.mention}."
        )
        footer = "Automations resume automatically at the deadline"
    else:
        desc = (
            "All automations for this ticket are paused **indefinitely**.\n"
            f"Paused by {ctx.author.mention}. Use `!resume` to lift the pause."
        )
        footer = "Paused — no automatic actions will run for this ticket"

    embed = discord.Embed(
        title="⏸️ Ticket Paused",
        description=desc,
        color=discord.Color.orange(),
        timestamp=now,
    )
    embed.set_footer(text=footer)
    await ctx.send(embed=embed)
    logging.info(f"[Pause] {ctx.author} paused ticket {ticket['ticket_id']} (duration={duration or 'indefinite'})")


@bot.command(name="resume", description="Resume automations for this paused ticket")
@commands.has_permissions(manage_channels=True)
async def resume_ticket_cmd(ctx: commands.Context) -> None:
    """Lift a ticket's automation pause.

    Pending delayed/no-response timers are shifted forward by the pause
    duration (paused time doesn't count), and the auto-close idle clock
    restarts from now.
    """
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await ctx.send("This is not a ticket channel.")
        return
    if not int(ticket.get('automation_paused') or 0):
        await ctx.send("This ticket is not paused.")
        return

    shifted = 0
    paused_seconds = 0
    if PREMIUM_AVAILABLE:
        try:
            pdb = getattr(bot, 'premium_db', None)
            if pdb is not None:
                result = TicketTool.automations.resume_ticket_automations(bot, pdb, ticket)
                shifted = result.get('shifted', 0)
                paused_seconds = result.get('paused_seconds', 0)
            else:
                ticket['automation_paused'] = 0
                ticket['automation_paused_at'] = None
                ticket['automation_paused_until'] = None
                ticket['automation_resumed_at'] = datetime.now(timezone.utc).isoformat()
                data_manager.save_ticket(ticket)
        except Exception as exc:
            logging.warning(f"[Resume] premium resume failed, falling back: {exc}")
            ticket['automation_paused'] = 0
            ticket['automation_paused_at'] = None
            ticket['automation_paused_until'] = None
            ticket['automation_resumed_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket(ticket)
    else:
        ticket['automation_paused'] = 0
        ticket['automation_paused_at'] = None
        ticket['automation_paused_until'] = None
        ticket['automation_resumed_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_ticket(ticket)

    paused_min = paused_seconds // 60
    extra = ""
    if shifted:
        extra = f"\n⏱️ {shifted} pending timer(s) were pushed forward so paused time doesn't count."
    embed = discord.Embed(
        title="▶️ Ticket Resumed",
        description=(
            f"Automations are active again for this ticket "
            f"(paused for ~{paused_min} minute(s)).{extra}\n"
            f"Resumed by {ctx.author.mention}."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="The auto-close idle clock restarts from now")
    await ctx.send(embed=embed)
    logging.info(f"[Resume] {ctx.author} resumed ticket {ticket['ticket_id']} (shifted {shifted} timers)")


# =============================================================================
# MANUAL RATING (Ticket Tool /rate) + TICKET INFO + PRIVATE + HELP
# =============================================================================

class ManualRatingView(View):
    """Ticket Tool-style manual rating prompt: the creator rates the support
    without closing the ticket (unlike TicketRatingView, which closes after).

    One rating per ticket — enforced on submit. Only the ticket creator can
    click the stars."""

    def __init__(self, ticket_id: str, creator_id: int):
        super().__init__(timeout=600)
        self.ticket_id = ticket_id
        self.creator_id = creator_id
        self.rated = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "Only the ticket creator can rate this ticket.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="⭐", style=discord.ButtonStyle.secondary, row=0)
    async def rate_1(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit(interaction, 1)

    @discord.ui.button(label="⭐⭐", style=discord.ButtonStyle.secondary, row=0)
    async def rate_2(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit(interaction, 2)

    @discord.ui.button(label="⭐⭐⭐", style=discord.ButtonStyle.secondary, row=0)
    async def rate_3(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit(interaction, 3)

    @discord.ui.button(label="⭐⭐⭐⭐", style=discord.ButtonStyle.secondary, row=1)
    async def rate_4(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit(interaction, 4)

    @discord.ui.button(label="⭐⭐⭐⭐⭐", style=discord.ButtonStyle.success, row=1)
    async def rate_5(self, interaction: discord.Interaction, button: Button) -> None:
        await self._submit(interaction, 5)

    async def _submit(self, interaction: discord.Interaction, rating: int) -> None:
        if self.rated:
            await interaction.response.defer()
            return
        self.rated = True
        ticket = data_manager.load_ticket(self.ticket_id)
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return
        if ticket.get('rating') is not None:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content="This ticket has already been rated. Thanks anyway!", view=self,
            )
            return
        ticket['rating'] = rating
        data_manager.save_ticket(ticket)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"⭐ Thank you for rating this support experience! You gave **{rating} star(s)**.",
            view=self,
        )
        self.stop()


@bot.command(name="rate", description="Send the rating prompt to this ticket's creator")
@commands.has_permissions(manage_channels=True)
async def rate_ticket_cmd(ctx: commands.Context) -> None:
    """Ticket Tool-style manual CSAT: staff send the star-rating prompt to
    the ticket creator (each ticket can be rated once)."""
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await ctx.send("This is not a ticket channel.")
        return
    if ticket.get('status') != 'open':
        await ctx.send("Only open tickets can be rated.")
        return
    if ticket.get('rating') is not None:
        await ctx.send(
            f"This ticket has already been rated: **{ticket['rating']} star(s)**."
        )
        return
    creator = ctx.guild.get_member(int(ticket.get('creator_id') or 0))
    if creator is None:
        await ctx.send("The ticket creator is no longer in this server — they can't rate it.")
        return

    await ctx.send(
        f"{creator.mention} — please rate your support experience in this ticket "
        f"(requested by {ctx.author.mention}):",
        view=ManualRatingView(ticket['ticket_id'], creator.id),
        allowed_mentions=discord.AllowedMentions(users=True),
    )


def _format_age(iso_raw: Optional[str]) -> str:
    """Human-readable age of an ISO timestamp ('2d 3h', '45m', …)."""
    if not iso_raw:
        return "—"
    try:
        then = datetime.fromisoformat(str(iso_raw).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return "—"
    seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@bot.command(name="ticket-info", description="Show full status info for this ticket")
async def ticket_info_cmd(ctx: commands.Context) -> None:
    """Ticket Tool-style ticket status overview: creator, category, priority,
    claim, SLA state, automation state, participants, age, activity, and more."""
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await ctx.send("This is not a ticket channel.")
        return

    status = ticket.get('status', 'open')
    status_emoji = {'open': '🟢', 'closed': '🔒', 'closing': '🟠',
                    'pending': '🟡', 'failed': '🔴'}.get(status, '❔')
    priority = ticket.get('priority') or 'normal'
    prio_emoji = PRIORITY_EMOJIS.get(priority, '')
    color = PRIORITY_COLORS.get(priority, discord.Color.blurple())
    if status == 'closed':
        color = discord.Color.dark_grey()

    creator = ctx.guild.get_member(int(ticket.get('creator_id') or 0))
    creator_str = creator.mention if creator else f"<@{ticket.get('creator_id')}>"
    claimer_str = "Unclaimed"
    if ticket.get('claimed_by'):
        claimer = ctx.guild.get_member(int(ticket['claimed_by']))
        claimer_str = claimer.mention if claimer else f"<@{ticket['claimed_by']}>"

    panel = data_manager.load_ticket_panel(ticket['panel_id']) if ticket.get('panel_id') else None
    ticket_type = ticket.get('category') or (panel.get('name') if panel else 'General')
    # Internal Ticket Category folder (Uncategorized when none assigned).
    ticket_category = _resolve_ticket_category_for_display(ctx.guild.id, ticket)

    embed = discord.Embed(
        title=f"{status_emoji} Ticket Info — {ticket['ticket_id']}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="📋 Status", value=status.capitalize(), inline=True)
    embed.add_field(name="🚨 Priority", value=f"{prio_emoji} {priority.capitalize()}", inline=True)
    embed.add_field(name="🧩 Type", value=str(ticket_type), inline=True)
    embed.add_field(name="📁 Category", value=ticket_category, inline=True)
    embed.add_field(name="👤 Creator", value=creator_str, inline=True)
    embed.add_field(name="🙋 Claimed By", value=claimer_str, inline=True)
    embed.add_field(name="🔒 Private", value="Yes" if ticket.get('is_private') else "No", inline=True)
    if ticket.get('subject'):
        embed.add_field(name="📝 Subject", value=str(ticket['subject'])[:1024], inline=False)

    # Activity block.
    created_iso = ticket.get('created_at')
    last_msg_iso = data_manager.get_last_ticket_message_time(ticket['ticket_id'])
    try:
        message_count = len(data_manager.load_ticket_messages(ticket['ticket_id']))
    except Exception:
        message_count = 0
    embed.add_field(
        name="⏱️ Activity",
        value=(
            f"Created: {created_iso[:16].replace('T', ' ')} UTC (age {_format_age(created_iso)})\n"
            f"Last message: {_format_age(last_msg_iso)} ago • Messages: {message_count}"
        ),
        inline=False,
    )
    if ticket.get('first_response_at'):
        embed.add_field(name="⚡ First Response", value=f"{_format_age(ticket['first_response_at'])} after creation", inline=True)
    if ticket.get('rating') is not None:
        embed.add_field(name="⭐ Rating", value=f"{ticket['rating']} star(s)", inline=True)
    if ticket.get('escalation_count'):
        embed.add_field(name="⬆️ Escalations", value=str(ticket['escalation_count']), inline=True)

    # Automation state (pause + premium SLA).
    auto_state = []
    if _ticket_automation_paused(ticket):
        until = ticket.get('automation_paused_until')
        if until:
            ts = int(datetime.fromisoformat(str(until).replace('Z', '+00:00')).timestamp())
            auto_state.append(f"⏸️ Paused — resumes <t:{ts}:R>")
        else:
            auto_state.append("⏸️ Paused (indefinite)")
    else:
        auto_state.append("✅ Active")
    if PREMIUM_AVAILABLE:
        try:
            pdb = getattr(bot, 'premium_db', None)
            if pdb is not None:
                sla_state = pdb.get_sla_state(ticket['ticket_id'])
                if sla_state:
                    fr = '✅ met' if sla_state.get('first_response_met_at') else (
                        '⏰ due' if sla_state.get('first_response_due_at') else '—')
                    res = '✅ met' if sla_state.get('resolution_met_at') else (
                        '⏰ due' if sla_state.get('resolution_due_at') else '—')
                    auto_state.append(f"SLA first response: {fr} • resolution: {res}")
        except Exception:
            pass
    embed.add_field(name="🤖 Automations", value='\n'.join(auto_state), inline=False)

    # Participants: members with explicit view overwrites (creator + added).
    participants = []
    try:
        for target, overwrite in getattr(ctx.channel, 'overwrites', {}).items():
            if isinstance(target, discord.Member) and target != ctx.guild.me:
                participants.append(target.mention)
    except Exception:
        pass
    if participants:
        embed.add_field(name="👥 Participants", value=', '.join(participants[:20]), inline=False)

    if status == 'closed':
        closer = ctx.guild.get_member(int(ticket.get('closed_by') or 0)) if ticket.get('closed_by') else None
        if closer is not None:
            closer_str = closer.mention
        elif ticket.get('closed_by'):
            closer_str = f"<@{ticket.get('closed_by')}>"
        else:
            closer_str = 'Unknown'
        embed.add_field(
            name="🔒 Closure",
            value=(
                f"Closed {_format_age(ticket.get('closed_at'))} ago by {closer_str}\n"
                f"Reason: {ticket.get('close_reason') or 'No reason provided'}"
            ),
            inline=False,
        )

    embed.set_footer(text=f"Ticket {ticket['ticket_id']} • /ticket-info")
    await ctx.send(embed=embed)


@bot.command(name="private", description="Make this ticket private (hidden from other staff)")
@commands.has_permissions(manage_channels=True)
async def private_ticket_cmd(ctx: commands.Context) -> None:
    """Ticket Tool-style /private: remove the support role's access so only
    the creator, the claimer, and admins (who bypass overwrites) can see the
    ticket."""
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await ctx.send("This is not a ticket channel.")
        return
    if ticket.get('is_private'):
        await ctx.send("This ticket is already private. Use `!unprivate` to restore staff access.")
        return

    panel = data_manager.load_ticket_panel(ticket['panel_id']) if ticket.get('panel_id') else None
    settings = data_manager.load_ticket_settings(ctx.guild.id) or {}
    support_role_id = (
        (panel.get('support_role_id') if panel else None)
        or settings.get('support_role_id')
    )
    hidden_roles = []
    if support_role_id:
        role = ctx.guild.get_role(int(support_role_id))
        if role:
            try:
                await ctx.channel.set_permissions(
                    role, view_channel=False, send_messages=False,
                    reason=f"Ticket made private by {ctx.author}",
                )
                hidden_roles.append(role.mention)
            except (discord.Forbidden, discord.HTTPException) as exc:
                await ctx.send(f"Could not hide the support role: {exc}")
                return
    ticket['is_private'] = 1
    data_manager.save_ticket(ticket)

    desc = "🔒 This ticket is now **private** — only the creator, claimer, and admins can see it."
    if hidden_roles:
        desc += f"\nHidden from: {', '.join(hidden_roles)}"
    await ctx.send(embed=discord.Embed(
        description=desc + f"\nMade private by {ctx.author.mention}.",
        color=discord.Color.dark_theme(),
        timestamp=datetime.now(timezone.utc),
    ))
    logging.info(f"[Tickets] {ctx.author} made ticket {ticket['ticket_id']} private")


@bot.command(name="unprivate", description="Restore staff access to this private ticket")
@commands.has_permissions(manage_channels=True)
async def unprivate_ticket_cmd(ctx: commands.Context) -> None:
    """Lift /private: give the support role its standard ticket access back."""
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await ctx.send("This is not a ticket channel.")
        return
    if not ticket.get('is_private'):
        await ctx.send("This ticket is not private.")
        return

    panel = data_manager.load_ticket_panel(ticket['panel_id']) if ticket.get('panel_id') else None
    settings = data_manager.load_ticket_settings(ctx.guild.id) or {}
    support_role_id = (
        (panel.get('support_role_id') if panel else None)
        or settings.get('support_role_id')
    )
    restored = []
    if support_role_id:
        role = ctx.guild.get_role(int(support_role_id))
        if role:
            try:
                await ctx.channel.set_permissions(
                    role, view_channel=True, send_messages=True,
                    read_message_history=True, attach_files=True,
                    reason=f"Ticket unprivated by {ctx.author}",
                )
                restored.append(role.mention)
            except (discord.Forbidden, discord.HTTPException) as exc:
                await ctx.send(f"Could not restore the support role: {exc}")
                return
    ticket['is_private'] = 0
    data_manager.save_ticket(ticket)

    desc = "🔓 This ticket is no longer private — the support team has access again."
    if restored:
        desc += f"\nRestored for: {', '.join(restored)}"
    await ctx.send(embed=discord.Embed(
        description=desc + f"\nRestored by {ctx.author.mention}.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    ))
    logging.info(f"[Tickets] {ctx.author} restored ticket {ticket['ticket_id']} to non-private")


@bot.command(name="tickethelp", description="Show every ticket-system command by category")
async def ticket_help_cmd(ctx: commands.Context) -> None:
    """Ticket Tool-style /help: categorized command discovery for the whole
    ticket subsystem (core + premium)."""
    premium_note = "" if PREMIUM_AVAILABLE else "\n*(Premium package not loaded — the ⭐ commands are inactive.)*"
    embed = discord.Embed(
        title="🎫 Ticket System Help",
        description=f"Everything you can do with the ticket system.{premium_note}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="🎫 Panels & Creation",
        value=(
            "`!panel` • `!panels` • `!deletepanel` • `!panelupdate`\n"
            "`!multipanel` • `!dropdownpanel` • `!reactionpanel`\n"
            "`!panelquestion` • `!new` • `!ticket`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎟️ In-Ticket (everyone)",
        value=(
            "`!ticket-info` • `!transcript` • `!closerequest` (`!ca`)\n"
            "`!add @user` • `!remove @user` (staff)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Staff Management",
        value=(
            "`!claim` • `!unclaim` • `!close` • `!reopen`\n"
            "`!rename` • `!move` • `!note` • `!notes` • `!priority`\n"
            "`!pause` • `!resume` • `!rate` • `!private` • `!unprivate`"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Configuration (admin)",
        value=(
            "`!ticketsettings` • `!ticketlog` • `!limitbypass` • `!ticketstats`\n"
            "`!ticketblacklist` • `!ticketunblacklist` • `!tickets` • `!dbcleanup`"
        ),
        inline=False,
    )
    embed.add_field(
        name="⭐ Premium (package)",
        value=(
            "`!naming` • `!schedule` • `!claimconfig` • `!roleauto` • `!automate`\n"
            "`!escalate` • `!escalateroute` • `!transcriptconfig` • `!slaconfig`\n"
            "`!analytics` • `!csat` • `!staffstats` • `!export` • `!kb`\n"
            "`!canned` • `!flow` • `!customcommand` • `!locale` • …"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔍 Diagnostics",
        value="`!ticketdebug` • `!permissionlevel`",
        inline=False,
    )
    embed.set_footer(text="Use commands inside a ticket channel where noted")
    await ctx.send(embed=embed)


@bot.command(name="transcript", description="Generate a transcript of this ticket")
@app_commands.describe(
    channel="Optional channel to send the transcript to",
    lines="Max number of messages to include (default: all)",
)
async def transcript_cmd(ctx: commands.Context, channel: Optional[discord.TextChannel] = None, lines: Optional[int] = None) -> None:
    """Generate a transcript of the current ticket (TicketTool $transcript)."""
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await ctx.send("This is not a ticket channel.")
        return
    
    limit = max(1, min(int(lines), 1000)) if lines else None
    transcript = await ticket_tool._generate_transcript(ctx.channel, ticket, ctx.author, limit=limit)
    if channel is not None:
        await channel.send(embed=transcript['embed'], file=transcript['file'])
        await _ticket_respond(ctx, f"Transcript sent to {channel.mention}.", ephemeral=True)
        try:
            await log_ticket_event(ctx.guild, 'transcript', ticket, actor=ctx.author,
                                   detail=f"Exported to #{channel.name}")
        except Exception:
            pass
    else:
        await ctx.send(embed=transcript['embed'], file=transcript['file'])


@bot.command(name="panelquestion", description="Manage the questions (form) shown before a ticket is created")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(
    action="Action: add, remove, or list",
    panel_id="Panel ID (see /panels)",
    question="Question text (for add)",
    question_type="text (single line) or paragraph (for add)",
    required="Whether answering is required (for add)",
    placeholder="Input placeholder (for add)",
    order_index="Position of the question, 1-5 (for add)",
    question_id="Question ID to delete (for remove)",
)
async def panel_question_cmd(
    ctx: commands.Context,
    action: str,
    panel_id: str,
    question: Optional[str] = None,
    question_type: str = "text",
    required: bool = True,
    placeholder: Optional[str] = None,
    order_index: Optional[int] = None,
    question_id: Optional[str] = None,
) -> None:
    """TicketTool-style panel forms: up to 5 questions asked in a modal
    before the ticket is created. The questions table + modal existed but no
    UI could ever create questions, leaving the whole feature orphaned."""
    action = (action or '').lower().strip()
    panel = data_manager.load_ticket_panel(panel_id)
    if not panel or panel.get('guild_id') != ctx.guild.id:
        await ctx.send(f"Panel `{panel_id}` not found in this server.")
        return

    if action == 'add':
        if not question:
            await ctx.send("Provide the `question` text to add.")
            return
        existing = data_manager.load_panel_questions(panel_id)
        if len(existing) >= 5:
            await ctx.send("This panel already has the maximum of 5 questions. Remove one first.")
            return
        q_type = 'paragraph' if (question_type or '').lower().startswith('para') else 'text'
        next_index = (max((int(q.get('order_index') or 0) for q in existing), default=0) + 1) if order_index is None else order_index
        question_row = {
            'question_id': str(uuid.uuid4())[:8],
            'panel_id': panel_id,
            'guild_id': ctx.guild.id,
            'question_text': question[:256],
            'question_type': q_type,
            'required': 1 if required else 0,
            'placeholder': (placeholder or '')[:100],
            'order_index': next_index,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        data_manager.save_ticket_question(question_row)
        embed = discord.Embed(
            title="❓ Panel Question Added",
            description=(
                f"**Panel:** {panel.get('name', 'Unknown')} (`{panel_id}`)\n"
                f"**Question:** {question[:256]}\n"
                f"**Type:** {q_type}\n"
                f"**Required:** {'Yes' if required else 'No'}\n"
                f"**Order:** {next_index}\n"
                f"**Question ID:** `{question_row['question_id']}`"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"{len(existing) + 1}/5 questions on this panel")
        await ctx.send(embed=embed)
        return

    if action == 'remove':
        if not question_id:
            await ctx.send("Provide the `question_id` to remove (see `!panelquestion list`).")
            return
        deleted = data_manager.delete_ticket_question(question_id)
        if deleted:
            await ctx.send(f"Question `{question_id}` removed from panel `{panel_id}`.")
        else:
            await ctx.send(f"Question `{question_id}` not found.")
        return

    if action == 'list':
        questions = data_manager.load_panel_questions(panel_id)
        if not questions:
            await ctx.send(
                f"Panel `{panel_id}` has no questions. Add one with "
                "`!panelquestion add`."
            )
            return
        embed = discord.Embed(
            title=f"❓ Panel Questions — {panel.get('name', 'Unknown')}",
            description=f"Panel ID: `{panel_id}`",
            color=discord.Color.blurple(),
        )
        for q in questions[:5]:
            embed.add_field(
                name="{}. {}".format(q.get('order_index', 0), str(q['question_text'])[:100]),
                value=(
                    f"ID: `{q['question_id']}` • Type: {q.get('question_type', 'text')} • "
                    f"Required: {'Yes' if q.get('required') else 'No'}"
                ),
                inline=False,
            )
        await ctx.send(embed=embed)
        return

    await ctx.send("Unknown action. Use `add`, `remove`, or `list`.")


@bot.command(name="limitbypass", description="Set roles that bypass the ticket limits for a panel")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(
    panel_id="Panel ID (see /panels)",
    roles="Roles to bypass limits (mention them), 'none' to clear",
)
async def limit_bypass_cmd(ctx: commands.Context, panel_id: str, roles: str) -> None:
    """TicketTool-style limit bypass roles (per panel). Members holding any
    of these roles skip the per-panel and global open-ticket limits."""
    panel = data_manager.load_ticket_panel(panel_id)
    if not panel or panel.get('guild_id') != ctx.guild.id:
        await ctx.send(f"Panel `{panel_id}` not found in this server.")
        return

    raw = (roles or '').strip()
    if raw.lower() in ('none', 'clear', 'off'):
        panel['limit_bypass_role_ids'] = None
        data_manager.save_ticket_panel(panel)
        await ctx.send(f"Limit bypass roles cleared for panel `{panel_id}`.")
        return

    import re as _re
    role_ids = [int(m) for m in _re.findall(r'<@&(\d+)>', raw)]
    for token in raw.split():
        if token.isdigit():
            role_ids.append(int(token))
    role_ids = list(dict.fromkeys(role_ids))
    valid_ids = []
    for rid in role_ids:
        if ctx.guild.get_role(rid) is not None:
            valid_ids.append(rid)
    if not valid_ids:
        await ctx.send("No valid roles found. Mention roles (`@Role`) or paste their IDs.")
        return
    panel['limit_bypass_role_ids'] = json.dumps(valid_ids)
    data_manager.save_ticket_panel(panel)
    mentions = ' '.join(f"<@&{rid}>" for rid in valid_ids)
    await ctx.send(embed=discord.Embed(
        description=(
            f"✅ Members with {mentions} now bypass the ticket limits on panel "
            f"**{panel.get('name', 'Unknown')}** (`{panel_id}`)."
        ),
        color=discord.Color.green(),
    ))


@bot.command(name="panelupdate", description="Refresh an existing panel message (TicketTool-style Update)")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(panel_id="Panel ID to refresh (see /panels)")
async def panel_update_cmd(ctx: commands.Context, panel_id: str) -> None:
    """Re-render an already-sent panel message with its current embeds and
    button configuration (multi-embed included). TicketTool's dashboard
    "Update" feature: edit the panel in place instead of re-sending."""
    panel = data_manager.load_ticket_panel(panel_id)
    if not panel or panel.get('guild_id') != ctx.guild.id:
        await ctx.send(f"Panel `{panel_id}` not found in this server.")
        return
    channel_id = panel.get('channel_id')
    message_id = panel.get('message_id')
    if not channel_id or not message_id:
        await ctx.send("This panel has no sent message yet. Use `!panel` to create and send one.")
        return
    channel = ctx.guild.get_channel(int(channel_id))
    if channel is None:
        await ctx.send("The channel this panel was sent in no longer exists.")
        return
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"Could not fetch the panel message: {exc}")
        return

    view = TicketPanelView(panel)
    try:
        await message.edit(embeds=_build_panel_message_embeds(panel), view=view)
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"Could not edit the panel message: {exc}")
        return
    # Keep the fresh view registered for persistence.
    ctx.bot.add_view(view)

    # TicketTool "Update" also refreshes multi-panel messages that contain
    # this panel (attached panels / dropdown panels), pruning rows whose
    # message no longer exists.
    refreshed_multi = 0
    pruned_multi = 0
    for row in data_manager.load_multi_panels_by_guild(ctx.guild.id):
        try:
            panel_ids = json.loads(row.get('panel_ids') or '[]')
        except (ValueError, TypeError):
            panel_ids = []
        if panel_id not in panel_ids:
            continue
        mp_channel = ctx.guild.get_channel(int(row.get('channel_id') or 0))
        if mp_channel is None:
            data_manager.delete_multi_panel(row['message_id'])
            pruned_multi += 1
            continue
        try:
            mp_message = await mp_channel.fetch_message(int(row['message_id']))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            data_manager.delete_multi_panel(row['message_id'])
            pruned_multi += 1
            continue
        try:
            mp_panels = [p for p in (data_manager.load_ticket_panel(pid) for pid in panel_ids)
                         if p and p.get('is_active', 1)]
            if not mp_panels:
                data_manager.delete_multi_panel(row['message_id'])
                pruned_multi += 1
                continue
            mp_view = build_multi_panel_view(row, mp_panels)
            await mp_message.edit(embeds=_build_multi_panel_embeds(mp_panels), view=mp_view)
            ctx.bot.add_view(mp_view)
            refreshed_multi += 1
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[PanelUpdate] multi-panel {row['message_id']} refresh failed: {exc}")

    extra = ""
    if refreshed_multi or pruned_multi:
        extra = f" Also refreshed {refreshed_multi} multi-panel message(s)"
        if pruned_multi:
            extra += f" and pruned {pruned_multi} dead multi-panel row(s)."
        else:
            extra += "."
    await ctx.send(
        f"✅ Panel `{panel_id}` updated in {channel.mention} "
        f"({len(_build_panel_message_embeds(panel))} embed(s)).{extra}"
    )


def _build_multi_panel_embeds(panels: List[Dict]) -> List[discord.Embed]:
    """Build the embed(s) shown on a multi-panel message: a header embed
    listing every attached panel with its description."""
    lines = []
    for panel in panels[:25]:
        name = panel.get('name', 'Panel')
        desc = (panel.get('embed_description') or '').strip()
        emoji = panel.get('button_emoji') or '🎫'
        line = f"{emoji} **{name}**"
        if desc:
            line += f"\n> {desc[:200]}"
        lines.append(line)
    embed = discord.Embed(
        title="🎫 Support Tickets",
        description=(
            "Select the type of ticket you need below.\n\n" + "\n\n".join(lines)
        )[:4000],
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"{len(panels)} ticket type(s) available")
    return [embed]


def _parse_panel_id_list(raw: str, guild_id: int) -> Tuple[List[Dict], Optional[str]]:
    """Parse a comma-separated panel-ID list into panel rows.

    Returns (panels, error). Error is None when every ID resolved to an
    active panel in this guild.
    """
    ids = [token.strip() for token in (raw or '').split(',') if token.strip()]
    if len(ids) < 2:
        return [], "Provide at least 2 panel IDs, comma-separated (see `!panels`)."
    if len(ids) > 25:
        return [], "Discord allows at most 25 panels per multi-panel message."
    panels = []
    missing = []
    for pid in ids:
        panel = data_manager.load_ticket_panel(pid)
        if not panel or panel.get('guild_id') != guild_id or not panel.get('is_active', 1):
            missing.append(pid)
        else:
            panels.append(panel)
    if missing:
        return [], f"Panel(s) not found in this server: {', '.join(f'`{m}`' for m in missing)}"
    return panels, None


@bot.command(name="multipanel", description="Combine up to 25 panels into ONE message (TicketTool Attached Panels)")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(
    panels="Comma-separated panel IDs (see /panels), e.g. 'abc123,def456'",
    per_row="Buttons per row, 1-5 (default 5)",
)
async def multi_panel_cmd(ctx: commands.Context, panels: str, per_row: int = 5) -> None:
    """Send a TicketTool-style multi-panel: one message, one create-button
    per attached panel. Each button keeps its panel's own label/emoji/style
    and routes through the full gate flow (limits, blacklist, schedule)."""
    panel_rows, error = _parse_panel_id_list(panels, ctx.guild.id)
    if error:
        await ctx.send(error)
        return
    per_row = max(1, min(5, per_row))

    view = MultiPanelView(panel_rows, per_row=per_row)
    message = await ctx.send(embeds=_build_multi_panel_embeds(panel_rows), view=view)

    data_manager.save_multi_panel({
        'message_id': message.id,
        'guild_id': ctx.guild.id,
        'channel_id': ctx.channel.id,
        'style': 'buttons',
        'panel_ids': json.dumps([p['panel_id'] for p in panel_rows]),
        'per_row': per_row,
        'placeholder': None,
        'created_at': datetime.now(timezone.utc).isoformat(),
    })
    ctx.bot.add_view(view)
    await ctx.send(
        f"✅ Multi-panel created with **{len(panel_rows)}** panels. "
        f"Refresh it after edits with `!panelupdate <panel_id>`.",
        ephemeral=True,
    )
    logging.info(f"[TicketTool] {ctx.author} created a {len(panel_rows)}-panel multi-panel in #{ctx.channel.name}")


@bot.command(name="dropdownpanel", description="Create a dropdown-style panel (select menu routes to a panel)")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(
    panels="Comma-separated panel IDs (see /panels), e.g. 'abc123,def456'",
    placeholder="Select-menu placeholder text (default 'Select a ticket type…')",
)
async def dropdown_panel_cmd(ctx: commands.Context, panels: str, placeholder: Optional[str] = None) -> None:
    """Send a TicketTool-style dropdown panel: a Discord select menu where
    each option (label/description/emoji from the panel config) opens that
    panel's ticket flow."""
    panel_rows, error = _parse_panel_id_list(panels, ctx.guild.id)
    if error:
        await ctx.send(error)
        return

    view = TicketPanelSelectView(panel_rows, placeholder or "Select a ticket type…")
    message = await ctx.send(embeds=_build_multi_panel_embeds(panel_rows), view=view)

    data_manager.save_multi_panel({
        'message_id': message.id,
        'guild_id': ctx.guild.id,
        'channel_id': ctx.channel.id,
        'style': 'dropdown',
        'panel_ids': json.dumps([p['panel_id'] for p in panel_rows]),
        'per_row': 5,
        'placeholder': placeholder,
        'created_at': datetime.now(timezone.utc).isoformat(),
    })
    ctx.bot.add_view(view)
    await ctx.send(
        f"✅ Dropdown panel created with **{len(panel_rows)}** options. "
        f"Refresh it after edits with `!panelupdate <panel_id>`.",
        ephemeral=True,
    )
    logging.info(f"[TicketTool] {ctx.author} created a {len(panel_rows)}-option dropdown panel in #{ctx.channel.name}")


@bot.command(name="reactionpanel", description="Create a reaction-based ticket panel (react to open a ticket)")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(
    panels="Comma-separated panel IDs in the SAME order as the emojis (see /panels)",
    emojis="Comma-separated emojis, one per panel (e.g. '🎫,🛠️')",
    title="Optional panel title",
)
async def reaction_panel_cmd(ctx: commands.Context, panels: str, emojis: str, title: Optional[str] = None) -> None:
    """Ticket Tool-style reaction panels: users REACT with an emoji to open
    the matching panel's ticket (legacy compatibility with reaction-based
    servers). Each emoji maps to one panel; the bot removes the reaction
    after handling so users can re-react later.
    """
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    panel_ids = [t.strip() for t in (panels or '').split(',') if t.strip()]
    emoji_list = [e.strip() for e in (emojis or '').split(',') if e.strip()]
    if len(panel_ids) < 1:
        await ctx.send("Provide at least one panel ID (see `!panels`).")
        return
    if len(panel_ids) != len(emoji_list):
        await ctx.send(
            f"Panel/emoji mismatch: {len(panel_ids)} panel(s) but {len(emoji_list)} emoji(s). "
            "List them in the same order, comma-separated."
        )
        return
    if len(panel_ids) > 20:
        await ctx.send("Reaction panels support at most 20 emoji mappings.")

    # Resolve + validate every panel.
    panel_rows = []
    for pid in panel_ids:
        panel = data_manager.load_ticket_panel(pid)
        if not panel or panel.get('guild_id') != ctx.guild.id or not panel.get('is_active', 1):
            await ctx.send(f"Panel `{pid}` not found in this server (see `!panels`).")
            return
        panel_rows.append(panel)

    mapping = {emoji: panel_rows[i]['panel_id'] for i, emoji in enumerate(emoji_list)}
    if len(mapping) != len(emoji_list):
        await ctx.send("Duplicate emojis detected — each emoji must be unique.")
        return

    # Build the panel message.
    lines = []
    for emoji, panel in zip(emoji_list, panel_rows):
        desc = (panel.get('embed_description') or '').strip()
        line = f"{emoji} **{panel.get('name', 'Panel')}**"
        if desc:
            line += f"\n> {desc[:200]}"
        lines.append(line)
    embed = discord.Embed(
        title=title or "🎫 Support Tickets",
        description=(
            "React with the emoji matching your issue to open a ticket.\n\n"
            + "\n\n".join(lines)
        )[:4000],
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"{len(panel_rows)} ticket type(s) • remove your reaction and re-react for another ticket")
    message = await ctx.send(embed=embed)

    # Persist the mapping + react with every emoji.
    data_manager.save_reaction_panel({
        'message_id': message.id,
        'guild_id': ctx.guild.id,
        'channel_id': ctx.channel.id,
        'title': title,
        'mapping': json.dumps(mapping),
        'created_at': datetime.now(timezone.utc).isoformat(),
    })
    failed = []
    for emoji in emoji_list:
        try:
            await message.add_reaction(emoji)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            failed.append(emoji)
    # Warm the in-memory lookup cache.
    _cache_reaction_panel(message.id, mapping)
    if failed:
        await ctx.send(
            f"⚠️ Reaction panel saved, but these emojis could not be added (the bot "
            f"may lack the Add Reactions permission): {' '.join(failed)}",
            ephemeral=True,
        )
    else:
        await ctx.send(
            f"✅ Reaction panel created with **{len(panel_rows)}** mapping(s). "
            "Users react with an emoji to open that ticket type.",
            ephemeral=True,
        )
    logging.info(f"[TicketTool] {ctx.author} created a reaction panel with {len(mapping)} mapping(s) in #{ctx.channel.name}")


# In-memory reaction-panel lookup cache: {message_id: {emoji: panel_id}}.
# Bounded to MAX_REACTION_PANEL_CACHE entries (LRU by insertion order) so a
# long-running bot cannot leak memory. Populated on startup and on
# `!reactionpanel`; a DB fallback keeps it correct if a row was added by
# another process.
_reaction_panel_cache: "OrderedDict[int, Dict[str, str]]" = OrderedDict()
MAX_REACTION_PANEL_CACHE = 5000


def _cache_reaction_panel(message_id: int, mapping: Dict[str, str]) -> None:
    """Insert or refresh a reaction-panel cache entry, evicting the oldest
    entry when the cache exceeds MAX_REACTION_PANEL_CACHE."""
    _reaction_panel_cache[message_id] = mapping
    _reaction_panel_cache.move_to_end(message_id)
    while len(_reaction_panel_cache) > MAX_REACTION_PANEL_CACHE:
        _reaction_panel_cache.popitem(last=False)


def _get_reaction_panel_mapping(message_id: int) -> Optional[Dict[str, str]]:
    """Resolve {emoji: panel_id} for a reaction-panel message, via cache
    first and the reaction_panels table as fallback."""
    cached = _reaction_panel_cache.get(message_id)
    if cached is not None:
        _reaction_panel_cache.move_to_end(message_id)
        return cached
    try:
        row = data_manager.load_reaction_panel(message_id)
    except Exception:
        row = None
    if not row:
        return None
    try:
        mapping = json.loads(row.get('mapping') or '{}')
    except (ValueError, TypeError):
        return None
    if not isinstance(mapping, dict) or not mapping:
        return None
    _cache_reaction_panel(message_id, mapping)
    return mapping


async def handle_ticket_reaction_panel(payload: "discord.RawReactionActionEvent") -> None:
    """Open a ticket when a member reacts on a reaction-panel message.

    Runs the same gates as the panel button (system enabled, blacklist,
    limits — schedule is checked inside when premium is available). Failures
    are DM'd to the reactor (reactions have no ephemeral responses). The
    reaction is removed afterwards so the user can re-react later."""
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return
    member = payload.member
    if member is None or member.bot:
        return

    mapping = _get_reaction_panel_mapping(payload.message_id)
    if not mapping:
        return
    panel_id = mapping.get(str(payload.emoji))
    if not panel_id:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    panel = data_manager.load_ticket_panel(panel_id)
    if not panel or not panel.get('is_active', 1):
        return

    # Remove the user's reaction first (best-effort) so they can re-react.
    try:
        channel = guild.get_channel_or_thread(payload.channel_id)
        if channel is not None:
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(payload.emoji, member)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        pass

    async def _dm(text: str) -> None:
        try:
            await member.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass

    if not ows_get("enable_tickets"):
        await _dm("The ticket system is currently disabled by the server owner. Please try again later.")
        return
    if ows_get("ticket_blacklist"):
        blacklisted, reason = data_manager.is_user_blacklisted(guild.id, member.id)
        if blacklisted:
            await _dm(f"You are blacklisted from creating tickets. Reason: {reason}")
            return

    # Premium business-hours gate (same as the panel button).
    if PREMIUM_AVAILABLE:
        try:
            pdb = getattr(bot, 'premium_db', None)
            if pdb is not None:
                role_ids = [r.id for r in member.roles] if hasattr(member, 'roles') else []
                is_open, unavailable_msg = TicketTool.scheduling.is_panel_open_now(pdb, panel, role_ids)
                if not is_open:
                    next_open = TicketTool.scheduling.next_open_time(pdb, panel)
                    extra = f"\n\n*Opens {next_open}.*" if next_open else ''
                    await _dm(f"{unavailable_msg}{extra}")
                    return
        except Exception as exc:
            logging.debug(f"[ReactionPanel] scheduling gate failed: {exc}")

    # create_ticket re-checks blacklist + limits internally; failures DM.
    channel_obj, result = await ticket_tool.create_ticket(guild, member, panel)
    if channel_obj is None:
        await _dm(f"Could not open your ticket: {result}")
        return
    await TicketPanelView(panel)._send_welcome_message(channel_obj, member, panel)
    try:
        await member.send(f"✅ Your ticket has been opened: {channel_obj.mention} — **{panel.get('name', 'Support')}**")
    except (discord.Forbidden, discord.HTTPException):
        pass
    logging.info(f"[ReactionPanel] {member} opened ticket {result} via reaction on message {payload.message_id}")


async def _resolve_command_style_panel(guild: discord.Guild, panel_id: Optional[str]):
    """Resolve which panel a command-style ticket (/new) uses.

    TicketTool semantics: an explicit panel wins; otherwise the guild's ONLY
    active panel is used; with multiple panels the user must specify one.
    Returns (panel, error_message).
    """
    if panel_id:
        panel = data_manager.load_ticket_panel(panel_id)
        if not panel or panel.get('guild_id') != guild.id or not panel.get('is_active', 1):
            return None, f"Panel `{panel_id}` not found in this server (see `!panels`)."
        return panel, None
    active_panels = data_manager.load_ticket_panels_by_guild(guild.id)
    if len(active_panels) == 1:
        return active_panels[0], None
    if not active_panels:
        return None, "This server has no ticket panels yet. Ask staff to create one with `!panel`."
    listing = ', '.join(f"`{p['panel_id']}` ({p.get('name', 'Unnamed')})" for p in active_panels[:10])
    return None, f"This server has multiple panels — specify one: {listing}"


@bot.command(name="new", description="Open a new ticket (command-style, TicketTool $new)")
@app_commands.describe(
    user="Open on behalf of this user (staff only)",
    panel_id="Panel to open the ticket in (see /panels)",
    reason="Reason / subject for the ticket",
)
async def new_ticket_cmd(
    ctx: commands.Context,
    user: Optional[discord.Member] = None,
    panel_id: Optional[str] = None,
    *,
    reason: str = "",
) -> None:
    """TicketTool-style command tickets (`$new` / `$ticket`).

    * Self-open: full gate flow (blacklist, limits, schedule) — panels with
      questions require the panel button (the form opens there).
    * Staff opening for another user: opens on their behalf with the given
      reason (questions are skipped — staff intent).
    """
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return

    panel, error = await _resolve_command_style_panel(ctx.guild, panel_id)
    if error:
        await ctx.send(error)
        return

    # Staff-on-behalf mode.
    if user is not None and user.id != ctx.author.id:
        perms = getattr(ctx.author, 'guild_permissions', None)
        if not perms or not (perms.administrator or perms.manage_channels):
            await ctx.send("Only staff can open tickets on behalf of another user.")
            return
        if user.bot:
            await ctx.send("You cannot open a ticket for a bot.")
            return
        channel, result = await ticket_tool.create_ticket(
            ctx.guild, user, panel, subject=(reason or None) or None,
        )
        if channel:
            await TicketPanelView(panel)._send_welcome_message(channel, user, panel)
            await ctx.send(f"✅ Ticket opened for {user.mention}: {channel.mention}")
        else:
            await ctx.send(f"Failed to create ticket: {result}")
        return

    # Self-open. Business-hours gate (same as the panel button).
    if PREMIUM_AVAILABLE:
        try:
            pdb = getattr(bot, 'premium_db', None)
            if pdb is not None:
                role_ids = [r.id for r in ctx.author.roles] if hasattr(ctx.author, 'roles') else []
                is_open, unavailable_msg = TicketTool.scheduling.is_panel_open_now(pdb, panel, role_ids)
                if not is_open:
                    next_open = TicketTool.scheduling.next_open_time(pdb, panel)
                    extra = f"\n\n*Opens {next_open}.*" if next_open else ''
                    await ctx.send(f"{unavailable_msg}{extra}")
                    return
        except Exception as exc:
            logging.debug(f"[Premium] /new scheduling gate failed: {exc}")

    questions = data_manager.load_panel_questions(panel['panel_id'])
    if questions:
        await ctx.send(
            "This panel requires a short form before the ticket is created — "
            "please use the panel button."
        )
        return

    channel, result = await ticket_tool.create_ticket(
        ctx.guild, ctx.author, panel, subject=(reason or None) or None,
    )
    if channel:
        await TicketPanelView(panel)._send_welcome_message(channel, ctx.author, panel)
        await ctx.send(f"✅ Ticket created: {channel.mention}")
    else:
        await ctx.send(f"Failed to create ticket: {result}")


@bot.command(name="ticket", description="Open a new ticket (alias of /new)")
@app_commands.describe(
    user="Open on behalf of this user (staff only)",
    panel_id="Panel to open the ticket in (see /panels)",
    reason="Reason / subject for the ticket",
)
async def ticket_cmd_alias(
    ctx: commands.Context,
    user: Optional[discord.Member] = None,
    panel_id: Optional[str] = None,
    *,
    reason: str = "",
) -> None:
    """Alias of /new (TicketTool's `$new` / `$ticket` pair)."""
    await new_ticket_cmd(ctx, user, panel_id, reason=reason)


@bot.command(name="ticketdebug", description="Ticket system diagnostics (TicketTool $debug)")
async def ticket_debug_cmd(ctx: commands.Context) -> None:
    """Show the ticket-system configuration + the bot's permission status,
    mirroring TicketTool's $debug command."""
    guild = ctx.guild
    settings = data_manager.load_ticket_settings(guild.id) or {}
    panels = data_manager.load_ticket_panels_by_guild(guild.id)
    open_count = data_manager.count_open_tickets_in_guild(guild.id)

    def _flag(ok: bool) -> str:
        return "✅" if ok else "❌"

    category = guild.get_channel(settings.get('category_id') or 0) if settings.get('category_id') else None
    transcripts = guild.get_channel(settings.get('transcripts_channel_id') or 0) if settings.get('transcripts_channel_id') else None
    log_channel = guild.get_channel(settings.get('log_channel_id') or 0) if settings.get('log_channel_id') else None
    support_role = guild.get_role(settings.get('support_role_id') or 0) if settings.get('support_role_id') else None

    me = guild.me
    perms = me.guild_permissions
    embed = discord.Embed(
        title="🎫 Ticket System Diagnostics",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="System",
        value=(
            f"{_flag(ows_get('enable_tickets'))} Tickets enabled\n"
            f"🎫 Panels: **{len(panels)}** • Open tickets: **{open_count}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Configuration",
        value=(
            f"{_flag(category is not None)} Ticket category: {category.name if category else 'Not set (config fallback)'}\n"
            f"{_flag(transcripts is not None)} Transcripts channel: {transcripts.mention if transcripts else 'Not set'}\n"
            f"{_flag(log_channel is not None)} Log channel: {log_channel.mention if log_channel else 'Not set'}\n"
            f"{_flag(support_role is not None)} Support role: {support_role.mention if support_role else 'Not set'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Bot Permissions",
        value=(
            f"{_flag(perms.manage_channels)} Manage Channels\n"
            f"{_flag(perms.manage_roles)} Manage Roles\n"
            f"{_flag(perms.view_channel and perms.send_messages)} View + Send in channels"
        ),
        inline=False,
    )
    if panels:
        panel_lines = []
        for p in panels[:10]:
            panel_lines.append(f"`{p['panel_id']}` {p.get('name', 'Unnamed')}")
        embed.add_field(name="Panels", value='\n'.join(panel_lines), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="permissionlevel", description="Show your ticket-system permission level (TicketTool $permissionlevel)")
async def permission_level_cmd(ctx: commands.Context) -> None:
    """Report the invoker's effective ticket-system access level,
    mirroring TicketTool's $permissionlevel / $levels command."""
    member = ctx.author
    perms = member.guild_permissions
    settings = data_manager.load_ticket_settings(ctx.guild.id) or {}

    if ctx.guild.owner_id == member.id:
        level, desc = 5, "Server Owner (full access)"
    elif perms.administrator:
        level, desc = 4, "Administrator (full access)"
    elif perms.manage_guild:
        level, desc = 3, "Manage Server (settings + panels)"
    elif perms.manage_channels:
        level, desc = 2, "Manage Channels (staff: claim/close/priority/notes)"
    else:
        support_role_id = settings.get('support_role_id')
        support_role = ctx.guild.get_role(support_role_id) if support_role_id else None
        if support_role and support_role in member.roles:
            level, desc = 2, f"Support Team ({support_role.mention}: claim/close)"
        else:
            level, desc = 1, "User (create tickets, rate, close own)"

    embed = discord.Embed(
        title="🎫 Ticket Permission Level",
        description=f"{member.mention} — **Level {level}**: {desc}",
        color=discord.Color.green() if level >= 2 else discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Levels",
        value=(
            "`5` Server Owner • `4` Administrator • `3` Manage Server\n"
            "`2` Staff (Manage Channels / Support Role) • `1` User"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="ticketlog", description="Configure the ticket log channel and logged events")
@commands.has_permissions(manage_guild=True)
@app_commands.describe(
    channel="Channel for ticket logs (leave empty to view the current config)",
    events="Comma-separated events, 'all', or 'none' (e.g. created,closed,transcript)",
)
async def ticket_log_cmd(ctx: commands.Context, channel: Optional[discord.TextChannel] = None, events: Optional[str] = None) -> None:
    """TicketTool-style logging channel configuration. Logged events:
    created, closed, reopened, renamed, deleted, transcript, claim, unclaim, priority."""
    settings = data_manager.load_ticket_settings(ctx.guild.id) or {'guild_id': ctx.guild.id}
    if channel is None and events is None:
        current_events = get_ticket_log_events(ctx.guild.id)
        log_channel = ctx.guild.get_channel(settings.get('log_channel_id') or 0) if settings.get('log_channel_id') else None
        embed = discord.Embed(
            title="🎫 Ticket Logging",
            description=(
                f"**Log channel:** {log_channel.mention if log_channel else 'Not set'}\n"
                f"**Logged events:** {', '.join(f'`{e}`' for e in current_events) if current_events else 'None'}"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Available events",
            value="`created` `closed` `reopened` `renamed` `deleted` `transcript` `claim` `unclaim` `priority`",
            inline=False,
        )
        embed.set_footer(text="Usage: /ticketlog channel:#logs events:created,closed,transcript")
        await ctx.send(embed=embed)
        return

    if channel is not None:
        settings['log_channel_id'] = channel.id
    if events is not None:
        raw = (events or '').strip().lower()
        if raw == 'all':
            new_events = list(TICKET_LOG_EVENT_INFO.keys())
        elif raw in ('none', 'off', 'disable'):
            new_events = []
        else:
            new_events = [e.strip() for e in raw.split(',') if e.strip() in TICKET_LOG_EVENT_INFO]
        settings['log_events'] = json.dumps(new_events)
    settings['updated_at'] = datetime.now(timezone.utc).isoformat()
    data_manager.save_ticket_settings(settings)

    log_channel = ctx.guild.get_channel(settings.get('log_channel_id') or 0) if settings.get('log_channel_id') else None
    current_events = get_ticket_log_events(ctx.guild.id)
    await ctx.send(embed=discord.Embed(
        description=(
            f"✅ Ticket logging updated.\n"
            f"**Log channel:** {log_channel.mention if log_channel else 'Not set'}\n"
            f"**Logged events:** {', '.join(f'`{e}`' for e in current_events) if current_events else 'None'}"
        ),
        color=discord.Color.green(),
    ))


@bot.command(name="ticketsettings", description="Configure ticket system settings")
@commands.has_permissions(manage_guild=True)
async def ticket_settings_cmd(ctx: commands.Context) -> None:
    """Open ticket settings configuration."""
    settings = data_manager.load_ticket_settings(ctx.guild.id) or {}
    
    embed = discord.Embed(title="Ticket System Settings", color=discord.Color.blurple())
    
    category_id = settings.get('category_id')
    category = ctx.guild.get_channel(category_id) if category_id else None
    
    transcripts_id = settings.get('transcripts_channel_id')
    transcripts = ctx.guild.get_channel(transcripts_id) if transcripts_id else None
    
    support_id = settings.get('support_role_id')
    support_role = ctx.guild.get_role(support_id) if support_id else None
    
    embed.add_field(name="Ticket Category", value=category.name if category else "Not Set", inline=True)
    embed.add_field(name="Ticket Transcripts Archive Channel", value=transcripts.mention if transcripts else "Not Set", inline=True)
    embed.add_field(name="Support Role", value=support_role.mention if support_role else "Not Set", inline=True)
    embed.add_field(name="Max Tickets/User", value=str(settings.get('max_tickets_per_user', 3)), inline=True)
    embed.add_field(name="Auto-Close Hours", value=str(settings.get('auto_close_hours', 24)), inline=True)
    embed.add_field(name="DM Transcripts", value="Yes" if settings.get('dm_transcripts', 1) else "No", inline=True)

    log_channel = ctx.guild.get_channel(settings.get('log_channel_id') or 0) if settings.get('log_channel_id') else None
    embed.add_field(name="Ticket Log Channel", value=log_channel.mention if log_channel else "Not Set", inline=True)
    closed_category = ctx.guild.get_channel(settings.get('closed_category_id') or 0) if settings.get('closed_category_id') else None
    embed.add_field(name="Closed Ticket Category", value=closed_category.name if closed_category else "Not Set", inline=True)
    logged_events = get_ticket_log_events(ctx.guild.id)
    embed.add_field(name="Logged Events", value=', '.join(f'`{e}`' for e in logged_events) if logged_events else 'None', inline=False)
    embed.add_field(
        name="Limits & Timers",
        value=(
            f"Max Closed/User: {settings.get('max_closed_tickets_per_user', 0) or 0} • "
            f"Max Open (all): {settings.get('max_open_tickets_all', 0) or 0} • "
            f"SLA: {settings.get('sla_hours', 0) or 0}h"
        ),
        inline=False,
    )
    
    embed.set_footer(text="Use the modal to update settings")
    
    await ctx.send(embed=embed, view=TicketSettingsConfigView(ctx.guild.id))


class TicketSettingsConfigView(View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
    
    @discord.ui.button(label="Set Category", style=discord.ButtonStyle.primary)
    async def set_category(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetTicketCategoryModal(self.guild_id))
    
    @discord.ui.button(label="Set Transcripts", style=discord.ButtonStyle.primary)
    async def set_transcripts(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetTranscriptsChannelModal(self.guild_id))
    
    @discord.ui.button(label="Set Support Role", style=discord.ButtonStyle.secondary)
    async def set_support_role(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetSupportRoleModal(self.guild_id))

    @discord.ui.button(label="Set Log Channel", style=discord.ButtonStyle.secondary, row=1)
    async def set_log_channel(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetTicketLogChannelModal(self.guild_id))

    @discord.ui.button(label="Set Closed Category", style=discord.ButtonStyle.secondary, row=1)
    async def set_closed_category(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetClosedCategoryModal(self.guild_id))

    @discord.ui.button(label="Set Limits", style=discord.ButtonStyle.primary, row=2)
    async def set_limits(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SetTicketLimitsModal(self.guild_id))


class SetTicketLimitsModal(Modal, title="Ticket Limits & Timers"):
    """TicketTool-style Limit Options (guild-wide):
    max tickets/user, max closed tickets/user, max open tickets overall,
    auto-close hours, and the first-response SLA.
    Previously these columns had no configuration UI at all."""

    max_per_user = TextInput(label="Max Open Tickets / User (0=off)", max_length=3, placeholder="3")
    max_closed = TextInput(label="Max CLOSED Tickets / User (0=off)", max_length=3, placeholder="0")
    max_open_all = TextInput(label="Max Open Tickets Overall (0=off)", max_length=4, placeholder="0")
    auto_close = TextInput(label="Auto-Close Idle Hours (0=off)", max_length=4, placeholder="24")
    sla = TextInput(label="First-Response SLA Hours (0=off)", max_length=4, placeholder="0")

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        self.max_per_user.default = str(settings.get('max_tickets_per_user', 3) or 3)
        self.max_closed.default = str(settings.get('max_closed_tickets_per_user', 0) or 0)
        self.max_open_all.default = str(settings.get('max_open_tickets_all', 0) or 0)
        self.auto_close.default = str(settings.get('auto_close_hours', 24) or 0)
        self.sla.default = str(settings.get('sla_hours', 0) or 0)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        def _to_int(value: str, default: int = 0, cap: int = 10000) -> int:
            try:
                return max(0, min(cap, int((value or '').strip() or default)))
            except ValueError:
                return default

        settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
        settings['max_tickets_per_user'] = _to_int(self.max_per_user.value, 3)
        settings['max_closed_tickets_per_user'] = _to_int(self.max_closed.value, 0)
        settings['max_open_tickets_all'] = _to_int(self.max_open_all.value, 0)
        settings['auto_close_hours'] = _to_int(self.auto_close.value, 0, cap=24*30)
        settings['sla_hours'] = _to_int(self.sla.value, 0, cap=24*30)
        settings['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_ticket_settings(settings)
        await interaction.response.send_message(
            "✅ Ticket limits updated:\n"
            f"• Max open tickets/user: **{settings['max_tickets_per_user']}**\n"
            f"• Max closed tickets/user: **{settings['max_closed_tickets_per_user']}** "
            "(checked at creation)\n"
            f"• Max open tickets overall: **{settings['max_open_tickets_all']}**\n"
            f"• Auto-close idle hours: **{settings['auto_close_hours']}** "
            "(needs the `Auto-Close Idle Tickets` toggle)\n"
            f"• First-response SLA: **{settings['sla_hours']}h**",
            ephemeral=True,
        )


class SetTicketLogChannelModal(Modal, title="Set Ticket Log Channel"):
    channel_input = TextInput(label="Channel ID", placeholder="Enter the ticket-log channel ID")

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        if settings.get('log_channel_id'):
            self.channel_input.default = str(settings['log_channel_id'])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.channel_input.value.strip()
        settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
        if raw.lower() in ('none', 'off', 'clear', '0'):
            settings['log_channel_id'] = None
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message("Ticket log channel cleared.", ephemeral=True)
            return
        try:
            channel_id = int(raw)
            channel = interaction.guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message("Invalid channel ID.", ephemeral=True)
                return
            settings['log_channel_id'] = channel_id
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message(
                f"Ticket log channel set to {channel.mention}. Configure logged events with `!ticketlog`.",
                ephemeral=True,
            )
        except ValueError:
            await interaction.response.send_message("Please enter a valid number (or 'none' to clear).", ephemeral=True)


class SetClosedCategoryModal(Modal, title="Set Closed Ticket Category"):
    category_input = TextInput(label="Category ID", placeholder="Category for two-step closed tickets")

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        if settings.get('closed_category_id'):
            self.category_input.default = str(settings['closed_category_id'])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.category_input.value.strip()
        settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
        if raw.lower() in ('none', 'off', 'clear', '0'):
            settings['closed_category_id'] = None
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message("Closed-ticket category cleared.", ephemeral=True)
            return
        try:
            category_id = int(raw)
            category = interaction.guild.get_channel(category_id)
            if not category or not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message("Invalid category ID.", ephemeral=True)
                return
            settings['closed_category_id'] = category_id
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message(
                f"Closed tickets (two-step panels) will move to **{category.name}**.", ephemeral=True,
            )
        except ValueError:
            await interaction.response.send_message("Please enter a valid number (or 'none' to clear).", ephemeral=True)


class SetTicketCategoryModal(Modal, title="Set Ticket Category"):
    category_input = TextInput(label="Category ID", placeholder="Enter category channel ID")
    
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        if settings.get('category_id'):
            self.category_input.default = str(settings['category_id'])
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            category_id = int(self.category_input.value)
            category = interaction.guild.get_channel(category_id)
            if not category or not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message("Invalid category ID.", ephemeral=True)
                return
            settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
            settings['category_id'] = category_id
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message(f"Ticket category set to **{category.name}**", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)


class SetTranscriptsChannelModal(Modal, title="Set Transcripts Channel"):
    channel_input = TextInput(label="Channel ID", placeholder="Enter transcripts channel ID")
    
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        if settings.get('transcripts_channel_id'):
            self.channel_input.default = str(settings['transcripts_channel_id'])
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            channel_id = int(self.channel_input.value)
            channel = interaction.guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message("Invalid channel ID.", ephemeral=True)
                return
            settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
            settings['transcripts_channel_id'] = channel_id
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message(f"Transcripts channel set to {channel.mention}", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)


class SetSupportRoleModal(Modal, title="Set Support Role"):
    role_input = TextInput(label="Role ID", placeholder="Enter support role ID")
    
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        settings = data_manager.load_ticket_settings(guild_id) or {}
        if settings.get('support_role_id'):
            self.role_input.default = str(settings['support_role_id'])
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            role_id = int(self.role_input.value)
            role = interaction.guild.get_role(role_id)
            if not role:
                await interaction.response.send_message("Invalid role ID.", ephemeral=True)
                return
            settings = data_manager.load_ticket_settings(self.guild_id) or {'guild_id': self.guild_id}
            settings['support_role_id'] = role_id
            settings['updated_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket_settings(settings)
            await interaction.response.send_message(f"Support role set to {role.mention}", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)


@bot.command(name="ticketblacklist", description="Blacklist a user from creating tickets")
@commands.has_permissions(manage_guild=True)
@app_commands.describe(user="User to blacklist", reason="Reason for blacklist")
async def ticket_blacklist_cmd(ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided") -> None:
    """Blacklist a user from creating tickets."""
    blacklist_data = {
        'blacklist_id': str(_uuid.uuid4())[:8],
        'guild_id': ctx.guild.id,
        'user_id': user.id,
        'reason': reason,
        'blacklisted_by': ctx.author.id,
        'blacklisted_at': datetime.now(timezone.utc).isoformat(),
        'is_active': 1
    }
    data_manager.save_ticket_blacklist(blacklist_data)
    await ctx.send(embed=discord.Embed(
        title="User Blacklisted",
        description=f"{user.mention} has been blacklisted from creating tickets.\n**Reason:** {reason}",
        color=discord.Color.red()
    ))


@bot.command(name="ticketunblacklist", description="Remove a user from the ticket blacklist")
@commands.has_permissions(manage_guild=True)
@app_commands.describe(user="User to unblacklist")
async def ticket_unblacklist_cmd(ctx: commands.Context, user: discord.Member) -> None:
    """Remove a user from the ticket blacklist."""
    success = data_manager.remove_ticket_blacklist(ctx.guild.id, user.id)
    if success:
        await ctx.send(f"{user.mention} has been removed from the blacklist.")
    else:
        await ctx.send(f"{user.mention} is not blacklisted.")


@bot.command(name="tickets", description="View open tickets")
@commands.has_permissions(manage_channels=True)
async def view_tickets_cmd(ctx: commands.Context) -> None:
    tickets = data_manager.load_tickets_by_guild(ctx.guild.id, 'open')
    if not tickets:
        await ctx.send("No open tickets.")
        return

    # Filter out orphaned tickets (channel no longer exists)
    valid_tickets = []
    for ticket in tickets:
        channel = ctx.guild.get_channel(ticket['channel_id'])
        if channel:
            valid_tickets.append(ticket)
        else:
            # Auto-close orphaned tickets
            ticket['status'] = 'closed'
            ticket['close_reason'] = 'Channel no longer exists (auto-cleaned)'
            ticket['closed_at'] = datetime.now(timezone.utc).isoformat()
            data_manager.save_ticket(ticket)

    if not valid_tickets:
        await ctx.send("No open tickets with active channels.")
        return

    embed = discord.Embed(title="Open Tickets", description=f"Found **{len(valid_tickets)}** open ticket(s)", color=discord.Color.blurple())
    for ticket in valid_tickets[:10]:
        creator = ctx.guild.get_member(ticket['creator_id'])
        creator_name = creator.mention if creator else f"<@{ticket['creator_id']}>"
        claimed = ""
        if ticket.get('claimed_by'):
            claimer = ctx.guild.get_member(ticket['claimed_by'])
            claimed = f"\nClaimed: {claimer.mention if claimer else 'Unknown'}"
        embed.add_field(
            name=f"Ticket #{ticket['ticket_id']}",
            value=f"Creator: {creator_name}\nCategory: {ticket.get('category', 'General')}\nChannel: <#{ticket['channel_id']}>{claimed}",
            inline=False
        )
    await ctx.send(embed=embed)


# =============================================================================
# --- NEW TICKET TOOL FEATURE COMMANDS ---
# =============================================================================

async def _ticket_respond(ctx: commands.Context, content: Optional[str] = None, *,
                          embed: Optional[discord.Embed] = None, ephemeral: bool = False) -> None:
    """Send a message that behaves correctly for prefix (!cmd) invocations.

    Regular (prefix) messages cannot be ephemeral; older discord.py 2.x
    versions raise `TypeError` on `ephemeral=` when the context is not
    interaction-backed.

    The interaction-backed branch below is a safe guard (kept from the
    hybrid-command era): all commands are prefix-only now, so `ephemeral=`
    is ignored and a normal message is sent.
    """
    if ephemeral and ctx.interaction is not None:
        await ctx.send(content, embed=embed, ephemeral=True)
    else:
        await ctx.send(content, embed=embed)


@bot.command(name="add", description="Add a user or role to the current ticket")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(user="User to add to this ticket", role="Role to add to this ticket")
async def ticket_add_cmd(ctx: commands.Context, user: Optional[discord.Member] = None, role: Optional[discord.Role] = None) -> None:
    """Add a user OR a role to the ticket (TicketTool $add accepts both)."""
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
        return
    if user is None and role is None:
        await _ticket_respond(ctx, "Mention a user or a role to add (e.g. `!add @user`).", ephemeral=True)
        return
    target = user or role
    try:
        await ctx.channel.set_permissions(
            target,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        await _ticket_respond(ctx, f"Could not add {target.mention}: {e}", ephemeral=True)
        return
    await _ticket_respond(ctx, embed=discord.Embed(
        description=f"✅ {target.mention} has been added to the ticket.",
        color=discord.Color.green()
    ))
    logging.info(f"[Tickets] {ctx.author} added {target} to ticket {ticket['ticket_id']}")


@bot.command(name="remove", description="Remove a user or role from the current ticket")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(user="User to remove from this ticket", role="Role to remove from this ticket")
async def ticket_remove_cmd(ctx: commands.Context, user: Optional[discord.Member] = None, role: Optional[discord.Role] = None) -> None:
    """Remove a user OR a role from the ticket (TicketTool $remove accepts both)."""
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
        return
    if user is None and role is None:
        await _ticket_respond(ctx, "Mention a user or a role to remove (e.g. `!remove @user`).", ephemeral=True)
        return
    if user is not None and user.id == ticket.get('creator_id'):
        await _ticket_respond(ctx, "You cannot remove the ticket creator.", ephemeral=True)
        return
    target = user or role
    try:
        await ctx.channel.set_permissions(target, overwrite=None)
    except (discord.Forbidden, discord.HTTPException) as e:
        await _ticket_respond(ctx, f"Could not remove {target.mention}: {e}", ephemeral=True)
        return
    await _ticket_respond(ctx, embed=discord.Embed(
        description=f"✅ {target.mention} has been removed from the ticket.",
        color=discord.Color.orange()
    ))
    logging.info(f"[Tickets] {ctx.author} removed {target} from ticket {ticket['ticket_id']}")


@bot.command(name="rename", description="Rename the current ticket channel")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(name="New channel name (no spaces)")
async def ticket_rename_cmd(ctx: commands.Context, *, name: str) -> None:
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
        return
    clean_name = ''.join(c if c.isalnum() or c == '-' else '-' for c in name.lower())[:50]
    old_name = ctx.channel.name
    await ctx.channel.edit(name=clean_name)
    try:
        await log_ticket_event(ctx.guild, 'renamed', ticket, actor=ctx.author,
                               detail=f"`{old_name}` → `{clean_name}`")
    except Exception:
        pass
    await ctx.send(embed=discord.Embed(
        description=f"✅ Channel renamed from `{old_name}` → `{clean_name}`",
        color=discord.Color.green()
    ))


@bot.command(name="move", description="Move the ticket to a different panel category")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(panel_id="Panel ID to move this ticket under")
async def ticket_move_cmd(ctx: commands.Context, panel_id: str) -> None:
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
        return
    panel = data_manager.load_ticket_panel(panel_id)
    if not panel or panel['guild_id'] != ctx.guild.id:
        await _ticket_respond(ctx, f"Panel `{panel_id}` not found in this server.", ephemeral=True)
        return
    category_id = panel.get('category_id')
    if not category_id:
        await _ticket_respond(ctx, "That panel has no category set.", ephemeral=True)
        return
    category = ctx.guild.get_channel(category_id)
    if not category:
        await _ticket_respond(ctx, "Category channel not found.", ephemeral=True)
        return
    await ctx.channel.edit(category=category)
    ticket['panel_id'] = panel_id
    ticket['category'] = panel.get('name', 'General')
    data_manager.save_ticket(ticket)
    await ctx.send(embed=discord.Embed(
        description=f"✅ Ticket moved to **{panel.get('name', 'Unknown')}** (category: {category.name})",
        color=discord.Color.green()
    ))


@bot.command(name="note", description="Add a private staff note to this ticket")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(content="Note content (only staff can view these)")
async def ticket_note_cmd(ctx: commands.Context, *, content: str) -> None:
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
        return
    note = {
        'note_id': str(_uuid.uuid4())[:8],
        'ticket_id': ticket['ticket_id'],
        'guild_id': ctx.guild.id,
        'author_id': ctx.author.id,
        'content': content,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    data_manager.save_ticket_note(note)
    await _ticket_respond(ctx, embed=discord.Embed(
        title="📝 Note Saved",
        description=content,
        color=discord.Color.yellow(),
        timestamp=datetime.now(timezone.utc)
    ).set_footer(text=f"By {ctx.author.display_name} • ID: {note['note_id']}"), ephemeral=True)


@bot.command(name="notes", description="View all staff notes for this ticket")
@commands.has_permissions(manage_channels=True)
async def ticket_notes_cmd(ctx: commands.Context) -> None:
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
        return
    notes = data_manager.load_ticket_notes(ticket['ticket_id'])
    if not notes:
        await _ticket_respond(ctx, "No notes found for this ticket.", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"📝 Notes for Ticket #{ticket['ticket_id']}",
        color=discord.Color.yellow(),
        timestamp=datetime.now(timezone.utc)
    )
    for note in notes[:10]:
        author = ctx.guild.get_member(note['author_id'])
        author_name = author.display_name if author else f"<@{note['author_id']}>"
        created = note['created_at'][:16].replace('T', ' ')
        embed.add_field(
            name=f"Note {note['note_id']} — {author_name} at {created}",
            value=note['content'][:1024],
            inline=False
        )
    await _ticket_respond(ctx, embed=embed, ephemeral=True)


@bot.command(name="priority", description="Set the priority of the current ticket")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(level="Priority level: low, normal, high, urgent")
@app_commands.choices(level=[
    app_commands.Choice(name="🟢 Low", value="low"),
    app_commands.Choice(name="🔵 Normal", value="normal"),
    app_commands.Choice(name="🟠 High", value="high"),
    app_commands.Choice(name="🔴 Urgent", value="urgent"),
])
async def ticket_priority_cmd(ctx: commands.Context, level: str) -> None:
    ticket = data_manager.load_ticket_by_channel(ctx.channel.id)
    if not ticket:
        await _ticket_respond(ctx, "This is not a ticket channel.", ephemeral=True)
        return
    ticket['priority'] = level
    data_manager.save_ticket(ticket)
    emoji = PRIORITY_EMOJIS.get(level, '')
    color = PRIORITY_COLORS.get(level, discord.Color.blue())
    try:
        await log_ticket_event(ctx.guild, 'priority', ticket, actor=ctx.author,
                               detail=f"Priority set to **{level.capitalize()}**")
    except Exception:
        pass
    await ctx.send(embed=discord.Embed(
        description=f"{emoji} Ticket priority set to **{level.capitalize()}** by {ctx.author.mention}",
        color=color,
        timestamp=datetime.now(timezone.utc)
    ))


@bot.command(name="reopen", description="Reopen a closed ticket")
@commands.has_permissions(manage_channels=True)
@app_commands.describe(ticket_id="The ticket ID to reopen")
async def ticket_reopen_cmd(ctx: commands.Context, ticket_id: str) -> None:
    """Reopen a closed ticket by recreating its channel."""
    if not ticket_tool:
        await ctx.send("Ticket system not initialized.")
        return
    ticket = data_manager.load_ticket(ticket_id)
    if not ticket:
        await ctx.send(f"Ticket `{ticket_id}` not found.")
        return
    if ticket['guild_id'] != ctx.guild.id:
        await ctx.send("That ticket does not belong to this server.")
        return
    if ticket.get('status') == 'open':
        existing_channel = ctx.guild.get_channel(ticket['channel_id'])
        if existing_channel:
            await ctx.send(f"That ticket is already open: {existing_channel.mention}")
            return

    # Two-step tickets keep their channel after closing: reopen IN PLACE
    # (restore perms, move back, reset row) instead of recreating a channel.
    existing_channel = ctx.guild.get_channel(ticket.get('channel_id') or 0) if ticket.get('channel_id') else None
    if ticket.get('status') == 'closed' and existing_channel is not None:
        ok, message = await reopen_ticket_in_place(existing_channel, ctx.author)
        if ok:
            await ctx.send(message)
        else:
            await ctx.send(message)
        return

    # Rebuild the channel
    creator = ctx.guild.get_member(ticket['creator_id'])
    if not creator:
        await ctx.send("Cannot reopen — the original ticket creator is no longer in the server.")
        return

    # Use the panel if available, otherwise use default settings
    panel = data_manager.load_ticket_panel(ticket.get('panel_id', '')) or {}
    settings = data_manager.load_ticket_settings(ctx.guild.id) or {}
    # Fall back to config defaults so re-opened tickets also land in the
    # configured Tickets category (same fix as create_ticket).
    category_id = panel.get('category_id') or settings.get('category_id') or config.channels.tickets
    support_role_id = panel.get('support_role_id') or settings.get('support_role_id') or config.roles.ticket_support

    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        creator: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        ctx.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
    }
    if support_role_id:
        role = ctx.guild.get_role(support_role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)

    category = ctx.guild.get_channel(category_id) if category_id else None
    # Unique channel name (same convention as create_ticket): the reopened
    # channel gets a fresh short suffix so it can't collide with a stale one.
    reopen_suffix = str(uuid.uuid4())[:8]
    channel_name = f"ticket-{creator.display_name}".lower()[:40]
    channel_name = ''.join(c if c.isalnum() or c == '-' else '-' for c in channel_name)
    channel_name = f"{channel_name}-{reopen_suffix}"[:90]

    try:
        new_channel = await ctx.guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket {ticket_id} (reopened) - {creator}"
        )
    except Exception as e:
        await ctx.send(f"Failed to create channel: {e}")
        return

    ticket['channel_id'] = new_channel.id
    ticket['status'] = 'open'
    ticket['closed_at'] = None
    ticket['closed_by'] = None
    ticket['close_reason'] = None
    data_manager.save_ticket(ticket)

    control_view = TicketControlView(ticket_id)
    await new_channel.send(
        embed=discord.Embed(
            title=f"🔓 Ticket Reopened — #{ticket_id}",
            description=f"This ticket was reopened by {ctx.author.mention}.\n{creator.mention} your ticket has been reopened.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        ),
        view=control_view
    )
    await ctx.send(f"Ticket `{ticket_id}` reopened: {new_channel.mention}")
    logging.info(f"[Tickets] {ctx.author} reopened ticket {ticket_id}")

    # TicketTool-style ticket logging: "Ticket Reopened" entry.
    try:
        await log_ticket_event(ctx.guild, 'reopened', ticket, actor=ctx.author)
    except Exception:
        pass

    # --- PREMIUM TIER 1: fire 'reopened' automations ---
    if PREMIUM_AVAILABLE:
        try:
            panel = data_manager.load_ticket_panel(ticket.get('panel_id') or '') if ticket.get('panel_id') else None
            await TicketTool.wiring.on_ticket_reopen(
                bot=bot, ticket_tool=ticket_tool, ticket=ticket,
                panel=panel, guild=ctx.guild,
            )
        except Exception as exc:
            logging.warning(f"[Premium] on_ticket_reopen failed: {exc}")


@bot.command(name="ticketstats", description="View ticket statistics for this server")
@commands.has_permissions(manage_channels=True)
async def ticket_stats_cmd(ctx: commands.Context) -> None:
    stats = data_manager.load_ticket_stats(ctx.guild.id)
    embed = discord.Embed(
        title=f"📊 Ticket Statistics — {ctx.guild.name}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Total Tickets", value=str(stats['total']), inline=True)
    embed.add_field(name="Open", value=str(stats['open']), inline=True)
    embed.add_field(name="Closed", value=str(stats['closed']), inline=True)

    avg_rating = f"⭐ {stats['avg_rating']}" if stats['avg_rating'] else "No ratings yet"
    embed.add_field(name="Avg Rating", value=avg_rating, inline=True)

    avg_close = f"{stats['avg_close_hours']}h" if stats['avg_close_hours'] is not None else "N/A"
    embed.add_field(name="Avg Close Time", value=avg_close, inline=True)

    priorities = stats.get('priorities', {})
    if priorities:
        prio_text = "\n".join(
            f"{PRIORITY_EMOJIS.get(k, '')} {k.capitalize()}: {v}"
            for k, v in priorities.items()
        )
    else:
        prio_text = "No open tickets"
    embed.add_field(name="Open by Priority", value=prio_text, inline=False)

    await ctx.send(embed=embed)


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
    # All counts go through the public DataManager API so we never touch
    # `_connection` directly (which would bypass the write lock and share
    # the connection across threads without synchronisation).
    orphan_answers     = data_manager.count_orphan_rows(
        'ticket_answers',     'ticket_id', valid_ticket_ids)
    orphan_notes       = data_manager.count_orphan_rows(
        'ticket_notes',       'ticket_id', valid_ticket_ids)
    orphan_messages    = data_manager.count_orphan_rows(
        'ticket_messages',    'ticket_id', valid_ticket_ids)
    orphan_transcripts = data_manager.count_orphan_rows(
        'ticket_transcripts', 'ticket_id', valid_ticket_ids)
    orphan_questions   = data_manager.count_orphan_rows(
        'ticket_questions',   'panel_id',  valid_panel_ids)

    inactive_warnings = data_manager.count_inactive_warnings()

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
    archived_invites_count = data_manager.count_invite_history(ctx.guild.id)
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

@bot.command()
@commands.has_permissions(manage_roles=True)
async def securitycheck(ctx: commands.Context, member: discord.Member) -> None:
    embed = discord.Embed(title=f"Security Check: {member.display_name}", color=discord.Color.blue())
    
    now = datetime.now(timezone.utc)
    created_at = member.created_at.replace(tzinfo=timezone.utc) if member.created_at.tzinfo is None else member.created_at
    account_age = (now - created_at).days
    
    age_risk = "HIGH" if account_age < 13 else "MEDIUM" if account_age < 30 else "LOW"
    embed.add_field(name="Account Age", value=f"{account_age} days ({age_risk} risk)", inline=True)
    
    join_age = (now - member.joined_at.replace(tzinfo=timezone.utc)).days if member.joined_at else "Unknown"
    embed.add_field(name="Time in Server", value=f"{join_age} days", inline=True)
    
    profile_flags: List[str] = []
    if not member.avatar:
        profile_flags.append("No profile picture")
    if len(member.display_name) < 3:
        profile_flags.append("Very short username")
    if member.display_name.isdigit():
        profile_flags.append("Username is all numbers")
    
    profile_risk = "HIGH" if len(profile_flags) >= 2 else "MEDIUM" if profile_flags else "LOW"
    embed.add_field(name="Profile Risk", value=f"{profile_risk}\n{', '.join(profile_flags) if profile_flags else 'No flags'}", inline=False)
    
    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
    embed.set_footer(text=f"User ID: {member.id}")
    
    await ctx.send(embed=embed)


# --- SHUTDOWN & STATUS COMMANDS ---
@bot.command()
@commands.has_permissions(administrator=True)
async def shutdown(ctx: commands.Context) -> None:
    if process_manager.is_busy():
        await ctx.send("Cannot shutdown: a background save is still in progress!")
        return

    await ctx.send("Shutting down bot...")
    logging.info(f"Bot shutdown initiated by {ctx.author}")
    save_all_data()
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


class PaginatedHelpView(View):
    """Paginated view for the `!cmds` help command.

    Shows one page at a time with First / Previous / page-indicator / Next /
    Last / Close buttons. Only the user who invoked the command can navigate.
    The page indicator is a disabled button that always shows "Page N / M".
    """

    def __init__(self, user_id: int, pages: List[discord.Embed], timeout: float = 300, initial_page: int = 0):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.pages = pages

        # Clamp the initial page to make sure it doesn't exceed the number of pages we have
        max_page = len(pages) - 1 if pages else 0
        self.current_page = min(initial_page, max_page) if max_page > 0 else 0

        self._update_buttons()

    def _update_buttons(self) -> None:
        self.first_button.disabled = self.current_page == 0
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= len(self.pages) - 1
        self.last_button.disabled = self.current_page >= len(self.pages) - 1
        # Page indicator reflects the current position.
        self.page_indicator.label = f"📄 {self.current_page + 1}/{len(self.pages)}"

    def current_embed(self) -> discord.Embed:
        embed = self.pages[self.current_page]
        # Update the footer to reflect the current page number.
        embed.set_footer(text=f"Page {self.current_page + 1}/{len(self.pages)} • {brand_text('[GANG ABBR]')} Commands")
        return embed

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=EmbedBuilder.warning("Not Your Menu", "Only the person who ran `!cmds` can navigate these pages."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="⏮ First", style=discord.ButtonStyle.secondary, custom_id="help_first_page")
    async def first_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        if self.current_page > 0:
            self.current_page = 0
            self._update_buttons()
            await interaction.response.edit_message(embed=self.current_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="⬅ Previous", style=discord.ButtonStyle.primary, custom_id="help_prev_page")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.current_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="📄 1/1", style=discord.ButtonStyle.secondary, custom_id="help_page_indicator", disabled=True)
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Disabled button — no interaction should reach here, but stay safe.
        await interaction.response.defer()

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.primary, custom_id="help_next_page")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.current_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Last ⏭", style=discord.ButtonStyle.secondary, custom_id="help_last_page")
    async def last_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        last = len(self.pages) - 1
        if self.current_page < last:
            self.current_page = last
            self._update_buttons()
            await interaction.response.edit_message(embed=self.current_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.danger, custom_id="help_close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        self.stop()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass

    async def on_timeout(self) -> None:
        """Disable all buttons when the view times out."""
        for child in self.children:
            child.disabled = True


def _hex_to_int(color_hex: str) -> Optional[int]:
    """Parse '#RRGGBB' / 'RRGGBB' / '0xRRGGBB' into an int. Returns None on bad input."""
    if not color_hex:
        return None
    raw = color_hex.strip().lstrip('#')
    if raw.lower().startswith('0x'):
        raw = raw[2:]
    if not re.fullmatch(r'[0-9a-fA-F]{6}', raw):
        return None
    return int(raw, 16)


async def _fetch_image_bytes(url: str) -> Optional[bytes]:
    """Download image bytes from a URL using aiohttp (bundled with discord.py)."""
    try:
        import aiohttp  # discord.py depends on aiohttp, so always available
    except ImportError:
        logging.error("[Branding] aiohttp not available to fetch avatar image")
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logging.warning(f"[Branding] avatar fetch HTTP {resp.status}")
                    return None
                data = await resp.read()
                if not data:
                    return None
                # Discord avatar limit is 10 MB; bail out early if absurdly large.
                if len(data) > 10 * 1024 * 1024:
                    logging.warning("[Branding] avatar image too large (>10MB)")
                    return None
                return data
    except Exception as exc:
        logging.warning(f"[Branding] avatar fetch failed: {exc}")
        return None


# =========================================================================
# 2) STICKY ROLES — Dyno premium (re-apply roles on rejoin)
# =========================================================================
class StickyRoleSystem:
    @staticmethod
    def get_eligible_roles(guild_id: int) -> List[int]:
        cfg = data_manager.get_sticky_role_config(guild_id)
        try:
            return [int(r) for r in json.loads(cfg.get('eligible_role_ids', '[]'))]
        except Exception:
            return []

    @staticmethod
    def set_eligible_roles(guild_id: int, role_ids: List[int]) -> None:
        cfg = data_manager.get_sticky_role_config(guild_id)
        cfg['eligible_role_ids'] = json.dumps(role_ids)
        cfg['enabled'] = 1
        cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_sticky_role_config(cfg)

    @staticmethod
    def set_enabled(guild_id: int, enabled: bool) -> None:
        cfg = data_manager.get_sticky_role_config(guild_id)
        cfg['enabled'] = 1 if enabled else 0
        cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
        data_manager.save_sticky_role_config(cfg)

    @staticmethod
    def is_enabled(guild_id: int) -> bool:
        return bool(data_manager.get_sticky_role_config(guild_id).get('enabled', 0))

    @staticmethod
    def capture_member_roles(member: discord.Member) -> None:
        """Save the member's eligible roles so they can be restored on rejoin."""
        if not StickyRoleSystem.is_enabled(member.guild.id):
            return
        eligible = set(StickyRoleSystem.get_eligible_roles(member.guild.id))
        if not eligible:
            # No eligible list configured → save all non-managed, non-everyone roles.
            saved = [
                r.id for r in member.roles
                if not r.managed and r.id != member.guild.default_role.id
            ]
        else:
            saved = [r.id for r in member.roles if r.id in eligible]
        # Always save at least an empty list so we know we've seen this member.
        data_manager.save_sticky_roles(member.guild.id, member.id, saved)
        logging.info(
            f"[Sticky] Saved {len(saved)} role(s) for {member} leaving {member.guild.id}"
        )

    @staticmethod
    async def restore_member_roles(member: discord.Member) -> int:
        """Re-apply previously saved roles. Returns count of roles restored."""
        if not StickyRoleSystem.is_enabled(member.guild.id):
            return 0
        saved = data_manager.load_sticky_roles(member.guild.id, member.id)
        if not saved:
            return 0

        guild = member.guild
        eligible = set(StickyRoleSystem.get_eligible_roles(guild.id))
        to_add: List[discord.Role] = []
        for rid in saved:
            role = guild.get_role(rid)
            if role is None:
                continue
            if role.managed:
                continue
            if eligible and rid not in eligible:
                continue
            # Hierarchy safety.
            if guild.me.top_role <= role:
                continue
            to_add.append(role)

        if not to_add:
            return 0
        try:
            await member.add_roles(*to_add, reason="Sticky role restore on rejoin")
            logging.info(f"[Sticky] Restored {len(to_add)} role(s) to {member} rejoining {guild.id}")
            return len(to_add)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[Sticky] Could not restore roles to {member}: {exc}")
            return 0


@bot.group(name="stickyrole", aliases=["stickyroles", "sr"], description="Sticky Roles (Dyno premium — re-apply on rejoin)", invoke_without_command=True)
@app_commands.default_permissions(manage_roles=True)
async def sticky_group(ctx: commands.Context) -> None:
    if ctx.invoked_subcommand is None:
        await ctx.send(embed=EmbedBuilder.info(
            "Sticky Roles",
            "Subcommands: `enable`, `disable`, `add`, `remove`, `list`, `status`.",
        ), ephemeral=True)


@sticky_group.command(name="enable", description="Turn ON sticky roles for this server")
@commands.has_permissions(manage_roles=True)
async def sticky_enable(ctx: commands.Context) -> None:
    StickyRoleSystem.set_enabled(ctx.guild.id, True)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Sticky Roles Enabled ✅",
        "Members will now keep their eligible roles when they leave and rejoin.",
    ), ctx.guild.id))


@sticky_group.command(name="disable", description="Turn OFF sticky roles for this server")
@commands.has_permissions(manage_roles=True)
async def sticky_disable(ctx: commands.Context) -> None:
    StickyRoleSystem.set_enabled(ctx.guild.id, False)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.warning(
        "Sticky Roles Disabled",
        "Roles will no longer be saved/restored on rejoin. Existing saved data is kept.",
    ), ctx.guild.id))


@sticky_group.command(name="add", description="Add a role to the sticky-eligible list")
@app_commands.describe(role="The role that should be saved/restored on rejoin")
@commands.has_permissions(manage_roles=True)
async def sticky_add(ctx: commands.Context, role: discord.Role) -> None:
    if role.managed:
        await ctx.send(embed=EmbedBuilder.error("Managed Role", "Integration/managed roles can't be sticky."), ephemeral=True)
        return
    eligible = StickyRoleSystem.get_eligible_roles(ctx.guild.id)
    if role.id not in eligible:
        eligible.append(role.id)
        StickyRoleSystem.set_eligible_roles(ctx.guild.id, eligible)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Sticky Role Added",
        f"{role.mention} is now sticky-eligible. {len(eligible)} role(s) on the list.",
    ), ctx.guild.id))


@sticky_group.command(name="remove", description="Remove a role from the sticky-eligible list")
@app_commands.describe(role="The role to remove from the sticky list")
@commands.has_permissions(manage_roles=True)
async def sticky_remove(ctx: commands.Context, role: discord.Role) -> None:
    eligible = StickyRoleSystem.get_eligible_roles(ctx.guild.id)
    if role.id in eligible:
        eligible.remove(role.id)
        StickyRoleSystem.set_eligible_roles(ctx.guild.id, eligible)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Sticky Role Removed",
        f"{role.mention} is no longer sticky-eligible. {len(eligible)} role(s) remain.",
    ), ctx.guild.id))


@sticky_group.command(name="list", description="Show all sticky-eligible roles")
@commands.has_permissions(manage_roles=True)
async def sticky_list(ctx: commands.Context) -> None:
    eligible = StickyRoleSystem.get_eligible_roles(ctx.guild.id)
    enabled = StickyRoleSystem.is_enabled(ctx.guild.id)
    embed = discord.Embed(
        title="📌 Sticky Roles",
        description=f"Status: **{'Enabled ✅' if enabled else 'Disabled ❌'}**\n"
                    f"Eligible roles: **{len(eligible)}**\n"
                    + ("_(No specific roles — all non-managed roles are saved.)_" if not eligible else ""),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    if eligible:
        lines = []
        for rid in eligible:
            r = ctx.guild.get_role(rid)
            lines.append(f"• {r.mention if r else f'~deleted:`{rid}`'}")
        embed.add_field(name="Eligible", value="\n".join(lines), inline=False)
    await ctx.send(embed=EmbedBuilder.branded(embed, ctx.guild.id))


@sticky_group.command(name="status", description="Show sticky-role status for the server or a member")
@app_commands.describe(member="Optional member to check their saved sticky roles")
@commands.has_permissions(manage_roles=True)
async def sticky_status(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    enabled = StickyRoleSystem.is_enabled(ctx.guild.id)
    if member is None:
        await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.info(
            "Sticky Role Status",
            f"Enabled: **{'Yes' if enabled else 'No'}**\n"
            f"Eligible roles: **{len(StickyRoleSystem.get_eligible_roles(ctx.guild.id))}**",
        ), ctx.guild.id))
        return
    saved = data_manager.load_sticky_roles(ctx.guild.id, member.id)
    roles = [ctx.guild.get_role(r) for r in saved]
    lines = [r.mention if r else f"~deleted:`{r}`" for r in roles]
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.info(
        f"Sticky Roles for {member.display_name}",
        f"Saved roles: **{len(saved)}**\n" + ("\n".join(lines) if lines else "_(none saved)_"),
    ), ctx.guild.id))


# =========================================================================
# 3) FULL MESSAGE LOGGING — Dyno premium (edit/delete content)
# =========================================================================
class MessageLogSystem:
    @staticmethod
    def get_config(guild_id: int) -> Dict:
        return data_manager.get_message_log_config(guild_id)

    @staticmethod
    def save_config(cfg: Dict) -> None:
        data_manager.save_message_log_config(cfg)

    @staticmethod
    def is_channel_ignored(cfg: Dict, channel_id: int) -> bool:
        try:
            ignored = set(json.loads(cfg.get('ignore_channels', '[]') or '[]'))
        except Exception:
            ignored = set()
        return channel_id in ignored

    @staticmethod
    def should_log(cfg: Dict, message: discord.Message, kind: str) -> bool:
        if not cfg.get('enabled'):
            return False
        if cfg.get('ignore_bots', 1) and message.author.bot:
            return False
        if MessageLogSystem.is_channel_ignored(cfg, message.channel.id):
            return False
        if kind == 'edit' and not cfg.get('log_edits', 1):
            return False
        if kind == 'delete' and not cfg.get('log_deletes', 1):
            return False
        return True

    @staticmethod
    def cache(message: discord.Message) -> None:
        """Snapshot a message so we can recover its original content on
        edit/delete.

        Called from `on_message` for EVERY message in a msglog-enabled
        channel. The SQLite write is dispatched to a worker thread via
        `asyncio.create_task(asyncio.to_thread(...))` so the message handler
        never blocks the gateway.
        """
        if message.guild is None:
            return
        cfg = MessageLogSystem.get_config(message.guild.id)
        if not cfg.get('enabled'):
            return
        if MessageLogSystem.is_channel_ignored(cfg, message.channel.id):
            return
        try:
            attachments = []
            for a in message.attachments:
                attachments.append({
                    'filename': a.filename,
                    'url': a.url,
                    'proxy_url': getattr(a, 'proxy_url', None),
                    'size': getattr(a, 'size', None),
                })
            payload = {
                'message_id': message.id,
                'guild_id': message.guild.id,
                'channel_id': message.channel.id,
                'author_id': message.author.id,
                'author_name': str(message.author),
                'content': message.content or '',
                'attachments': json.dumps(attachments),
                'created_at': datetime.now(timezone.utc).isoformat(),
            }
            # Fire-and-forget: the write is offloaded to a thread; the caller
            # doesn't need to await it. `create_task` also serialises ordering
            # via the event loop's task scheduling.
            asyncio.create_task(asyncio.to_thread(data_manager.cache_message, payload))
        except Exception as exc:
            logging.debug(f"[MsgLog] cache scheduling failed: {exc}")

    @staticmethod
    async def log_delete(message: "discord.Message") -> None:
        if message.guild is None:
            return
        cfg = MessageLogSystem.get_config(message.guild.id)
        if not MessageLogSystem.should_log(cfg, message, 'delete'):
            return
        log_channel = message.guild.get_channel(cfg.get('log_channel_id') or 0)
        if log_channel is None:
            return

        # Prefer our cached original content (Discord's delete payload sometimes
        # still has it, but the cache is the reliable source of truth).
        cached = data_manager.load_cached_message(message.id)
        original_content = (cached['content'] if cached else None) or message.content or ''
        author_name = (cached['author_name'] if cached else None) or str(message.author)
        author_id = (cached['author_id'] if cached else None) or message.author.id

        snippet = original_content if len(original_content) <= 1024 else (original_content[:1021] + '...')

        embed = discord.Embed(
            title="🗑️ Message Deleted",
            description=(
                f"**Author:** <@{author_id}> (`{author_name}` / `{author_id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Message ID:** `{message.id}`"
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Content",
            value=(snippet if snippet.strip() else "_(empty or embed-only message)_"),
            inline=False,
        )

        # Attachments.
        attachments = []
        if cached:
            try:
                attachments = json.loads(cached.get('attachments', '[]') or '[]')
            except Exception:
                attachments = []
        elif message.attachments:
            attachments = [{'filename': a.filename, 'url': a.url} for a in message.attachments]
        if attachments:
            att_lines = [f"• [{a.get('filename','file')}]({a.get('url')})" for a in attachments[:5]]
            embed.add_field(name="Attachments", value="\n".join(att_lines), inline=False)

        try:
            await log_channel.send(embed=EmbedBuilder.branded(embed, message.guild.id))
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[MsgLog] could not send delete log: {exc}")

        data_manager.delete_cached_message(message.id)

    @staticmethod
    async def log_bulk_delete(messages: List["discord.Message"], deleted_by: "discord.Member", channel: "discord.TextChannel", reason: str = "purge") -> None:
        """Log a batch of messages deleted via `!purge` / `!purgeall`.

        discord.py's `TextChannel.purge()` uses the bulk-delete endpoint,
        which does NOT fire `on_message_delete` for individual messages —
        so the normal MessageLogSystem.log_delete path never sees them.
        This helper is called from the purge command BEFORE the bulk delete
        so each purged message is recorded in the log channel.

        Sends a single compact summary embed (not one embed per message) so
        a `!purge 100` doesn't spam the log channel with 100 embeds.
        """
        if not messages:
            return
        if channel is None or channel.guild is None:
            return
        cfg = MessageLogSystem.get_config(channel.guild.id)
        if not MessageLogSystem.should_log(cfg, messages[0], 'delete'):
            return
        log_channel = channel.guild.get_channel(cfg.get('log_channel_id') or 0)
        if log_channel is None:
            return

        embed = discord.Embed(
            title=f"🧹 Bulk Purge — {len(messages)} message(s)",
            description=(
                f"**Moderator:** {deleted_by.mention} (`{deleted_by}` / `{deleted_by.id}`)\n"
                f"**Channel:** {channel.mention}\n"
                f"**Reason:** `{reason}`"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        # Compact list of purged messages: one line each, truncated.
        # Discord embed field value limit is 1024 chars, so we cap the list
        # and note how many were truncated.
        MAX_FIELD = 1024
        lines: List[str] = []
        current_len = 0
        shown = 0
        truncated = 0
        for m in messages:
            author_disp = str(m.author)
            content = (m.content or '').replace('\n', ' ').strip()
            if not content and m.attachments:
                content = f"[attachment: {m.attachments[0].filename}]"
            if not content:
                content = "(empty)"
            if len(content) > 80:
                content = content[:77] + '...'
            line = f"`{m.id}` **{author_disp}**: {content}"
            line_len = len(line) + 1
            if current_len + line_len > MAX_FIELD - 20 and shown >= 1:
                # Stop adding lines to keep under the field limit.
                truncated = len(messages) - shown
                break
            lines.append(line)
            current_len += line_len
            shown += 1

        list_text = "\n".join(lines)
        if truncated > 0:
            list_text += f"\n..._and {truncated} more_"

        embed.add_field(
            name=f"Deleted messages ({len(messages)} total)",
            value=list_text or "_(no content)_",
            inline=False,
        )

        # Mention the bulk-delete behavior so log readers know individual
        # on_message_delete events did NOT fire for these.
        embed.set_footer(text="Bulk-purged via command • individual delete events were not fired by Discord")

        try:
            await log_channel.send(embed=EmbedBuilder.branded(embed, channel.guild.id))
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[MsgLog] could not send bulk-purge log: {exc}")

        # Also delete the cached copies of the purged messages so the cache
        # doesn't grow stale with rows for messages that no longer exist.
        for m in messages:
            try:
                data_manager.delete_cached_message(m.id)
            except Exception:
                pass

    @staticmethod
    async def log_edit(before: "discord.Message", after: "discord.Message") -> None:
        if before.guild is None:
            return
        cfg = MessageLogSystem.get_config(before.guild.id)
        if not MessageLogSystem.should_log(cfg, before, 'edit'):
            return
        # No-op if content didn't change (pin edits etc. fire on_message_edit).
        if (before.content or '') == (after.content or ''):
            return
        log_channel = before.guild.get_channel(cfg.get('log_channel_id') or 0)
        if log_channel is None:
            return

        old_content = before.content or ''
        new_content = after.content or ''
        old_snip = old_content if len(old_content) <= 1024 else (old_content[:1021] + '...')
        new_snip = new_content if len(new_content) <= 1024 else (new_content[:1021] + '...')

        embed = discord.Embed(
            title="✏️ Message Edited",
            description=(
                f"**Author:** {before.author.mention} (`{before.author.id}`)\n"
                f"**Channel:** {before.channel.mention}\n"
                f"**Message ID:** `{before.id}` — [jump]({after.jump_url})"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Before", value=(old_snip if old_snip.strip() else "_(empty)_"), inline=False)
        embed.add_field(name="After", value=(new_snip if new_snip.strip() else "_(empty)_"), inline=False)
        try:
            await log_channel.send(embed=EmbedBuilder.branded(embed, before.guild.id))
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning(f"[MsgLog] could not send edit log: {exc}")

        # Refresh the cache with the new content.
        MessageLogSystem.cache(after)


@bot.group(name="msglog", description="Full Message Logging (Dyno premium — edits + deletes with content)", invoke_without_command=True)
@app_commands.default_permissions(manage_guild=True)
async def msglog_group(ctx: commands.Context) -> None:
    if ctx.invoked_subcommand is None:
        await ctx.send(embed=EmbedBuilder.info(
            "Message Logging",
            "Subcommands: `enable`, `disable`, `channel`, `edits`, `deletes`, `ignore`, `status`.",
        ), ephemeral=True)


@msglog_group.command(name="enable", description="Turn ON full message logging")
@app_commands.describe(channel="The channel to send logs to")
@commands.has_permissions(manage_guild=True)
async def msglog_enable(ctx: commands.Context, channel: discord.TextChannel) -> None:
    cfg = MessageLogSystem.get_config(ctx.guild.id)
    cfg['enabled'] = 1
    cfg['log_channel_id'] = channel.id
    cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
    MessageLogSystem.save_config(cfg)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Message Logging Enabled ✅",
        f"Edits + deletes will be logged to {channel.mention}.\n_(Bot messages are ignored by default.)_",
    ), ctx.guild.id))


@msglog_group.command(name="disable", description="Turn OFF full message logging")
@commands.has_permissions(manage_guild=True)
async def msglog_disable(ctx: commands.Context) -> None:
    cfg = MessageLogSystem.get_config(ctx.guild.id)
    cfg['enabled'] = 0
    cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
    MessageLogSystem.save_config(cfg)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.warning(
        "Message Logging Disabled", "Edits and deletes will no longer be logged."
    ), ctx.guild.id))


@msglog_group.command(name="channel", description="Change the log channel")
@app_commands.describe(channel="The channel to send logs to")
@commands.has_permissions(manage_guild=True)
async def msglog_channel(ctx: commands.Context, channel: discord.TextChannel) -> None:
    cfg = MessageLogSystem.get_config(ctx.guild.id)
    cfg['log_channel_id'] = channel.id
    cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
    MessageLogSystem.save_config(cfg)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Log Channel Updated", f"Logs will go to {channel.mention}."
    ), ctx.guild.id))


@msglog_group.command(name="edits", description="Toggle edit logging on/off")
@app_commands.describe(enabled="True to log edits, False to stop")
@commands.has_permissions(manage_guild=True)
async def msglog_edits(ctx: commands.Context, enabled: bool) -> None:
    cfg = MessageLogSystem.get_config(ctx.guild.id)
    cfg['log_edits'] = 1 if enabled else 0
    cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
    MessageLogSystem.save_config(cfg)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Edit Logging Updated", f"Edit logging is now **{'ON' if enabled else 'OFF'}**."
    ), ctx.guild.id))


@msglog_group.command(name="deletes", description="Toggle delete logging on/off")
@app_commands.describe(enabled="True to log deletes, False to stop")
@commands.has_permissions(manage_guild=True)
async def msglog_deletes(ctx: commands.Context, enabled: bool) -> None:
    cfg = MessageLogSystem.get_config(ctx.guild.id)
    cfg['log_deletes'] = 1 if enabled else 0
    cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
    MessageLogSystem.save_config(cfg)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Delete Logging Updated", f"Delete logging is now **{'ON' if enabled else 'OFF'}**."
    ), ctx.guild.id))


@msglog_group.command(name="ignore", description="Add/remove a channel from the ignore list")
@app_commands.describe(channel="The channel to toggle ignore status for")
@commands.has_permissions(manage_guild=True)
async def msglog_ignore(ctx: commands.Context, channel: discord.TextChannel) -> None:
    cfg = MessageLogSystem.get_config(ctx.guild.id)
    try:
        ignored = json.loads(cfg.get('ignore_channels', '[]') or '[]')
    except Exception:
        ignored = []
    if channel.id in ignored:
        ignored.remove(channel.id)
        state = "no longer ignored"
    else:
        ignored.append(channel.id)
        state = "now ignored"
    cfg['ignore_channels'] = json.dumps(ignored)
    cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
    MessageLogSystem.save_config(cfg)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Ignore List Updated", f"{channel.mention} is **{state}**."
    ), ctx.guild.id))


@msglog_group.command(name="status", description="Show the current message-logging configuration")
@commands.has_permissions(manage_guild=True)
async def msglog_status(ctx: commands.Context) -> None:
    cfg = MessageLogSystem.get_config(ctx.guild.id)
    chan = ctx.guild.get_channel(cfg.get('log_channel_id') or 0)
    try:
        ignored = json.loads(cfg.get('ignore_channels', '[]') or '[]')
    except Exception:
        ignored = []
    embed = discord.Embed(
        title="📝 Message Logging Status",
        description=(
            f"Enabled: **{'Yes ✅' if cfg.get('enabled') else 'No ❌'}**\n"
            f"Log channel: {chan.mention if chan else '_(not set)_'}\n"
            f"Log edits: **{'Yes' if cfg.get('log_edits', 1) else 'No'}**\n"
            f"Log deletes: **{'Yes' if cfg.get('log_deletes', 1) else 'No'}**\n"
            f"Ignore bots: **{'Yes' if cfg.get('ignore_bots', 1) else 'No'}**\n"
            f"Ignored channels: **{len(ignored)}**"
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    if ignored:
        lines = [ctx.guild.get_channel(c).mention if ctx.guild.get_channel(c) else f"`{c}`" for c in ignored]
        embed.add_field(name="Ignored", value="\n".join(lines), inline=False)
    await ctx.send(embed=EmbedBuilder.branded(embed, ctx.guild.id))


# =========================================================================
# 4) CUSTOM BOT BRANDING — avatar / banner / footer (premium feel)
# =========================================================================
@bot.group(name="botbranding", aliases=["branding"], description="Custom Bot Branding (avatar / banner / footer)", invoke_without_command=True)
@app_commands.default_permissions(manage_guild=True)
async def branding_group(ctx: commands.Context) -> None:
    if ctx.invoked_subcommand is None:
        await ctx.send(embed=EmbedBuilder.info(
            "Custom Bot Branding",
            "Subcommands: `name`, `footer`, `color`, `thumbnail`, `image`, `banner`, `avatar`, `view`, `clear`.",
        ), ephemeral=True)


@branding_group.command(name="footer", description="Set a custom embed footer for this server")
@app_commands.describe(text="Footer text (supports [GANG NAME] / [GANG ABBR] placeholders)")
@commands.has_permissions(manage_guild=True)
async def branding_footer(ctx: commands.Context, *, text: str) -> None:
    b = data_manager.get_branding(ctx.guild.id)
    b['embed_footer'] = text[:300]
    b['updated_at'] = datetime.now(timezone.utc).isoformat()
    data_manager.save_branding(b)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Footer Updated",
        f"Embeds will now show:\n`{brand_text(text)}`\n_(applies to new reaction-role / sticky / msglog embeds)_",
    ), ctx.guild.id))


@branding_group.command(name="color", description="Set a custom embed color (hex)")
@app_commands.describe(color="Hex color, e.g. #FF6B35 or 0xFF6B35")
@commands.has_permissions(manage_guild=True)
async def branding_color(ctx: commands.Context, color: str) -> None:
    color_int = _hex_to_int(color)
    if color_int is None:
        await ctx.send(embed=EmbedBuilder.error("Bad Color", "Use a 6-digit hex like `#FF6B35`."), ephemeral=True)
        return
    b = data_manager.get_branding(ctx.guild.id)
    b['embed_color'] = color_int
    b['updated_at'] = datetime.now(timezone.utc).isoformat()
    data_manager.save_branding(b)
    preview = discord.Embed(title="Color Preview", description=f"`{color}` → `#{color_int:06X}`", color=discord.Color(color_int))
    await ctx.send(embed=EmbedBuilder.branded(preview, ctx.guild.id))


@branding_group.command(name="thumbnail", description="Set a custom embed thumbnail URL")
@app_commands.describe(url="Direct image URL")
@commands.has_permissions(manage_guild=True)
async def branding_thumbnail(ctx: commands.Context, url: str) -> None:
    if not url.startswith(("http://", "https://")):
        await ctx.send(embed=EmbedBuilder.error("Bad URL", "Thumbnail must be an http(s) URL."), ephemeral=True)
        return
    b = data_manager.get_branding(ctx.guild.id)
    b['embed_thumbnail'] = url
    b['updated_at'] = datetime.now(timezone.utc).isoformat()
    data_manager.save_branding(b)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Thumbnail Updated", "New embeds will use this thumbnail."
    ), ctx.guild.id))


@branding_group.command(name="image", description="Set a custom embed image URL")
@app_commands.describe(url="Direct image URL")
@commands.has_permissions(manage_guild=True)
async def branding_image(ctx: commands.Context, url: str) -> None:
    if not url.startswith(("http://", "https://")):
        await ctx.send(embed=EmbedBuilder.error("Bad URL", "Image must be an http(s) URL."), ephemeral=True)
        return
    b = data_manager.get_branding(ctx.guild.id)
    b['embed_image'] = url
    b['updated_at'] = datetime.now(timezone.utc).isoformat()
    data_manager.save_branding(b)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Image Updated", "New embeds will use this image."
    ), ctx.guild.id))


@branding_group.command(name="banner", description="Set a banner image used in branding embeds")
@app_commands.describe(url="Direct image URL")
@commands.has_permissions(manage_guild=True)
async def branding_banner(ctx: commands.Context, url: str) -> None:
    if not url.startswith(("http://", "https://")):
        await ctx.send(embed=EmbedBuilder.error("Bad URL", "Banner must be an http(s) URL."), ephemeral=True)
        return
    b = data_manager.get_branding(ctx.guild.id)
    b['banner_url'] = url
    b['updated_at'] = datetime.now(timezone.utc).isoformat()
    data_manager.save_branding(b)
    await ctx.send(embed=EmbedBuilder.branded(EmbedBuilder.success(
        "Banner Updated", "Banner is shown on `!botbranding view`."
    ), ctx.guild.id))


@branding_group.command(name="avatar", description="Change the bot's actual avatar (owner only)")
@app_commands.describe(url="Direct image URL (png/jpg/gif)")
@commands.is_owner()
async def branding_avatar(ctx: commands.Context, url: str) -> None:
    if not url.startswith(("http://", "https://")):
        await ctx.send(embed=EmbedBuilder.error("Bad URL", "Avatar must be an http(s) URL."), ephemeral=True)
        return
    await ctx.defer()
    image_bytes = await _fetch_image_bytes(url)
    if not image_bytes:
        await ctx.send(embed=EmbedBuilder.error("Download Failed", "Could not fetch the image."), ephemeral=True)
        return
    try:
        await bot.user.edit(avatar=image_bytes)
        await ctx.send(embed=EmbedBuilder.success(
            "Bot Avatar Updated ✅",
            "The bot's avatar has been changed. It may take a moment to propagate in Discord.",
        ))
    except discord.HTTPException as exc:
        if 'You are being rate limited' in str(exc) or 'rate limit' in str(exc).lower():
            await ctx.send(embed=EmbedBuilder.error("Rate Limited", "Discord limits avatar changes to ~2/hour. Try again later."), ephemeral=True)
        else:
            await ctx.send(embed=EmbedBuilder.error("Avatar Update Failed", f"Discord rejected the image: `{exc}`"), ephemeral=True)
    except Exception as exc:
        await ctx.send(embed=EmbedBuilder.error("Avatar Update Failed", f"`{exc}`"), ephemeral=True)


@branding_group.command(name="name", description="Set the community name used by [GANG NAME] branding")
@app_commands.describe(name="The community/faction name (up to 60 chars)")
@commands.has_permissions(manage_guild=True)
async def branding_name(ctx: commands.Context, *, name: str) -> None:
    name = name.strip()[:60]
    if not name:
        await ctx.send(embed=EmbedBuilder.error("Invalid Name", "Please provide a non-empty name."))
        return
    old = config.gang_name
    config.gang_name = name
    try:
        config.save_branding_settings()
    except Exception as exc:
        config.gang_name = old
        await ctx.send(embed=EmbedBuilder.error("Save Failed", f"Could not persist the name: {exc}"))
        return
    await ctx.send(embed=EmbedBuilder.success(
        "Community Name Updated",
        f"Branding name set to **{name}**.\n`[GANG NAME]` placeholders now resolve to it everywhere."
    ))


@branding_group.command(name="view", description="View the current branding for this server")
@commands.has_permissions(manage_guild=True)
async def branding_view(ctx: commands.Context) -> None:
    b = data_manager.get_branding(ctx.guild.id)
    embed = discord.Embed(
        title=f"🎨 Branding — {ctx.guild.name}",
        description=(
            f"**Footer:** {b.get('embed_footer') or '_(not set)_'}\n"
            f"**Color:** {('#%06X' % b['embed_color']) if isinstance(b.get('embed_color'), int) else '_(not set)_'}\n"
            f"**Thumbnail:** [link]({b['embed_thumbnail']})" if b.get('embed_thumbnail') else "**Thumbnail:** _(not set)_"
        ),
        color=(discord.Color(b['embed_color']) if isinstance(b.get('embed_color'), int) else discord.Color.blurple()),
        timestamp=datetime.now(timezone.utc),
    )
    if b.get('embed_thumbnail'):
        embed.set_thumbnail(url=b['embed_thumbnail'])
    if b.get('embed_image'):
        embed.set_image(url=b['embed_image'])
    elif b.get('banner_url'):
        embed.set_image(url=b['banner_url'])
    await ctx.send(embed=embed)


@branding_group.command(name="clear", description="Reset all branding for this server")
@commands.has_permissions(manage_guild=True)
async def branding_clear(ctx: commands.Context) -> None:
    data_manager.save_branding({
        'guild_id': ctx.guild.id,
        'embed_footer': None, 'embed_color': None,
        'embed_thumbnail': None, 'embed_image': None,
        'avatar_url': None, 'banner_url': None,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })
    await ctx.send(embed=EmbedBuilder.success("Branding Cleared", "This server will use the bot's default styling."))


# =========================================================================
# EVENT HANDLERS for the four premium features
# =========================================================================

@bot.event
async def on_raw_reaction_add(payload: "discord.RawReactionActionEvent") -> None:
    # Ticket reaction panels first (Ticket Tool reaction-based panels).
    if instance_handles('ticket'):
        try:
            await handle_ticket_reaction_panel(payload)
        except Exception as exc:
            logging.exception(f"[ReactionPanel] on_raw_reaction_add error: {exc}")

    # Delegate to the extracted ReactionRoles package. The wiring hook is
    # fail-safe (it catches + logs internally); the outer guard mirrors the
    # pre-extraction behavior exactly.
    if RR_AVAILABLE and instance_handles('utility'):
        try:
            await ReactionRoles.wiring.on_raw_reaction_add(payload, bot)
        except Exception as exc:
            logging.exception(f"[RR] on_raw_reaction_add error: {exc}")


@bot.event
async def on_raw_reaction_remove(payload: "discord.RawReactionActionEvent") -> None:
    if RR_AVAILABLE and instance_handles('utility'):
        try:
            await ReactionRoles.wiring.on_raw_reaction_remove(payload, bot)
        except Exception as exc:
            logging.exception(f"[RR] on_raw_reaction_remove error: {exc}")


@bot.event
async def on_raw_reaction_clear(payload: "discord.RawReactionClearEvent") -> None:
    """When all reactions are cleared from a message, drop its RR mappings."""
    if RR_AVAILABLE and instance_handles('utility'):
        try:
            await ReactionRoles.wiring.on_raw_reaction_clear(payload, bot)
        except Exception as exc:
            logging.exception(f"[RR] on_raw_reaction_clear error: {exc}")


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if not instance_handles('mod'):
        return
    try:
        # Drop any reaction-role mappings tied to the deleted message.
        if RR_AVAILABLE:
            try:
                await ReactionRoles.wiring.on_message_delete(message.id, bot)
            except Exception as exc:
                logging.exception(f"[RR] on_message_delete error: {exc}")
        # Full message logging.
        await MessageLogSystem.log_delete(message)
    except Exception as exc:
        logging.exception(f"[MsgLog] on_message_delete error: {exc}")


@bot.event
async def on_raw_bulk_message_delete(payload: "discord.RawBulkMessageDeleteEvent") -> None:
    """Log mass message deletions.

    `TextChannel.purge()` and `delete_messages()` (used by `!purge`,
    `!purgeall`, and the verification auto-purge) fire this event ONCE for
    the whole batch instead of `on_message_delete` per message. Without
    this handler, mass-purges were invisible to the msg-log system.
    """
    if not instance_handles('mod'):
        return
    if not payload.message_ids:
        return

    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if guild is None:
        return
    channel = guild.get_channel_or_thread(payload.channel_id)
    if channel is None:
        return

    # Recover the message content we still have cached. `on_message` writes
    # a snapshot into `message_log_cache` for every message while msglog is
    # on, so we can reconstruct the batch even though Discord already
    # deleted it.
    cached_messages: List[Any] = []
    for mid in payload.message_ids:
        cached = data_manager.load_cached_message(mid)
        if not cached:
            continue
        author = None
        try:
            author = guild.get_member(int(cached['author_id']))
        except Exception:
            author = None
        # Minimal stub that MessageLogSystem.log_bulk_delete can render.
        stub = _BulkDeleteStub(
            message_id=mid,
            content=cached.get('content') or '',
            author=author,
            attachments=[],
        )
        cached_messages.append(stub)

    if cached_messages:
        try:
            await MessageLogSystem.log_bulk_delete(
                cached_messages,
                bot.user,
                channel,
                reason=f"bulk message delete ({len(payload.message_ids)} messages)",
            )
        except Exception as exc:
            logging.warning(f"[MsgLog] bulk-delete log failed: {exc}")
        return

    # Nothing cached: still log a summary so staff know a purge happened.
    try:
        cfg = MessageLogSystem.get_config(guild.id)
        if not cfg.get('enabled'):
            return
        log_channel = guild.get_channel(cfg.get('log_channel_id') or 0)
        if log_channel is None:
            return
        embed = discord.Embed(
            title=f"🧹 Bulk Delete — {len(payload.message_ids)} message(s)",
            description=(
                f"**Channel:** {channel.mention}\n"
                f"**Count:** {len(payload.message_ids)}\n"
                f"*(No cached content — messages were deleted too quickly or "
                f"before msglog cached them.)*"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        await log_channel.send(embed=embed)
    except Exception as exc:
        logging.debug(f"[MsgLog] bulk-delete summary failed: {exc}")


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if not instance_handles('mod'):
        return
    try:
        await MessageLogSystem.log_edit(before, after)
    except Exception as exc:
        logging.exception(f"[MsgLog] on_message_edit error: {exc}")


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    if instance_handles('utility'):
        try:
            StickyRoleSystem.capture_member_roles(member)
        except Exception as exc:
            logging.exception(f"[Sticky] on_member_remove error: {exc}")

    # --- PREMIUM TIER 1: fire 'owner_left' automations ---
    # For every open ticket owned by the leaving member, fire the automation
    # engine's owner_left trigger (which can auto-close, notify staff, etc.).
    if instance_handles('ticket') and PREMIUM_AVAILABLE and member.guild and getattr(bot, 'ticket_tool', None):
        try:
            pdb = getattr(bot, 'premium_db', None)
            if pdb is not None:
                open_tickets = data_manager.load_tickets_by_creator(member.id, member.guild.id)
                for t in open_tickets:
                    panel = data_manager.load_ticket_panel(t.get('panel_id') or '') if t.get('panel_id') else None
                    await TicketTool.wiring.on_owner_left(
                        bot=bot, ticket_tool=ticket_tool, ticket=t,
                        panel=panel, guild=member.guild,
                    )
        except Exception as exc:
            logging.warning(f"[Premium] on_member_remove owner_left failed: {exc}")

# --- ERROR HANDLING ---
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    # Domain-split bots: a command this instance doesn't own is another bot's
    # job — ignore it silently instead of erroring in the channel.
    if INSTANCE_DOMAIN != 'full' and isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        # Permission checks already produce friendly messages below; generic
        # CheckFailure (e.g. guild-only commands used in DMs) stays quiet.
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You don't have permission to use this command.")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing required argument.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Invalid argument provided.")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Cooldown. Try again in {error.retry_after:.0f} seconds.")
    elif isinstance(error, commands.CommandNotFound):
        # Full-bot mode: unknown commands stay quiet (typos shouldn't spam).
        return
    else:
        logging.error(f'Error: {str(error)}')
        await ctx.send(f"An error occurred: {str(error)}")





# --- GETALLROLES AUTO-UPDATE EVENTS ---
# Whenever a role is created, deleted, or its name changes, refresh every
# active getallroles embed in that guild so the lists stay live without the
# owner having to re-run the command.
@bot.event
async def on_guild_role_create(role: discord.Role) -> None:
    # Avoid running during startup before data_manager is ready.
    if 'data_manager' not in globals() or data_manager is None:
        return
    try:
        await refresh_getallroles_messages(role.guild)
    except Exception as e:
        logging.warning(f"[GetAllRoles] on_guild_role_create refresh failed: {e}")


@bot.event
async def on_guild_role_delete(role: discord.Role) -> None:
    if 'data_manager' not in globals() or data_manager is None:
        return
    try:
        await refresh_getallroles_messages(role.guild)
    except Exception as e:
        logging.warning(f"[GetAllRoles] on_guild_role_delete refresh failed: {e}")


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role) -> None:
    # Only refresh if something visible to the embed changed (name or
    # position). Other updates (permissions, color) don't affect the list.
    if before.name != after.name or before.position != after.position:
        if 'data_manager' not in globals() or data_manager is None:
            return
        try:
            await refresh_getallroles_messages(after.guild)
        except Exception as e:
            logging.warning(f"[GetAllRoles] on_guild_role_update refresh failed: {e}")


def build_getallroles_embed(guild: discord.Guild) -> discord.Embed:
    """Build the All-Roles embed for a guild.

    Shared between the command, the auto-update handlers, and the startup
    refresh so every code path renders the embed identically. All roles are
    packed into a single embed (auto-split into multiple code blocks if the
    list would exceed Discord's 4096-char description limit). No pagination.
    """
    roles = sorted(guild.roles, key=lambda r: r.position, reverse=True)

    lines: List[str] = []
    for role in roles:
        if role.is_default():
            lines.append(f"@everyone - {role.id}")
        else:
            lines.append(f"{role.name} - {role.id}")

    embed = discord.Embed(
        title=f"📋 All Roles in {guild.name}",
        description=f"**{len(roles)} role(s) total**",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    if not roles:
        embed.description = "No roles found in this server."
        return embed

    # Discord embed description max = 4096 chars. Each code block needs
    # ```\n ... \n``` wrappers (8 chars overhead). Pack as many roles as
    # possible into each block, splitting into multiple blocks if needed —
    # but always within a SINGLE embed (no pagination buttons).
    MAX_DESC = 4096
    CODE_FENCE_OVERHEAD = 8

    blocks: List[str] = []
    current_block: List[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len + CODE_FENCE_OVERHEAD > MAX_DESC - len(blocks) * 2 and current_block:
            blocks.append("```\n" + "\n".join(current_block) + "\n```")
            current_block = []
            current_len = 0
        current_block.append(line)
        current_len += line_len

    if current_block:
        blocks.append("```\n" + "\n".join(current_block) + "\n```")

    if len(blocks) == 1:
        embed.description = f"**{len(roles)} role(s) total**\n{blocks[0]}"
    else:
        embed.description = f"**{len(roles)} role(s) total** — split into {len(blocks)} blocks"
        for i, block in enumerate(blocks[:25]):  # Discord max 25 fields
            embed.add_field(
                name=f"Roles (part {i + 1}/{len(blocks)})",
                value=block,
                inline=False,
            )

    embed.set_footer(text="📋 Click Copy List to copy all roles • Auto-updates on role changes")
    return embed


def build_getallroles_plain_text(guild: discord.Guild) -> str:
    """Build a plain-text (no markdown) list of all roles + IDs.

    Used by the Copy List button so the ephemeral response is easy to
    select-all-and-copy. One role per line: `RoleName - ID`.
    """
    roles = sorted(guild.roles, key=lambda r: r.position, reverse=True)
    parts: List[str] = []
    for role in roles:
        if role.is_default():
            parts.append(f"@everyone - {role.id}")
        else:
            parts.append(f"{role.name} - {role.id}")
    return "\n".join(parts) if parts else "No roles found."


async def refresh_getallroles_messages(guild: discord.Guild) -> None:
    """Rebuild and edit every active getallroles embed for `guild`.

    Called from on_guild_role_create / on_guild_role_delete /
    on_guild_role_update so the lists stay live. Messages that no longer
    exist are pruned from the tracking table.
    """
    tracked = data_manager.load_getallroles_messages(guild_id=guild.id)
    if not tracked:
        return

    embed = build_getallroles_embed(guild)
    view = GetAllRolesView()

    for row in tracked:
        channel = guild.get_channel(row['channel_id'])
        if channel is None:
            # Channel was deleted — stop tracking this embed.
            data_manager.delete_getallroles_message(row['message_id'])
            continue
        try:
            message = await channel.fetch_message(row['message_id'])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Message was deleted or we lost access — stop tracking it.
            data_manager.delete_getallroles_message(row['message_id'])
            continue
        try:
            await message.edit(embed=embed, view=view)
        except (discord.HTTPException, discord.Forbidden):
            # Edit failed (e.g. channel now read-only). Leave it tracked;
            # a future role change will retry, or it'll be pruned if the
            # message is gone.
            pass


class GetAllRolesView(View):
    """Persistent view attached to every getallroles embed.

    Carries a single 'Copy List' button that DMs the clicker the full role
    list as plain text in a code block so they can select-all-and-copy.
    Persisted via bot.add_view() on startup so the button keeps working
    after a bot restart.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Copy List", style=discord.ButtonStyle.secondary, emoji="📋", custom_id="getallroles_copy")
    async def copy_button(self, interaction: discord.Interaction, button: Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return
        text = build_getallroles_plain_text(interaction.guild)

        # Discord message content limit is 2000 chars. Code fence adds 8.
        # If the list fits in one message, send it as a single code block.
        # Otherwise split across multiple ephemeral followups.
        MAX_MSG = 2000
        CODE_OVERHEAD = 8  # "```\n" + "\n```"
        CHUNK = MAX_MSG - CODE_OVERHEAD  # ~1992 chars per code block

        await interaction.response.defer(ephemeral=True)

        if len(text) <= CHUNK:
            await interaction.followup.send(f"```\n{text}\n```", ephemeral=True)
            return

        # Split on newline boundaries so we never cut a role line in half.
        lines = text.split("\n")
        chunks: List[str] = []
        current: List[str] = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 1
            if current_len + line_len > CHUNK and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len
        if current:
            chunks.append("\n".join(current))

        total = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            header = f"**All roles ({i}/{total})**\n" if total > 1 else ""
            await interaction.followup.send(f"{header}```\n{chunk}\n```", ephemeral=True)


@bot.command(name="getallroles", aliases=["roles", "listroles"], description="Get a list of all roles and their IDs (owner only)")
@commands.is_owner()
@commands.guild_only()
async def getallroles_cmd(ctx: commands.Context) -> None:
    """Display all roles in the server with their IDs on a single list.

    The embed is auto-updated whenever a role is created, deleted, or renamed
    in this server — no need to re-run the command. A persistent Copy List
    button sends the full list to your DMs (ephemeral) for easy copy-paste.
    The embed and button both survive a bot restart.
    """
    embed = build_getallroles_embed(ctx.guild)
    view = GetAllRolesView()
    message = await ctx.send(embed=embed, view=view)

    # Track the message so role-create/delete/update events can refresh it
    # and on_ready can re-attach the persistent view after a restart.
    try:
        data_manager.save_getallroles_message(message.id, ctx.channel.id, ctx.guild.id, ctx.author.id)
    except Exception as e:
        logging.warning(f"[GetAllRoles] Could not track message {message.id}: {e}")


# ------------------------------------------------------------------
# Owner-only command to re-send the setup tutorial on demand.
# ------------------------------------------------------------------
@bot.command(name="tutorial", description="Re-send the bot setup tutorial to your DMs (owner only)")
@commands.is_owner()
async def tutorial_cmd(ctx: commands.Context) -> None:
    """Re-send the full setup tutorial to the owner's DMs."""
    await ctx.send(embed=EmbedBuilder.success(
        "Tutorial Sent",
        "Check your DMs — the full setup tutorial is on its way.\n"
        "_(If you didn't receive it, your DMs may be closed. Enable DMs from server members and try again.)_"
    ), ephemeral=True)
    await send_owner_tutorial(force=True)
    logging.info(f"[Tutorial] Manually re-sent by owner {ctx.author}")





# =====================================================================
# MULTI-BOT DOMAIN TOKEN RESOLUTION
#
# tokens.txt (or .env) can define one token PER BOT, exactly like:
#     ModBot_Token=...      -> src/ModBot.py   (moderation)
#     TicketBot_Token=...   -> src/TicketBot.py (tickets)
#     UtilityBot_Token=...  -> src/UtilityBot.py (utility)
#
# A comma-separated list also works positionally:
#     BOT_TOKENS=modToken,ticketToken,utilityToken
# (assigned to Mod / Ticket / Utility in that order; extras are ignored).
# Extra tokens after a comma on a NAMED line are ignored (one bot per domain
# — two instances of the same domain would double-handle everything).
# =====================================================================

DOMAIN_ENTRY_FILES = {'mod': 'ModBot.py', 'ticket': 'TicketBot.py', 'utility': 'UtilityBot.py'}
DOMAIN_LABELS = {'mod': 'ModBot', 'ticket': 'TicketBot', 'utility': 'UtilityBot'}
_DOMAIN_ORDER = ('mod', 'ticket', 'utility')

_DOMAIN_TOKEN_KEYS = {
    'mod': ('ModBot_Token', 'ModBot_Tokens', 'MOD_BOT_TOKEN', 'MOD_BOT_TOKENS',
            'Mod_Token', 'MOD_TOKEN'),
    'ticket': ('TicketBot_Token', 'TicketBot_Tokens', 'TICKET_BOT_TOKEN',
               'TICKET_BOT_TOKENS', 'Ticket_Token', 'TICKET_TOKEN'),
    'utility': ('UtilityBot_Token', 'UtilityBot_Tokens', 'UTILITY_BOT_TOKEN',
                'UTILITY_BOT_TOKENS', 'Utility_Token', 'UTILITY_TOKEN'),
}


def _first_token(raw: Optional[str]) -> Optional[str]:
    """First valid token from a comma-separated value (placeholders/empties
    skipped, inline annotations after whitespace ignored); extra tokens are
    ignored with a warning."""
    cleaned = _clean_token_value(raw)
    if not cleaned:
        return None
    tokens = [t.strip() for t in cleaned.split(',') if t.strip()]
    valid = [t for t in tokens if not is_placeholder_token(t)]
    if len(valid) > 1:
        logging.warning(
            f"[Startup] Multiple tokens on one line ({len(valid)} found) — only the "
            f"first is used. Run ONE bot per domain; add separate bots as their own "
            f"domain entries instead.")
    return valid[0] if valid else None


def _resolve_domain_token_from_config(domain: str) -> Optional[str]:
    """Resolve a domain bot's token from .env/tokens.txt named variables."""
    load_local_env_file()
    for key in _DOMAIN_TOKEN_KEYS[domain]:
        for source in (os.environ.get(key), read_token_from_file(key)):
            token = _first_token(source)
            if token:
                return token
    return None


def _resolve_positional_tokens() -> Dict[str, str]:
    """Resolve the comma-separated BOT_TOKENS list into domain tokens
    (positional: mod, ticket, utility)."""
    load_local_env_file()
    raw = None
    for key in ('BOT_TOKENS', 'BOT_Tokens', 'All_Tokens'):
        raw = os.environ.get(key) or read_token_from_file(key)
        if raw:
            break
    if not raw:
        return {}
    tokens = [t.strip() for t in raw.split(',') if t.strip() and not is_placeholder_token(t)]
    result: Dict[str, str] = {}
    for domain, token in zip(_DOMAIN_ORDER, tokens):
        result[domain] = token
    if len(tokens) > len(_DOMAIN_ORDER):
        logging.warning(f"[Startup] BOT_TOKENS had {len(tokens)} tokens; only the first "
                        f"{len(_DOMAIN_ORDER)} are used (mod, ticket, utility).")
    return result


def resolve_domain_token(domain: str) -> Optional[str]:
    """A domain bot's token: named variable first, then the positional list."""
    if domain not in _DOMAIN_ORDER:
        return None
    return _resolve_domain_token_from_config(domain) or _resolve_positional_tokens().get(domain)


def resolve_deployed_domains() -> set:
    """Every domain (standard OR custom) that has a token configured — used by
    all instances to agree on who is the lead bot."""
    deployed = set()
    for domain in _DOMAIN_ORDER:
        if resolve_domain_token(domain):
            deployed.add(domain)
    deployed.update(resolve_custom_domains().keys())
    return deployed


def resolve_all_domain_tokens() -> Dict[str, str]:
    """{domain: token} for every configured domain bot."""
    return {d: resolve_domain_token(d) for d in _DOMAIN_ORDER if resolve_domain_token(d)}


# =====================================================================
# DOMAIN COMMAND PRUNING + LAUNCH
# =====================================================================

def _prune_commands_to_domain(domain: str) -> Dict[str, int]:
    """Remove every command NOT owned by `domain` from this instance.

    Prunes the prefix command registry (bot.remove_command), so a domain bot
    only answers its own commands."""
    removed_prefix = 0
    keep = {domain}

    # Prefix side (groups remove their subcommands).
    for cmd in list(bot.commands):
        if _command_domain(cmd.name) not in keep:
            try:
                bot.remove_command(cmd.name)
                removed_prefix += 1
            except Exception as exc:
                logging.warning(f"[Domain] prefix prune failed for {cmd.name}: {exc}")

    return {'prefix': removed_prefix}


def launch_domain_bot(domain: str) -> None:
    """Run THIS file's bot as a single-domain instance.

    Called by the thin entry files (ModBot.py / TicketBot.py /
    UtilityBot.py — and CustomBot.py for any custom bot defined via
    <Name>Bot_Token + <Name>Bot_Cmds). Prunes foreign-domain commands,
    activates only this domain's events + background tasks, and runs with
    the domain's token."""
    global INSTANCE_DOMAIN, ACTIVE_DOMAINS

    customs = resolve_custom_domains()
    is_custom = domain in customs
    if domain not in _DOMAIN_ORDER and not is_custom:
        known = ', '.join(_DOMAIN_ORDER) + ' (or any custom bot with a <Name>Bot_Token)'
        print(f"Unknown bot '{domain}'. Valid: {known}")
        exit(1)

    label = DOMAIN_LABELS.get(domain, domain.capitalize())
    if is_custom:
        token = customs[domain]['token']
    else:
        token = resolve_domain_token(domain)
    if not token:
        print("=" * 60)
        print(f"No token configured for the {label} bot!")
        print("Add one of these to .env or tokens.txt:")
        print(f"  {label}_Token=YOUR_TOKEN_HERE")
        print("(or a comma-separated BOT_TOKENS list: mod,ticket,utility)")
        print("=" * 60)
        exit(1)

    INSTANCE_DOMAIN = domain
    ACTIVE_DOMAINS = {domain}

    counts = _prune_commands_to_domain(domain)
    total_kept = len(bot.commands)
    if total_kept == 0:
        print("=" * 60)
        print(f"The {label} bot has no commands to run!")
        if is_custom:
            print(f"{label}_Token is set, but {label}_Cmds is missing or lists")
            print("only unknown command names. Add a command list, e.g.:")
            print(f"  {label}_Cmds=poll,rank,leaderboard")
            print("The listed commands are MOVED to this bot from the bot that")
            print("normally runs them (see the bottom of tokens.txt).")
        else:
            print(f"Check {label}_Cmds in tokens.txt — it may list only unknown commands.")
        print("=" * 60)
        exit(1)

    print("=" * 60)
    print(f"{label} — starting ({domain} domain)")
    print(f"Commands kept: {total_kept} "
          f"(pruned {counts['prefix']} commands from other domains)")
    print("=" * 60)
    logging.info(f"[Domain] {label} instance: kept {total_kept} commands, "
                 f"pruned {counts['prefix']} commands; "
                 f"lead instance: {_is_lead_instance()}")

    try:
        bot.run(token)
    finally:
        save_all_data()


# --- MAIN ENTRY POINT ---
def main() -> None:
    """Entry point: runs the single bot with all features."""
    token = get_bot_token()
    if token is None or is_placeholder_token(token):
        print("=" * 60)
        print("ERROR: Bot token not found!")
        print("=" * 60)
        print("Put your Discord bot token in one of these files:")
        print("  • .env          (project root)  ->  BOT_TOKEN=your-token")
        print("  • tokens.txt    (project root)  ->  BOT_Token=your-token")
        print("Tip: the committed .env.example is a ready-to-copy template:")
        print("  cp .env.example .env")
        print("Then run:  python src/bot.py")
        print("=" * 60)
        logging.error("Bot token not found.")
        exit(1)

    print("=" * 60)
    print(f"FactionBot {EDITION} Edition v{__version__} — starting")
    print(f"Configured for: {config.gang_name} (By IdkAnymore_039)")
    print("=" * 60)
    try:
        bot.run(token)
    except (discord.LoginFailure, discord.HTTPException) as exc:
        # Discord rejected the token — either leaked (auto-invalidated) or
        # copied incorrectly. Both 401 and LoginFailure surface here.
        print("=" * 60)
        print("LOGIN FAILED — Discord rejected the bot token.")
        print("=" * 60)
        print("The token loaded fine but Discord says it's invalid.")
        print("Most likely the token was leaked and auto-reset by Discord.")
        print()
        print("Generate a NEW token:")
        print("  https://discord.com/developers/applications")
        print("  -> your app -> Bot -> Reset Token")
        print()
        print("Paste it into .env or tokens.txt and run Bot.py again.")
        print("=" * 60)
        logging.error(f"[Startup] Login failed: {exc}")
        exit(1)
    finally:
        save_all_data()
        process_manager.clear_lock_file()


# Register the timing appliers now that `send_report_message` and
# `auto_blacklist_scan` have been defined at module level.
_register_timing_appliers()

if __name__ == "__main__":
    print(f"[Startup] Module loaded completely. {len(bot.commands)} commands registered. Starting main()...")
    main()