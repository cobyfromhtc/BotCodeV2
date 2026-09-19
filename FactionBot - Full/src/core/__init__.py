# -*- coding: utf-8 -*-
"""FactionBot core — configuration, data layer, state, and shared plumbing.

The bottom layer of the package graph. Hard layering rule (enforced by
review, tested by import): ``core/`` imports only the standard library and
discord.py at module level — never ``modules/``, ``utils/`` or
``packages/``. Cross-layer references from core to feature code are
always deferred, function-local imports (see ``core.lifecycle`` and
``core.ows``).

Module map
----------
paths            Filesystem anchors ``_SRC_ROOT`` / ``_PROJECT_ROOT`` /
                 ``_DATA_DIR``. Every runtime path (DB, logs, JSON data)
                 resolves from these, so the bot works regardless of the
                 CWD it is launched from.
environment      (sibling package ``config``) .env / tokens.txt resolution +
                 placeholder detection — re-exported below.
settings         (sibling package ``config``) ``Config`` — persisted settings
                 (branding, channels, roles, timings, limits); loaded from
                 SQLite during setup_hook — re-exported below.
data_manager     ``DataManager`` — SQLite persistence layer with read-through
                 caches and a write-behind message buffer (hot paths never
                 touch the disk synchronously).
state            Process-wide shared runtime state. The ``config`` and
                 ``data_manager`` singletons live here, alongside the
                 cross-module rebound state (``bot``, ``invite_manager``…).
helpers          Leveling math, ``brand_text``, uptime, audit logging,
                 guild-scoped hybrid command decorators.
models           Dataclasses & enums (Ticket, Giveaway, Warning, UserLevel…).
ows              Owner-Warning-Settings — 60+ feature toggles persisted in
                 SQLite; hydration happens in setup_hook.
lifecycle        save_all_data / reset_temporary_data / JSON→SQLite import,
                 invoked from the signal handler and on_ready.
process_manager  Single-instance lock file + verification busy-registry.
domains          Command-domain routing (custom domains, moved commands).

Canonical imports
-----------------
    from core import state                        # shared mutable state
    from core.state import config, data_manager   # singletons
    from core import Config, DataManager          # classes (re-exported)
    from core import save_all_data               # lifecycle helpers (re-exported)
"""
from core.paths import _DATA_DIR, _HERE, _PROJECT_ROOT, _SRC_ROOT
from config.environment import (
    get_bot_token,
    is_placeholder_token,
    load_local_env_file,
    read_token_from_file,
)
from config.settings import Config
from core.data_manager import DataManager
from core import state  # shared mutable runtime state (see state.py docstring)
from core.helpers import (
    PREMIUM_AVAILABLE,
    RR_AVAILABLE,
    brand_text,
    compute_level_from_xp,
    get_uptime,
    log_event,
    xp_for_level,
    xp_for_next_level,
)
from core.models import (
    Giveaway,
    GiveawayStatus,
    Ticket,
    TicketStatus,
    UserLevel,
    VerificationStatus,
    Warning,
    WarningType,
)
from core.lifecycle import import_json_to_sqlite, reset_temporary_data, save_all_data
from core.ows import (
    get_owner_setting,
    hydrate_ows_settings,
    ows_bulk_set_category,
    ows_get,
    ows_set,
    set_owner_setting,
)
from core.process_manager import ProcessManager
from core.domains import (
    TICKET_DOMAIN_COMMANDS,
    commands_owned_by,
    custom_domain_names,
    domain_active,
    instance_handles,
    resolve_custom_domains,
)

# Aligned with CHANGELOG.md (Round 4 = the SaaS package split).
__version__ = "4.0.0"

__all__ = [
    # package surface
    "__version__", "state",
    # paths — filesystem anchors
    "_HERE", "_PROJECT_ROOT", "_SRC_ROOT", "_DATA_DIR",
    # environment (from config.environment)
    "get_bot_token", "is_placeholder_token", "load_local_env_file", "read_token_from_file",
    # config / data layer (classes; singletons live on core.state)
    "Config", "DataManager",
    # helpers
    "PREMIUM_AVAILABLE", "RR_AVAILABLE", "brand_text", "compute_level_from_xp",
    "get_uptime", "log_event", "xp_for_level", "xp_for_next_level",
    # models
    "Giveaway", "GiveawayStatus", "Ticket", "TicketStatus", "UserLevel",
    "VerificationStatus", "Warning", "WarningType",
    # lifecycle
    "import_json_to_sqlite", "reset_temporary_data", "save_all_data",
    # ows — owner warning settings
    "get_owner_setting", "hydrate_ows_settings", "ows_bulk_set_category",
    "ows_get", "ows_set", "set_owner_setting",
    # process management
    "ProcessManager",
    # command domains
    "TICKET_DOMAIN_COMMANDS", "commands_owned_by", "custom_domain_names",
    "domain_active", "instance_handles", "resolve_custom_domains",
]
