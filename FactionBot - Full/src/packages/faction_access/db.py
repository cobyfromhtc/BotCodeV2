# -*- coding: utf-8 -*-
'''
FactionAccess.db — schema + accessor for the faction_* tables.

Four tables, all created idempotently by ``FactionAccess.wiring.on_setup_hook``
right after the DataManager base tables exist:

    faction_guilds    one row per guild the bot knows about (license state)
    faction_features  per-guild feature-bundle grants
    faction_identity  per-guild gang tag / gang name / bot display nickname
    faction_audit     append-only audit trail of every licensing action

Package settings (home guild id, license-authority allowlist) live in the
shared ``bot_config`` key/value table under the ``faction_access`` key, using
the same ``get_config_value`` / ``set_config_value`` methods the Config
facade uses — no new global-config mechanism is invented.

The accessor reuses the DataManager's sqlite3 connection + threading.Lock
(matching TicketTool.db / ReactionRoles.db conventions) so there is exactly
one writer path and no second connection is opened on the same .db file.
'''

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional

# bot_config key holding this package's settings JSON.
SETTINGS_KEY: str = "faction_access"

# Valid license states (faction_guilds.status).
STATUS_HOME: str = "home"
STATUS_PENDING: str = "pending"
STATUS_LICENSED: str = "licensed"
STATUS_SUSPENDED: str = "suspended"
STATUS_REVOKED: str = "revoked"
STATUS_LEFT: str = "left"
ALL_STATUSES: tuple = (
    STATUS_HOME, STATUS_PENDING, STATUS_LICENSED,
    STATUS_SUSPENDED, STATUS_REVOKED, STATUS_LEFT,
)


# =====================================================================
# SCHEMA INSTALL (idempotent)
# =====================================================================

