# -*- coding: utf-8 -*-
"""Lifecycle — save_all_data, reset, JSON→SQLite import.
Cross-domain imports are deferred to call time to stay cycle-free."""

# stdlib + discord.py
import json
import logging
import os

from core import state  # shared mutable runtime state
from core.state import config, data_manager



def save_all_data() -> None:
    """Save all persistent data to SQLite.

    Tickets persist in SQLite via data_manager (written on every ticket event),
    so there is no separate tickets save step here. Giveaways are intentionally
    in-memory/JSON and reset on restart.
    """
    # If the database was never connected — e.g. the bot exited before
    # setup_hook ran (invalid token / login failure) — there is nothing to
    # persist and the in-memory caches were never hydrated. Skip cleanly
    # instead of crashing on a None-cursor access in the save helpers below.
    from modules.factions.rules import save_rules_cache  # deferred import (cycle-safe)
    from modules.engagement.leveling import save_levels_data  # deferred import (cycle-safe)
    from modules.moderation.moderation import save_blacklist_data  # deferred import (cycle-safe)
    if not data_manager.is_connected():
        logging.info("[DataManager] Database not connected; skipping shutdown save.")
        return
    if state.invite_manager:
        state.invite_manager.save_data()
    save_blacklist_data()
    save_rules_cache()
    save_levels_data()
    logging.info("[DataManager] All persistent data saved to SQLite.")


def reset_temporary_data() -> None:
    """Reset temporary in-memory data (giveaways) on bot shutdown.

    Tickets are persisted in SQLite and are intentionally NOT reset here.
    """
    from modules.engagement.giveaways import reset_giveaways_data  # deferred import (cycle-safe)
    reset_giveaways_data()
    logging.info("[DataManager] Temporary data (giveaways) reset.")


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
    
    # === IMPORT LEVELS ===
    if os.path.exists('levels_data.json'):
        try:
            with open('levels_data.json', 'r') as f:
                data = json.load(f)
            
            count = 0
            for key, value in data.items():
                parts = key.split('_')
                if len(parts) == 2:
                    user_id = int(parts[0])
                    guild_id = int(parts[1])
                    data_manager.save_level(
                        user_id, guild_id,
                        value.get('xp', 0),
                        value.get('level', 0),
                        value.get('total_messages', 0),
                        value.get('last_xp_gain')
                    )
                    count += 1
            
            # Rename to .bak
            os.rename('levels_data.json', 'levels_data.json.bak')
            
            logging.info(f"[Import] Imported {count} user level(s) from levels_data.json")
            imported_something = True
        except Exception as e:
            logging.error(f"[Import] Error importing levels: {e}")
    
    # === IMPORT RULES CACHE ===
    if os.path.exists('rules_cache.json'):
        try:
            with open('rules_cache.json', 'r') as f:
                data = json.load(f)
            
            data_manager.save_rules_cache(data)
            
            # Rename to .bak
            os.rename('rules_cache.json', 'rules_cache.json.bak')
            
            logging.info(f"[Import] Imported rules cache from rules_cache.json")
            imported_something = True
        except Exception as e:
            logging.error(f"[Import] Error importing rules cache: {e}")
    
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

    # === IMPORT GIVEAWAYS ===
    if os.path.exists(config.giveaways_data_file):
        try:
            with open(config.giveaways_data_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for g_id, g_data in data.items():
                    g_data['giveaway_id'] = g_id
                    data_manager.save_giveaway(g_data)
                os.rename(config.giveaways_data_file, f"{config.giveaways_data_file}.bak")
                logging.info(f"[Import] Imported {len(data)} giveaway(s) to SQLite")
                imported_something = True
        except Exception as e:
            logging.error(f"[Import] Error importing giveaways: {e}")
    
    if imported_something:
        logging.info("[Import] JSON import complete! Old files renamed to .bak")
    else:
        logging.info("[Import] No JSON files found to import")
