# -*- coding: utf-8 -*-
'''
ReactionRoles.db — schema + accessor for the reaction_roles table.

This is the EXACT same schema that previously lived inline in
DataManager._create_tables() in Bot.py. It is installed idempotently by
ReactionRoles.wiring.on_setup_hook() right after DataManager creates the base
tables, so:
  - existing databases are untouched (CREATE TABLE IF NOT EXISTS is a no-op),
  - new databases get the table on first run.

The accessor reuses the DataManager's sqlite3 connection + threading.Lock
(matching the conventions used by TicketTool.db.PremiumDB) so there is exactly
one writer path and no second connection is opened on the same .db file.
'''

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, List, Optional


# Carl-bot signature limit — free, up to 250 reaction-role mappings per guild.
MAX_REACTION_ROLES_PER_GUILD: int = 250


# =====================================================================
# SCHEMA INSTALL (idempotent)
# =====================================================================

def install_reaction_roles_schema(conn: sqlite3.Connection) -> None:
    '''Create the reaction_roles table + indexes if they don't yet exist.

    Safe to call repeatedly (CREATE TABLE IF NOT EXISTS). Called from
    ReactionRoles.wiring.on_setup_hook() after DataManager._create_tables().
    '''
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reaction_roles (
            mapping_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            role_id INTEGER NOT NULL,
            mode TEXT DEFAULT 'normal',
            description TEXT,
            created_by INTEGER,
            created_at TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_rr_guild ON reaction_roles (guild_id)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_rr_msg ON reaction_roles (message_id)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_rr_lookup ON reaction_roles (message_id, emoji)'
    )
    conn.commit()


# =====================================================================
# ACCESSOR
# =====================================================================

class ReactionRolesDB:
    '''Thin accessor over the reaction_roles table.

    Constructed once in ReactionRoles.wiring.on_setup_hook() and stashed on the
    bot as `bot.reaction_roles_db`. Mirrors the original DataManager methods
    byte-for-byte (same SQL, same lock discipline) so behavior is identical to
    the pre-extraction code.
    '''

    # Exposed as a class attribute too, for callers that prefer
    # `bot.reaction_roles_db.MAX_REACTION_ROLES_PER_GUILD`.
    MAX_REACTION_ROLES_PER_GUILD = MAX_REACTION_ROLES_PER_GUILD

    def __init__(self, data_manager) -> None:
        # Reuse the DataManager's connection + write lock — single writer
        # path, no second sqlite handle opened on the same .db file.
        self._connection = data_manager._connection
        self._lock = data_manager._lock

    def save_reaction_role(self, rr: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO reaction_roles
                (mapping_id, guild_id, channel_id, message_id, emoji, role_id,
                 mode, description, created_by, created_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                rr.get('mapping_id'),
                rr.get('guild_id'),
                rr.get('channel_id'),
                rr.get('message_id'),
                rr.get('emoji'),
                rr.get('role_id'),
                rr.get('mode', 'normal'),
                rr.get('description'),
                rr.get('created_by'),
                rr.get('created_at'),
                1 if rr.get('is_active', True) else 0,
            ))
            self._connection.commit()

    def count_reaction_roles(self, guild_id: int) -> int:
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT COUNT(*) as c FROM reaction_roles WHERE guild_id = ? AND is_active = 1',
            (guild_id,)
        )
        row = cursor.fetchone()
        return row['c'] if row else 0

    def load_reaction_roles_by_guild(self, guild_id: int) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT * FROM reaction_roles WHERE guild_id = ? AND is_active = 1 ORDER BY created_at',
            (guild_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def load_reaction_roles_by_message(self, message_id: int) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT * FROM reaction_roles WHERE message_id = ? AND is_active = 1',
            (message_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def find_reaction_role(self, message_id: int, emoji: str) -> Optional[Dict]:
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT * FROM reaction_roles WHERE message_id = ? AND emoji = ? AND is_active = 1',
            (message_id, emoji)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def delete_reaction_role(self, mapping_id: str) -> bool:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'DELETE FROM reaction_roles WHERE mapping_id = ?', (mapping_id,)
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def delete_reaction_roles_for_message(self, message_id: int) -> int:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'DELETE FROM reaction_roles WHERE message_id = ?', (message_id,)
            )
            self._connection.commit()
            return cursor.rowcount
