# -*- coding: utf-8 -*-
"""Process-wide singletons + shared mutable runtime state.

Holds the Config/DataManager singletons plus cross-module mutable state.
Two access rules:

* **from-import safe** — dicts/sets that are only ever MUTATED in place
  (``config``, ``data_manager``, ``verification_flags``, …). Import the
  name directly.
* **state-attribute access** — names that other modules REBIND at runtime
  (``bot``, ``ticket_tool``, ``invite_manager``, ``messages_enabled``,
  ``blacklisted_keywords``, ``rules_cache``, ``giveaways_data``,
  ``levels_data``). Always import this module (``from core import state``)
  and access via ``state.<name>`` so rebinding stays visible everywhere.
"""
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from config.settings import Config
from core.data_manager import DataManager

# ── singletons (from-import safe) ───────────────────────────────────────────
config = Config()
data_manager = DataManager(config.db_file)

# ── shared runtime state (from-import safe — mutated in place only) ─────────
verification_flags: Dict[int, bool] = {}
verification_cooldowns: Dict[int, float] = {}
start_time: float = time.time()
warnings_data: Dict[int, List[Dict]] = {}  # guild_id -> list of warnings
# NOTE: tickets are persisted entirely in SQLite via data_manager (no in-memory cache).

# ── cross-module REBOUND state — access via state.<name> ────────────────────
messages_enabled: bool = True
invite_manager: Optional[Any] = None
blacklisted_keywords: Set[str] = set()
rules_cache: Dict[str, Any] = {
    'gang_rules': '', 'server_rules': '',
    'gang_last_updated': None, 'server_last_updated': None,
}
giveaways_data: Dict[str, Dict] = {}       # giveaway_id -> giveaway data
levels_data: Dict[Tuple[int, int], Dict] = {}  # (user_id, guild_id) -> level data
# bot instance — assigned by Bot.py right after creation, BEFORE any cog code runs
bot: Optional[Any] = None
# ticket engine instance — created in on_ready (needs guilds cached)
ticket_tool: Optional[Any] = None
# FactionAccess licensing service — created in setup_hook by
# packages.faction_access.wiring.on_setup_hook. Lower layers (utils.ui.embeds,
# runtime events) resolve per-guild faction identity through it via
# ``getattr(state, 'faction_access', None)`` so layering stays intact.
faction_access: Optional[Any] = None