def install_faction_access_schema(conn: sqlite3.Connection) -> None:
    '''Create the faction_* tables + indexes if they don't yet exist.

    Safe to call repeatedly (CREATE TABLE IF NOT EXISTS). Called from
    FactionAccess.wiring.on_setup_hook() after DataManager._create_tables().
    '''
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS faction_guilds (
            guild_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            licensed_by INTEGER,
            licensed_at TEXT,
            expires_at TEXT,
            notes TEXT,
            joined_at TEXT,
            updated_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS faction_features (
            guild_id INTEGER NOT NULL,
            feature TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            granted_by INTEGER,
            granted_at TEXT,
            PRIMARY KEY (guild_id, feature)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS faction_identity (
            guild_id INTEGER PRIMARY KEY,
            gang_tag TEXT,
            gang_name TEXT,
            display_name TEXT,
            updated_by INTEGER,
            updated_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS faction_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor_id INTEGER,
            action TEXT NOT NULL,
            guild_id INTEGER,
            detail TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_fa_guilds_status ON faction_guilds (status)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_fa_audit_ts ON faction_audit (ts)'
    )
    conn.commit()


# =====================================================================
# ACCESSOR
# =====================================================================

class FactionAccessDB:
    '''Thin accessor over the faction_* tables + the settings KV entry.

    Constructed once in FactionAccess.wiring.on_setup_hook() and stashed on
    the bot as ``bot.faction_access_db``. Read paths go through small
    in-memory caches hydrated by ``hydrate()``; every write updates the cache
    in the same critical section as the SQL so the cache can never drift.
    '''

    def __init__(self, data_manager) -> None:
        # Reuse the DataManager's connection + write lock — single writer
        # path, no second sqlite handle opened on the same .db file.
        self._connection = data_manager._connection
        self._lock = data_manager._lock
        # Read-through caches (hydrated once at setup, maintained on write).
        self._settings: Dict[str, Any] = {}
        self._guilds: Dict[int, Dict] = {}
        self._features: Dict[int, Dict[str, bool]] = {}
        self._identity: Dict[int, Dict] = {}

    # ------------------------------------------------------------------
    # Settings (bot_config KV: {"home_guild_id": int, "authority": [ids]})
    # ------------------------------------------------------------------

    def hydrate(self) -> Dict[str, Any]:
        '''Load settings + every guild/feature/identity row into the caches.'''
        with self._lock:
            cursor = self._connection.cursor()
            self._hydrate_settings(cursor)
            self._guilds = {}
            cursor.execute(
                'SELECT guild_id, status, licensed_by, licensed_at, expires_at,'
                ' notes, joined_at, updated_at FROM faction_guilds'
            )
            for row in cursor.fetchall():
                self._guilds[int(row['guild_id'])] = dict(row)
            self._features = {}
            cursor.execute(
                'SELECT guild_id, feature, enabled FROM faction_features'
            )
            for row in cursor.fetchall():
                self._features.setdefault(int(row['guild_id']), {})[row['feature']] = bool(row['enabled'])
            self._identity = {}
            cursor.execute(
                'SELECT guild_id, gang_tag, gang_name, display_name, updated_by,'
                ' updated_at FROM faction_identity'
            )
            for row in cursor.fetchall():
                self._identity[int(row['guild_id'])] = dict(row)
        return dict(self._settings)

    def _hydrate_settings(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute('SELECT value FROM bot_config WHERE key = ?', (SETTINGS_KEY,))
        row = cursor.fetchone()
        settings: Dict[str, Any] = {}
        if row is not None and row['value']:
            try:
                loaded = json.loads(row['value'])
                if isinstance(loaded, dict):
                    settings = loaded
            except (TypeError, ValueError):
                logging.warning("[faction_access] settings payload was not valid JSON; using defaults")
        self._settings = settings

    def get_settings(self) -> Dict[str, Any]:
        return dict(self._settings)

    def save_settings(self, settings: Dict[str, Any]) -> None:
        '''Persist the settings JSON under the faction_access bot_config key.'''
        payload = json.dumps(settings, ensure_ascii=False)
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)',
                (SETTINGS_KEY, payload),
            )
            self._connection.commit()
            self._settings = dict(settings)

    # ------------------------------------------------------------------
    # Guild license rows
    # ------------------------------------------------------------------

    def upsert_guild(self, guild_id: int, **fields: Any) -> Dict:
        '''Insert or update a faction_guilds row; returns the stored row.

        Recognized field names match the table columns exactly; unknown
        fields raise TypeError (typos must never silently no-op).
        '''
        allowed = {'status', 'licensed_by', 'licensed_at', 'expires_at',
                   'notes', 'joined_at', 'updated_at'}
        unknown = set(fields) - allowed
        if unknown:
            raise TypeError(f"upsert_guild: unknown field(s): {sorted(unknown)}")
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'SELECT guild_id FROM faction_guilds WHERE guild_id = ?', (guild_id,)
            )
            exists = cursor.fetchone() is not None
            if exists:
                if fields:
                    assignments = ', '.join(f"{name} = ?" for name in fields)
                    cursor.execute(
                        f'UPDATE faction_guilds SET {assignments} WHERE guild_id = ?',
                        (*fields.values(), guild_id),
                    )
            else:
                columns = ['guild_id'] + list(fields)
                placeholders = ', '.join('?' for _ in columns)
                cursor.execute(
                    f'INSERT INTO faction_guilds ({", ".join(columns)}) VALUES ({placeholders})',
                    (guild_id, *fields.values()),
                )
            self._connection.commit()
            cursor.execute(
                'SELECT guild_id, status, licensed_by, licensed_at, expires_at,'
                ' notes, joined_at, updated_at FROM faction_guilds WHERE guild_id = ?',
                (guild_id,),
            )
            row = cursor.fetchone()
            stored = dict(row) if row is not None else {'guild_id': guild_id, 'status': 'pending'}
        self._guilds[int(guild_id)] = dict(stored)
        return dict(stored)

    def get_guild(self, guild_id: int) -> Optional[Dict]:
        row = self._guilds.get(int(guild_id))
        return dict(row) if row is not None else None

    def list_guilds(self, status: Optional[str] = None) -> List[Dict]:
        rows = list(self._guilds.values())
        if status is not None:
            rows = [r for r in rows if r.get('status') == status]
        rows.sort(key=lambda r: (r.get('status') or '', r.get('guild_id') or 0))
        return [dict(r) for r in rows]

    def guilds_with_expiry_due(self, now_iso: str) -> List[Dict]:
        '''Licensed rows whose expires_at is set and strictly before now_iso.'''
        due = []
        for row in self._guilds.values():
            expires = row.get('expires_at')
            if row.get('status') == STATUS_LICENSED and expires and str(expires) < now_iso:
                due.append(dict(row))
        due.sort(key=lambda r: str(r.get('expires_at')))
        return due

    # ------------------------------------------------------------------
    # Feature grants
    # ------------------------------------------------------------------

    def set_feature(self, guild_id: int, feature: str, enabled: bool,
                    granted_by: Optional[int], granted_at: str) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO faction_features
                    (guild_id, feature, enabled, granted_by, granted_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (int(guild_id), feature, 1 if enabled else 0, granted_by, granted_at))
            self._connection.commit()
        self._features.setdefault(int(guild_id), {})[feature] = bool(enabled)

    def get_features(self, guild_id: int) -> Dict[str, bool]:
        return dict(self._features.get(int(guild_id), {}))

    def drop_features(self, guild_id: int) -> int:
        '''Remove every feature row for a guild (license revocation).'''
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('SELECT COUNT(*) AS c FROM faction_features WHERE guild_id = ?', (int(guild_id),))
            count = (cursor.fetchone() or {'c': 0})['c']
            cursor.execute('DELETE FROM faction_features WHERE guild_id = ?', (int(guild_id),))
            self._connection.commit()
        self._features.pop(int(guild_id), None)
        return int(count or 0)

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def save_identity(self, guild_id: int, gang_tag: Optional[str],
                      gang_name: Optional[str], display_name: Optional[str],
                      updated_by: Optional[int], updated_at: str) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO faction_identity
                    (guild_id, gang_tag, gang_name, display_name, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (int(guild_id), gang_tag, gang_name, display_name, updated_by, updated_at))
            self._connection.commit()
        self._identity[int(guild_id)] = {
            'guild_id': int(guild_id), 'gang_tag': gang_tag, 'gang_name': gang_name,
            'display_name': display_name, 'updated_by': updated_by, 'updated_at': updated_at,
        }

    def get_identity(self, guild_id: int) -> Optional[Dict]:
        row = self._identity.get(int(guild_id))
        return dict(row) if row is not None else None

    def delete_identity(self, guild_id: int) -> bool:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM faction_identity WHERE guild_id = ?', (int(guild_id),))
            removed = cursor.rowcount > 0
            self._connection.commit()
        self._identity.pop(int(guild_id), None)
        return removed

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def add_audit(self, ts: str, actor_id: Optional[int], action: str,
                  guild_id: Optional[int], detail: Optional[str] = None) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT INTO faction_audit (ts, actor_id, action, guild_id, detail)
                VALUES (?, ?, ?, ?, ?)
            ''', (ts, actor_id, action, guild_id, detail))
            self._connection.commit()

    def list_audit(self, guild_id: Optional[int] = None, limit: int = 20) -> List[Dict]:
        '''Most-recent-first audit entries, optionally filtered by guild.'''
        with self._lock:
            cursor = self._connection.cursor()
            if guild_id is None:
                cursor.execute(
                    'SELECT id, ts, actor_id, action, guild_id, detail FROM faction_audit'
                    ' ORDER BY id DESC LIMIT ?', (int(limit),)
                )
            else:
                cursor.execute(
                    'SELECT id, ts, actor_id, action, guild_id, detail FROM faction_audit'
                    ' WHERE guild_id = ? ORDER BY id DESC LIMIT ?', (int(guild_id), int(limit))
                )
            rows = cursor.fetchall()
        return [dict(row) for row in reversed(rows)]
