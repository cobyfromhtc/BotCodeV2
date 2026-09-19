# -*- coding: utf-8 -*-
"""FactionBot core (Short variant) — data layer, state and shared plumbing.

The bottom layer of the package graph. Hard layering rule: ``core/``
imports only the standard library at module level — never ``modules/``,
``utils/`` or ``packages/``. The sibling ``config`` package
(``config.settings`` / ``config.environment``) is re-exported here for
convenience.

Module map
----------
paths            Filesystem anchors ``_SRC_ROOT`` / ``_PROJECT_ROOT`` /
                 ``_DATA_DIR`` — every runtime path resolves from these.
data_manager     ``DataManager`` — SQLite persistence layer (read-through
                 caches + write-behind message buffer).
state            The ``config`` and ``data_manager`` singletons.
environment      (sibling package ``config``) .env / tokens.txt resolution +
                 placeholder detection.
settings         (sibling package ``config``) ``Config`` — persisted settings
                 (branding, channels, roles, timings, limits).

Canonical imports
-----------------
    from core import state                        # shared mutable state
    from core.state import config, data_manager   # singletons
    from core import DataManager                  # class (re-exported)
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
from core import state  # shared runtime singletons (see state.py docstring)

# Aligned with docs/CHANGELOG.md (Round 7 = the SaaS restructure,
# Round 8 = the SaaS quality pass, Round 9 = FactionAccess multi-guild
# licensing: !license / !request, per-guild identity, feature-bundle gating).
EDITION = "Short"
__version__ = "5.2.0"

__all__ = [
    "__version__", "EDITION", "state",
    "_HERE", "_PROJECT_ROOT", "_SRC_ROOT", "_DATA_DIR",
    "get_bot_token", "is_placeholder_token", "load_local_env_file",
    "read_token_from_file",
    "Config", "DataManager",
]
