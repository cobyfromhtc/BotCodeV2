# -*- coding: utf-8 -*-
"""Process-wide singletons for the Short variant.

The ``config`` and ``data_manager`` singletons live here (created exactly
once, never rebound — from-import safe). The application state that IS
rebound at runtime (``bot``, ``ticket_tool``, …) stays in ``src/bot.py``
where the rebinding happens; feature modules receive everything through
bot attributes (``bot.fb_config`` / ``bot.fb_data_manager``), never by
importing the entry module.
"""

from config.settings import Config
from core.data_manager import DataManager

# --- GLOBAL INSTANCE ---
config = Config()
# Ensure the data directories exist BEFORE DataManager.connect() opens the
# SQLite file. Previously this was never called, so a fresh checkout (with
# no `data/` directory present) crashed on the first sqlite3.connect() with
# "unable to open database file".
config.ensure_directories()
data_manager = DataManager(config.db_file)