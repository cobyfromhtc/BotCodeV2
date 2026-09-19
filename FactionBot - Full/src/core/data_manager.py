# -*- coding: utf-8 -*-
"""DataManager — the SQLite data layer (tickets, warnings, giveaways,
levels, blacklist, rules, branding, message-log config).

Perf notes (Round 3): read-through caches on the hottest queries,
write-behind buffer for ticket messages, generation-guarded installs."""

# stdlib + discord.py
import json
import logging
import pickle
import sqlite3
import threading
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple




# --- SQLITE DATA MANAGER (For persistent data - saves PC resources) ---
# Sentinel distinguishing "cached negative result" (None) from "not cached"
# in read-through caches below.
_CACHE_MISS = object()


class DataManager:
    """
    SQLite-based data manager for persistent data.
    More efficient than JSON for frequent read/write operations.
    Stores: invites, blacklist, rules_cache, levels, warnings
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        # --- PERFORMANCE (Phase 2): in-memory read caches -----------------
        # These four reads dominated per-message/per-command SQLite traffic;
        # they are now served from memory and invalidated by the matching
        # save_*/atomic_*/purge writers (_invalidate_read_caches):
        #   * load_ticket_by_channel() — ran on EVERY gateway message
        #     (positive AND negative entries cached: most channels are not
        #     ticket channels)
        #   * get_message_log_config() — ran on EVERY logged message
        #   * load_ticket_settings()  — ran on every ticket flow + loop tick
        #   * get_branding()          — ran on every branded embed build
        # All reads return COPIES so callers may safely mutate + re-save.
        # The generation counter closes the read-modify-install race against
        # worker threads: a read that started before a write refuses to
        # install its (now stale) result.
        self._read_caches_generation: int = 0
        self._channel_ticket_cache: Dict[int, Optional[Dict]] = {}
        self._msglog_config_cache: Dict[int, Dict] = {}
        self._ticket_settings_cache: Dict[int, Optional[Dict]] = {}
        self._branding_cache: Dict[int, Dict] = {}
        # --- PERFORMANCE (Phase 2): message-log write-behind buffer -------
        # message_log_cache rows were committed one-per-message; they are now
        # buffered and committed in a single transaction (see cache_message).
        self._msg_cache_lock = threading.Lock()
        self._msg_cache_buffer: List[Tuple] = []
        self._msg_cache_wake = threading.Event()
        self._msg_cache_flusher: Optional[threading.Thread] = None
        self._msg_cache_flusher_stop = threading.Event()

    def _invalidate_read_caches(self) -> None:
        """Drop every in-memory read cache entry (called after cached-table writes).

        Called by EVERY method that writes to `tickets`, `ticket_settings`,
        `message_log_config` or `bot_branding` (save_ticket, the atomic_*
        transitions, first-response / SLA markers, category updates, the
        three config savers, and purge_stale_data). Those writes are rare
        compared to message events, so a full clear is cheaper and safer than
        per-entry bookkeeping. Bumping the generation also prevents a
        concurrent reader from installing a stale query result afterwards.
        """
        self._read_caches_generation += 1
        self._channel_ticket_cache.clear()
        self._msglog_config_cache.clear()
        self._ticket_settings_cache.clear()
        self._branding_cache.clear()

    def connect(self) -> None:
        # Guard against the on_ready reconnect case: discord.py can fire
        # on_ready again after a gateway reconnect, and without this guard a
        # second sqlite3.Connection would be opened (leaking the first one and
        # leaving _create_tables() to run again). If we're already connected,
        # the tables already exist — nothing to do.
        if self._connection is not None:
            logging.info("[DataManager] SQLite connection already established; reusing it.")
            return
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # Multi-process safety (domain bots share this SQLite file):
        # WAL lets concurrent bot processes read/write without lock errors,
        # and busy_timeout makes writers wait instead of failing instantly.
        try:
            self._connection.execute('PRAGMA journal_mode=WAL')
            self._connection.execute('PRAGMA busy_timeout=5000')
            self._connection.execute('PRAGMA synchronous=NORMAL')
        except Exception as exc:
            logging.warning(f"[DataManager] Could not apply WAL pragmas: {exc}")
        self._create_tables()
        # PERFORMANCE (Phase 2): start the message-log write-behind flusher
        # thread. Daemon so it never blocks interpreter exit; close() stops
        # it with a final drain so no buffered rows are lost on shutdown.
        if self._msg_cache_flusher is None or not self._msg_cache_flusher.is_alive():
            self._msg_cache_flusher_stop.clear()
            self._msg_cache_flusher = threading.Thread(
                target=self._msg_cache_flush_loop,
                name="DataManager-msglog-flush",
                daemon=True,
            )
            self._msg_cache_flusher.start()
        logging.info(f"[DataManager] Connected to database: {self.db_path}")

    def is_connected(self) -> bool:
        """Return True iff connect() has been called and the connection is live.

        Used by the shutdown save path (save_all_data) so it can skip cleanly
        when the bot exits before setup_hook ever ran — e.g. a login failure
        from an invalid token — instead of crashing on a None-cursor access.
        """
        return self._connection is not None

    def _create_tables(self) -> None:
        cursor = self._connection.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER,
                channel_id INTEGER,
                tracked_invites BLOB
            )
        ''')

        # Archive of previous invite batches, so the "View Previous Invites"
        # button can show a historical record after a regenerate/expire cycle.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invite_history (
                history_id TEXT PRIMARY KEY,
                archived_at TEXT NOT NULL,
                archived_by INTEGER,
                invite_code TEXT NOT NULL,
                invite_name TEXT,
                max_uses INTEGER DEFAULT 0,
                final_uses INTEGER DEFAULT 0,
                status TEXT DEFAULT 'expired',
                created_at TEXT,
                guild_id INTEGER
            )
        ''')
        # Migration: older databases may not have invite_history yet — the
        # CREATE TABLE IF NOT EXISTS above already handles that, so nothing
        # else to do here.

        # Tracks every active !getallroles embed so we can auto-update them
        # when roles are created/deleted/renamed, and re-attach the persistent
        # "Copy List" button after a bot restart.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS getallroles_messages (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                created_by INTEGER,
                created_at TEXT NOT NULL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS blacklist (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                keywords BLOB
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rules_cache (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                gang_rules TEXT,
                server_rules TEXT,
                gang_last_updated TEXT,
                server_last_updated TEXT
            )
        ''')

        # Migration: older databases may lack gang_rules / server_rules columns
        cursor.execute("PRAGMA table_info(rules_cache)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        if 'gang_rules' not in existing_cols:
            cursor.execute('ALTER TABLE rules_cache ADD COLUMN gang_rules TEXT')
        if 'server_rules' not in existing_cols:
            cursor.execute('ALTER TABLE rules_cache ADD COLUMN server_rules TEXT')
        if 'gang_last_updated' not in existing_cols:
            cursor.execute('ALTER TABLE rules_cache ADD COLUMN gang_last_updated TEXT')
        if 'server_last_updated' not in existing_cols:
            cursor.execute('ALTER TABLE rules_cache ADD COLUMN server_last_updated TEXT')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS levels (
                user_id INTEGER,
                guild_id INTEGER,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 0,
                total_messages INTEGER DEFAULT 0,
                last_xp_gain TEXT,
                PRIMARY KEY (user_id, guild_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS warnings (
                warning_id TEXT PRIMARY KEY,
                user_id INTEGER,
                guild_id INTEGER,
                moderator_id INTEGER,
                warning_type TEXT,
                reason TEXT,
                points INTEGER DEFAULT 1,
                created_at TEXT,
                expires_at TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')

        # =============================================================================
        # TICKET TOOL TABLES (Full Ticket Tool Clone)
        # =============================================================================

        # Ticket Panels - Store panel configurations
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_panels (
                panel_id TEXT PRIMARY KEY,
                guild_id INTEGER,
                channel_id INTEGER,
                message_id INTEGER,
                name TEXT,
                description TEXT,
                embed_title TEXT,
                embed_description TEXT,
                embed_color INTEGER,
                embed_image TEXT,
                embed_thumbnail TEXT,
                button_style INTEGER,
                button_label TEXT,
                button_emoji TEXT,
                category_id INTEGER,
                support_role_id INTEGER,
                ticket_limit INTEGER DEFAULT 1,
                auto_close_hours INTEGER DEFAULT 24,
                welcome_message TEXT,
                claim_required INTEGER DEFAULT 0,
                created_at TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')

        # Tickets - Store individual ticket data
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id TEXT PRIMARY KEY,
                guild_id INTEGER,
                channel_id INTEGER,
                panel_id TEXT,
                creator_id INTEGER,
                category TEXT,
                subject TEXT,
                claimed_by INTEGER,
                claimed_at TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT,
                closed_at TEXT,
                closed_by INTEGER,
                close_reason TEXT,
                rating INTEGER,
                rating_feedback TEXT
            )
        ''')

        # Ticket Transcripts - Store transcript data
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_transcripts (
                transcript_id TEXT PRIMARY KEY,
                ticket_id TEXT,
                guild_id INTEGER,
                channel_id INTEGER,
                creator_id INTEGER,
                closed_by INTEGER,
                claimed_by INTEGER,
                category TEXT,
                created_at TEXT,
                closed_at TEXT,
                message_count INTEGER,
                file_path TEXT,
                html_content TEXT
            )
        ''')

        # Ticket Blacklist - Users blocked from creating tickets
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_blacklist (
                blacklist_id TEXT PRIMARY KEY,
                guild_id INTEGER,
                user_id INTEGER,
                reason TEXT,
                blacklisted_by INTEGER,
                blacklisted_at TEXT,
                expires_at TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')

        # Ticket Settings - Per-guild ticket settings
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_settings (
                guild_id INTEGER PRIMARY KEY,
                category_id INTEGER,
                transcripts_channel_id INTEGER,
                log_channel_id INTEGER,
                support_role_id INTEGER,
                admin_role_id INTEGER,
                max_tickets_per_user INTEGER DEFAULT 3,
                auto_close_hours INTEGER DEFAULT 24,
                mention_on_create INTEGER DEFAULT 1,
                dm_transcripts INTEGER DEFAULT 1,
                require_claim INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
        ''')

        # Ticket Questions - Custom questions for panels
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_questions (
                question_id TEXT PRIMARY KEY,
                panel_id TEXT,
                guild_id INTEGER,
                question_text TEXT,
                question_type TEXT DEFAULT 'text',
                required INTEGER DEFAULT 1,
                placeholder TEXT,
                order_index INTEGER,
                created_at TEXT
            )
        ''')

        # Ticket Answers - Store user answers to questions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_answers (
                answer_id TEXT PRIMARY KEY,
                ticket_id TEXT,
                question_id TEXT,
                user_id INTEGER,
                answer_text TEXT,
                answered_at TEXT
            )
        ''')

        # Ticket Messages - Track messages for transcripts
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_messages (
                message_id INTEGER PRIMARY KEY,
                ticket_id TEXT,
                author_id INTEGER,
                author_name TEXT,
                author_avatar TEXT,
                content TEXT,
                attachments TEXT,
                created_at TEXT
            )
        ''')

        # Ticket Notes - Private staff-only notes per ticket
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_notes (
                note_id TEXT PRIMARY KEY,
                ticket_id TEXT,
                guild_id INTEGER,
                author_id INTEGER,
                content TEXT,
                created_at TEXT
            )
        ''')

        # Reaction Panels (TicketTool reaction-based panels): message_id +
        # emoji → panel routing for reaction-opened tickets.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reaction_panels (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                channel_id INTEGER,
                title TEXT,
                mapping TEXT,
                created_at TEXT
            )
        ''')

        # Multi-Panels (TicketTool "Attached Panels" / Dropdown Style) — one
        # message combining up to 25 panels as buttons or a select menu.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS multi_panels (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                channel_id INTEGER,
                style TEXT DEFAULT 'buttons',
                panel_ids TEXT,
                per_row INTEGER DEFAULT 5,
                placeholder TEXT,
                created_at TEXT
            )
        ''')

        # Ticket Categories (internal "folders" for grouping related tickets
        # — NOT Discord channel categories). Panels reference a category via
        # ticket_panels.ticket_category_id; tickets inherit it at creation and
        # store it in tickets.ticket_category_id. Deleting a category NULLs
        # those references (tickets fall back to "Uncategorized"), never
        # deleting ticket data or Discord channels.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ticket_categories (
                category_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                emoji TEXT,
                created_by INTEGER,
                created_at TEXT,
                updated_at TEXT
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_ticket_categories_guild '
            'ON ticket_categories (guild_id)'
        )
        # Hot-path indexes: load_ticket_by_channel() runs on EVERY message
        # (on_message ticket detection) and previously did a full table scan.
        # load_tickets_by_guild() feeds the SLA/auto-close sweeps and
        # load_tickets_by_creator() runs on every ticket-open attempt.
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_tickets_channel '
            'ON tickets (channel_id)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_tickets_guild_status '
            'ON tickets (guild_id, status)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_tickets_creator_status '
            'ON tickets (creator_id, status)'
        )

        # === MIGRATIONS: add new columns to existing tables safely ===
        migrations = [
            'ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT "normal"',
            'ALTER TABLE tickets ADD COLUMN first_response_at TEXT',
            'ALTER TABLE ticket_settings ADD COLUMN sla_hours INTEGER DEFAULT 0',
            'ALTER TABLE ticket_panels ADD COLUMN sla_hours INTEGER DEFAULT 0',
            # TicketTool-style two-step close (retain closed channel with a
            # moderator message instead of deleting), per-panel limit bypass
            # roles, ticket logging config, staff-notes channel, and the
            # category closed tickets are moved to.
            'ALTER TABLE ticket_panels ADD COLUMN two_step_ticket INTEGER DEFAULT 0',
            'ALTER TABLE ticket_panels ADD COLUMN limit_bypass_role_ids TEXT',
            'ALTER TABLE ticket_settings ADD COLUMN notes_channel_id INTEGER',
            'ALTER TABLE ticket_settings ADD COLUMN closed_category_id INTEGER',
            'ALTER TABLE ticket_settings ADD COLUMN log_events TEXT',
            # TicketTool Limit Options: closed-ticket limits (checked at
            # creation to prevent open/close cycling) and the all-users open
            # ticket cap.
            'ALTER TABLE ticket_settings ADD COLUMN max_closed_tickets_per_user INTEGER DEFAULT 0',
            'ALTER TABLE ticket_settings ADD COLUMN max_open_tickets_all INTEGER DEFAULT 0',
            # TicketTool /pause + /resume: per-ticket automation kill switch
            # (paused flag, when it started, optional auto-resume deadline, and
            # the resume timestamp used as the auto-close activity baseline).
            'ALTER TABLE tickets ADD COLUMN automation_paused INTEGER DEFAULT 0',
            'ALTER TABLE tickets ADD COLUMN automation_paused_at TEXT',
            'ALTER TABLE tickets ADD COLUMN automation_paused_until TEXT',
            'ALTER TABLE tickets ADD COLUMN automation_resumed_at TEXT',
            # TicketTool /private + /unprivate: standalone private-ticket state.
            'ALTER TABLE tickets ADD COLUMN is_private INTEGER DEFAULT 0',
            # Ticket Categories (internal ticket folders): the category a panel
            # assigns to its tickets, and the category stored on each ticket.
            # Existing rows keep NULL = "Uncategorized" (backwards compatible).
            'ALTER TABLE ticket_panels ADD COLUMN ticket_category_id TEXT',
            'ALTER TABLE tickets ADD COLUMN ticket_category_id TEXT',
            # Legacy SLA loop warn-once tracking. It used to fake
            # first_response_at to avoid re-warning, which corrupted response
            # analytics. This dedicated column keeps analytics truthful.
            'ALTER TABLE tickets ADD COLUMN sla_warned_at TEXT',
        ]
        for sql in migrations:
            try:
                cursor.execute(sql)
            except Exception:
                pass  # Column already exists - safe to ignore

        # BOT CONFIG (Branding & Channel settings - replaces JSON files)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        # GIVEAWAYS (Persistent giveaways - replaces giveaways_data.json)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS giveaways (
                giveaway_id TEXT PRIMARY KEY,
                message_id INTEGER,
                channel_id INTEGER,
                guild_id INTEGER,
                host_id INTEGER,
                prize TEXT,
                winner_count INTEGER,
                entries TEXT,
                winners TEXT,
                status TEXT,
                created_at TEXT,
                ends_at TEXT
            )
        ''')

        # === TEMP MUTES (persist across restarts) ===
        # Stores active temp-mutes so they survive a bot restart. A background
        # task polls for expired rows and unmutes the users; this is the source
        # of truth (NOT an in-memory asyncio.sleep), so mutes can never get
        # "stuck forever" if the bot restarts.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS temp_mutes (
                mute_id TEXT PRIMARY KEY,
                guild_id INTEGER,
                user_id INTEGER,
                role_id INTEGER,
                moderator_id INTEGER,
                reason TEXT,
                muted_at TEXT,
                unmute_at TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_temp_mutes_unmute ON temp_mutes (unmute_at)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_temp_mutes_active ON temp_mutes (is_active)'
        )

        # =============================================================================
        # STICKY ROLES (Dyno premium style — re-apply roles on rejoin)
        # =============================================================================
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sticky_roles (
                entry_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_ids TEXT NOT NULL,
                saved_at TEXT,
                UNIQUE (guild_id, user_id)
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_sticky_lookup ON sticky_roles (guild_id, user_id)'
        )
        # Per-guild sticky-role config (which roles are eligible to be sticky)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sticky_role_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                eligible_role_ids TEXT,
                updated_at TEXT
            )
        ''')

        # =============================================================================
        # FULL MESSAGE LOGGING (Dyno premium style — edits + deletes with content)
        # =============================================================================
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_log_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                log_channel_id INTEGER,
                log_edits INTEGER DEFAULT 1,
                log_deletes INTEGER DEFAULT 1,
                ignore_bots INTEGER DEFAULT 1,
                ignore_channels TEXT,
                updated_at TEXT
            )
        ''')
        # Cache of recently-seen message content so we can log the *original*
        # text when a message is edited or deleted (Discord doesn't always give
        # us the before-content on delete).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_log_cache (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                channel_id INTEGER,
                author_id INTEGER,
                author_name TEXT,
                content TEXT,
                attachments TEXT,
                created_at TEXT
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_mlcache_chan ON message_log_cache (channel_id)'
        )

        # =============================================================================
        # CUSTOM BOT BRANDING (avatar / banner / embed footer override)
        # =============================================================================
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_branding (
                guild_id INTEGER PRIMARY KEY,
                embed_footer TEXT,
                embed_color INTEGER,
                embed_thumbnail TEXT,
                embed_image TEXT,
                avatar_url TEXT,
                banner_url TEXT,
                updated_at TEXT
            )
        ''')

        # =====================================================================
        # OWS PERSISTENT PANEL (remembers the open !ows menu on restart)
        # =====================================================================
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ows_active_panel (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                current_category TEXT
            )
        ''')

        # =====================================================================
        # NO-PURGE EXCLUSIONS (verification auto-purge protection)
        # Message IDs marked with !nopurge are never deleted by the auto-purge
        # system, !purge, or !purgeall. Stored per-channel so a message ID is
        # unambiguous, and survived across restarts (SQLite, not in-memory).
        # =====================================================================
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS no_purge_messages (
                message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                marked_by INTEGER NOT NULL,
                marked_at TEXT,
                PRIMARY KEY (channel_id, message_id)
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_nopurge_channel ON no_purge_messages (channel_id)'
        )

        self._connection.commit()

    def close(self) -> None:
        # Stop the message-log flusher FIRST so its final batch commits
        # before the connection goes away (flush loop drains on exit).
        if self._msg_cache_flusher is not None and self._msg_cache_flusher.is_alive():
            self._msg_cache_flusher_stop.set()
            self._msg_cache_wake.set()
            self._msg_cache_flusher.join(timeout=5.0)
        if self._connection:
            self._connection.close()
            logging.info("[DataManager] Database connection closed")
        # In-memory caches belong to the closed connection — drop them.
        self._invalidate_read_caches()

    # =========================================================================
    # TEMP MUTES (persistent across restarts)
    # =========================================================================
    def save_temp_mute(self, mute: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO temp_mutes
                (mute_id, guild_id, user_id, role_id, moderator_id, reason,
                 muted_at, unmute_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                mute.get('mute_id'),
                mute.get('guild_id'),
                mute.get('user_id'),
                mute.get('role_id'),
                mute.get('moderator_id'),
                mute.get('reason'),
                mute.get('muted_at'),
                mute.get('unmute_at'),
                1 if mute.get('is_active', True) else 0,
            ))
            self._connection.commit()

    def get_temp_mute(self, mute_id: str) -> Optional[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM temp_mutes WHERE mute_id = ?', (mute_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def deactivate_temp_mute(self, mute_id: str) -> bool:
        """Mark a temp-mute as no longer active. Returns True if a row was updated."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'UPDATE temp_mutes SET is_active = 0 WHERE mute_id = ? AND is_active = 1',
                (mute_id,),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def load_active_temp_mutes(self) -> List[Dict]:
        """All temp-mutes that are still active (regardless of expiry)."""
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM temp_mutes WHERE is_active = 1')
        return [dict(row) for row in cursor.fetchall()]

    def load_expired_temp_mutes(self, now_iso: str) -> List[Dict]:
        """Active temp-mutes whose unmute_at has passed."""
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT * FROM temp_mutes WHERE is_active = 1 AND unmute_at <= ?',
            (now_iso,),
        )
        return [dict(row) for row in cursor.fetchall()]

    # === INVITES ===
    def save_invites(self, message_id: Optional[int], channel_id: Optional[int], tracked_invites: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM invites')
            cursor.execute(
                'INSERT INTO invites (message_id, channel_id, tracked_invites) VALUES (?, ?, ?)',
                (message_id, channel_id, pickle.dumps(tracked_invites))
            )
            self._connection.commit()

    def load_invites(self) -> Tuple[Optional[int], Optional[int], Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT message_id, channel_id, tracked_invites FROM invites LIMIT 1')
        row = cursor.fetchone()
        if row:
            tracked = pickle.loads(row['tracked_invites']) if row['tracked_invites'] else {}
            return row['message_id'], row['channel_id'], tracked
        return None, None, {}

    # === INVITE HISTORY (for the "View Previous Invites" button) ===
    def archive_invite_batch(self, invites: Dict[str, Dict[str, Any]], guild_id: Optional[int], archived_by: Optional[int]) -> int:
        """Persist a snapshot of the current tracked invites into history.

        Called when invites are regenerated (or fully expire) so the user can
        browse previous batches later. Returns the number of rows written.
        """
        if not invites:
            return 0
        archived_at = datetime.now(timezone.utc).isoformat()
        rows = []
        for code, data in invites.items():
            rows.append((
                str(_uuid.uuid4())[:8],
                archived_at,
                archived_by,
                code,
                data.get('name'),
                int(data.get('max_uses', 0) or 0),
                int(data.get('uses', 0) or 0),
                data.get('status', 'expired'),
                data.get('created_at'),
                guild_id,
            ))
        with self._lock:
            cursor = self._connection.cursor()
            cursor.executemany(
                '''INSERT INTO invite_history
                   (history_id, archived_at, archived_by, invite_code, invite_name,
                    max_uses, final_uses, status, created_at, guild_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                rows,
            )
            self._connection.commit()
        return len(rows)

    def load_invite_history(self, guild_id: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
        """Return invite history rows, newest first."""
        cursor = self._connection.cursor()
        if guild_id is not None:
            cursor.execute(
                'SELECT * FROM invite_history WHERE guild_id = ? ORDER BY archived_at DESC, rowid DESC LIMIT ?',
                (guild_id, limit),
            )
        else:
            cursor.execute(
                'SELECT * FROM invite_history ORDER BY archived_at DESC, rowid DESC LIMIT ?',
                (limit,),
            )
        return [dict(r) for r in cursor.fetchall()]

    def load_invite_history_grouped(self, guild_id: Optional[int] = None, limit: int = 500) -> List[Dict[str, Any]]:
        """Return invite history grouped by archived_at timestamp (newest batch first).

        Each group is a dict: { 'archived_at': str, 'archived_by': int|None, 'invites': [row, ...] }.
        Pagination works on this grouped list (one page = one archived batch).
        """
        rows = self.load_invite_history(guild_id=guild_id, limit=limit)
        groups: List[Dict[str, Any]] = []
        by_ts: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            ts = r.get('archived_at') or 'unknown'
            grp = by_ts.get(ts)
            if grp is None:
                grp = {'archived_at': ts, 'archived_by': r.get('archived_by'), 'invites': []}
                by_ts[ts] = grp
                groups.append(grp)
            grp['invites'].append(r)
        return groups

    def clear_invite_history(self, guild_id: Optional[int] = None) -> int:
        with self._lock:
            cursor = self._connection.cursor()
            if guild_id is not None:
                cursor.execute('DELETE FROM invite_history WHERE guild_id = ?', (guild_id,))
            else:
                cursor.execute('DELETE FROM invite_history')
            self._connection.commit()
            return cursor.rowcount

    # === GETALLROLES MESSAGE TRACKING (auto-updating role list embeds) ===
    def save_getallroles_message(self, message_id: int, channel_id: int, guild_id: int, created_by: Optional[int]) -> None:
        """Record that a getallroles embed was posted, so role-create/delete
        events can find and update it, and the persistent Copy button can be
        re-attached after a restart."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO getallroles_messages
                   (message_id, channel_id, guild_id, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?)''',
                (message_id, channel_id, guild_id, created_by, datetime.now(timezone.utc).isoformat()),
            )
            self._connection.commit()

    def load_getallroles_messages(self, guild_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return tracked getallroles embed rows. If guild_id is given, only
        rows for that guild are returned."""
        cursor = self._connection.cursor()
        if guild_id is not None:
            cursor.execute(
                'SELECT * FROM getallroles_messages WHERE guild_id = ? ORDER BY created_at DESC',
                (guild_id,),
            )
        else:
            cursor.execute('SELECT * FROM getallroles_messages ORDER BY created_at DESC')
        return [dict(r) for r in cursor.fetchall()]

    def delete_getallroles_message(self, message_id: int) -> None:
        """Stop tracking a getallroles embed (e.g. the message was deleted)."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM getallroles_messages WHERE message_id = ?', (message_id,))
            self._connection.commit()

    # === BLACKLIST ===
    def save_blacklist(self, keywords: Set[str]) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM blacklist')
            cursor.execute('INSERT INTO blacklist (id, keywords) VALUES (1, ?)', (pickle.dumps(keywords),))
            self._connection.commit()

    def load_blacklist(self) -> Set[str]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT keywords FROM blacklist WHERE id = 1')
        row = cursor.fetchone()
        return pickle.loads(row['keywords']) if row and row['keywords'] else set()

    # === RULES CACHE ===
    def save_rules_cache(self, cache: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM rules_cache')
            cursor.execute('''
                INSERT INTO rules_cache (id, gang_rules, server_rules, gang_last_updated, server_last_updated)
                VALUES (1, ?, ?, ?, ?)
            ''', (
                cache.get('gang_rules', ''),
                cache.get('server_rules', ''),
                cache.get('gang_last_updated'),
                cache.get('server_last_updated')
            ))
            self._connection.commit()

    @staticmethod
    def _fix_mojibake(text: str) -> str:
        """
        Fix text that was stored as UTF-8 but read/saved as cp1252 (Windows encoding bug).
        Converts garbled characters like 'âž¢' back to their correct form '➢'.
        Safe to call on already-correct text - it will return it unchanged.
        """
        if not text:
            return text
        try:
            return text.encode('cp1252').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text  # Already correct UTF-8, leave it alone

    def load_rules_cache(self) -> Dict:
        cursor = self._connection.cursor()
        cursor.execute('SELECT gang_rules, server_rules, gang_last_updated, server_last_updated FROM rules_cache WHERE id = 1')
        row = cursor.fetchone()
        if row:
            gang_rules = self._fix_mojibake(row['gang_rules'] or '')
            server_rules = self._fix_mojibake(row['server_rules'] or '')
            self.save_rules_cache({
                'gang_rules': gang_rules,
                'server_rules': server_rules,
                'gang_last_updated': row['gang_last_updated'],
                'server_last_updated': row['server_last_updated']
            })
            return {
                'gang_rules': gang_rules,
                'server_rules': server_rules,
                'gang_last_updated': row['gang_last_updated'],
                'server_last_updated': row['server_last_updated']
            }
        return {'gang_rules': '', 'server_rules': '', 'gang_last_updated': None, 'server_last_updated': None}

    # === LEVELS ===
    def save_level(self, user_id: int, guild_id: int, xp: int, level: int, total_messages: int, last_xp_gain: Optional[datetime]) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO levels (user_id, guild_id, xp, level, total_messages, last_xp_gain)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, guild_id, xp, level, total_messages, last_xp_gain.isoformat() if last_xp_gain else None))
            self._connection.commit()

    def load_level(self, user_id: int, guild_id: int) -> Optional[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT xp, level, total_messages, last_xp_gain FROM levels WHERE user_id = ? AND guild_id = ?',
                      (user_id, guild_id))
        row = cursor.fetchone()
        if row:
            return {
                'xp': row['xp'],
                'level': row['level'],
                'total_messages': row['total_messages'],
                'last_xp_gain': datetime.fromisoformat(row['last_xp_gain']) if row['last_xp_gain'] else None
            }
        return None

    def load_all_levels(self) -> Dict[Tuple[int, int], Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT user_id, guild_id, xp, level, total_messages, last_xp_gain FROM levels')
        result = {}
        for row in cursor.fetchall():
            result[(row['user_id'], row['guild_id'])] = {
                'xp': row['xp'],
                'level': row['level'],
                'total_messages': row['total_messages'],
                'last_xp_gain': datetime.fromisoformat(row['last_xp_gain']) if row['last_xp_gain'] else None
            }
        return result

    def save_all_levels(self, levels: Dict[Tuple[int, int], Dict]) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            for (user_id, guild_id), data in levels.items():
                cursor.execute('''
                    INSERT OR REPLACE INTO levels (user_id, guild_id, xp, level, total_messages, last_xp_gain)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    user_id, guild_id,
                    data.get('xp', 0),
                    data.get('level', 0),
                    data.get('total_messages', 0),
                    data['last_xp_gain'].isoformat() if data.get('last_xp_gain') else None
                ))
            self._connection.commit()

    # === WARNINGS ===
    def save_warning(self, warning: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO warnings
                (warning_id, user_id, guild_id, moderator_id, warning_type, reason, points, created_at, expires_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                warning.get('warning_id'),
                warning.get('user_id'),
                warning.get('guild_id'),
                warning.get('moderator_id'),
                warning.get('warning_type'),
                warning.get('reason'),
                warning.get('points', 1),
                warning.get('created_at'),
                warning.get('expires_at'),
                1 if warning.get('is_active', True) else 0
            ))
            self._connection.commit()

    def load_warnings(self, guild_id: Optional[int] = None) -> Dict[int, List[Dict]]:
        cursor = self._connection.cursor()
        if guild_id:
            cursor.execute('SELECT * FROM warnings WHERE guild_id = ? AND is_active = 1', (guild_id,))
        else:
            cursor.execute('SELECT * FROM warnings WHERE is_active = 1')

        result: Dict[int, List[Dict]] = {}
        for row in cursor.fetchall():
            user_id = row['user_id']
            if user_id not in result:
                result[user_id] = []
            result[user_id].append({
                'warning_id': row['warning_id'],
                'user_id': row['user_id'],
                'guild_id': row['guild_id'],
                'moderator_id': row['moderator_id'],
                'warning_type': row['warning_type'],
                'reason': row['reason'],
                'points': row['points'],
                'created_at': row['created_at'],
                'expires_at': row['expires_at'],
                'is_active': bool(row['is_active'])
            })
        return result

    def delete_warning(self, warning_id: str) -> bool:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('UPDATE warnings SET is_active = 0 WHERE warning_id = ?', (warning_id,))
            self._connection.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # TICKET TOOL METHODS
    # =========================================================================

    def _table_columns(self, table: str) -> List[str]:
        """Return the column names actually present in `table`.

        Used by save_ticket/save_ticket_panel so their INSERT OR REPLACE
        statements include EVERY column that exists (base + premium package
        migrations + our own migrations). Previously the fixed column lists
        silently reset every non-listed column to its default on each save
        (e.g. wiping use_threads/flow_id/priority/first_response_at).
        """
        cursor = self._connection.cursor()
        cursor.execute(f'PRAGMA table_info({table})')
        return [row['name'] for row in cursor.fetchall()]

    # === TICKET PANELS ===
    def save_ticket_panel(self, panel: Dict) -> None:
        # Every known panel column. Columns that don't exist yet in this
        # database (e.g. premium columns before the TicketTool package has
        # installed its schema) are filtered out by _table_columns.
        fields = {
            'panel_id': panel.get('panel_id'),
            'guild_id': panel.get('guild_id'),
            'channel_id': panel.get('channel_id'),
            'message_id': panel.get('message_id'),
            'name': panel.get('name'),
            'description': panel.get('description'),
            'embed_title': panel.get('embed_title'),
            'embed_description': panel.get('embed_description'),
            'embed_color': panel.get('embed_color', 0x5865F2),
            'embed_image': panel.get('embed_image'),
            'embed_thumbnail': panel.get('embed_thumbnail'),
            'button_style': panel.get('button_style', 3),
            'button_label': panel.get('button_label', 'Create Ticket'),
            'button_emoji': panel.get('button_emoji'),
            'category_id': panel.get('category_id'),
            'support_role_id': panel.get('support_role_id'),
            'ticket_limit': panel.get('ticket_limit', 1),
            'auto_close_hours': panel.get('auto_close_hours', 24),
            'welcome_message': panel.get('welcome_message'),
            'claim_required': panel.get('claim_required', 0),
            'created_at': panel.get('created_at'),
            'is_active': panel.get('is_active', 1),
            # Premium package columns (only persisted when the premium schema
            # has been installed, so base-only databases keep working).
            'naming_template': panel.get('naming_template'),
            'schedule_enabled': panel.get('schedule_enabled'),
            'use_threads': panel.get('use_threads'),
            'thread_parent_channel_id': panel.get('thread_parent_channel_id'),
            'allow_user_invite_in_thread': panel.get('allow_user_invite_in_thread'),
            'create_staff_thread': panel.get('create_staff_thread'),
            'recycle_channels': panel.get('recycle_channels'),
            'flow_id': panel.get('flow_id'),
            'branded_replies_enabled': panel.get('branded_replies_enabled'),
            'branded_webhook_id': panel.get('branded_webhook_id'),
            'use_multi_embed': panel.get('use_multi_embed'),
            # TicketTool-style columns owned by Bot.py migrations.
            'two_step_ticket': panel.get('two_step_ticket', 0),
            'limit_bypass_role_ids': panel.get('limit_bypass_role_ids'),
            # Ticket Category (internal folder) assigned to this panel's tickets.
            'ticket_category_id': panel.get('ticket_category_id'),
        }
        with self._lock:
            cursor = self._connection.cursor()
            cols = [c for c in fields if c in self._table_columns('ticket_panels')]
            col_list = ', '.join(cols)
            placeholders = ', '.join('?' for _ in cols)
            cursor.execute(
                f'INSERT OR REPLACE INTO ticket_panels ({col_list}) VALUES ({placeholders})',
                [fields[c] for c in cols],
            )
            self._connection.commit()

    def load_ticket_panel(self, panel_id: str) -> Optional[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_panels WHERE panel_id = ?', (panel_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def load_ticket_panels_by_guild(self, guild_id: int) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_panels WHERE guild_id = ? AND is_active = 1', (guild_id,))
        return [dict(row) for row in cursor.fetchall()]

    def load_ticket_panel_by_message(self, message_id: int) -> Optional[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_panels WHERE message_id = ?', (message_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def delete_ticket_panel(self, panel_id: str) -> bool:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('UPDATE ticket_panels SET is_active = 0 WHERE panel_id = ?', (panel_id,))
            self._connection.commit()
            return cursor.rowcount > 0

    # === TICKET CATEGORIES (internal ticket "folders") ===
    def save_ticket_category(self, category: Dict) -> None:
        """Insert or update a ticket-category row."""
        fields = {
            'category_id': category.get('category_id'),
            'guild_id': category.get('guild_id'),
            'name': category.get('name'),
            'description': category.get('description'),
            'emoji': category.get('emoji'),
            'created_by': category.get('created_by'),
            'created_at': category.get('created_at'),
            'updated_at': category.get('updated_at'),
        }
        with self._lock:
            cursor = self._connection.cursor()
            cols = ', '.join(fields.keys())
            placeholders = ', '.join('?' for _ in fields)
            cursor.execute(
                f'INSERT OR REPLACE INTO ticket_categories ({cols}) VALUES ({placeholders})',
                list(fields.values()),
            )
            self._connection.commit()

    def load_ticket_category(self, category_id: str) -> Optional[Dict]:
        if not category_id:
            return None
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_categories WHERE category_id = ?', (category_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def load_ticket_categories(self, guild_id: int) -> List[Dict]:
        """All ticket categories in a guild, ordered by name."""
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT * FROM ticket_categories WHERE guild_id = ? ORDER BY name COLLATE NOCASE',
            (guild_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def load_ticket_category_by_name(self, guild_id: int, name: str) -> Optional[Dict]:
        """Case-insensitive duplicate check for category names."""
        if not name:
            return None
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT * FROM ticket_categories WHERE guild_id = ? AND name = ? COLLATE NOCASE',
            (guild_id, name.strip()),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def count_tickets_in_category(self, guild_id: int, category_id: str) -> int:
        """How many tickets currently reference this category."""
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND ticket_category_id = ?',
            (guild_id, category_id),
        )
        return int(cursor.fetchone()[0] or 0)

    def count_panels_in_category(self, guild_id: int, category_id: str) -> int:
        """How many active panels currently reference this category."""
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT COUNT(*) FROM ticket_panels WHERE guild_id = ? AND ticket_category_id = ? '
            'AND is_active = 1',
            (guild_id, category_id),
        )
        return int(cursor.fetchone()[0] or 0)

    def delete_ticket_category(self, category_id: str) -> bool:
        """Delete a ticket category and safely detach its references.

        Tickets and panels that referenced the category fall back to NULL
        ("Uncategorized"). Ticket rows are otherwise untouched — no channel
        deletions, no data loss. Discord channel categories are a separate
        concept and are never modified here.
        """
        with self._lock:
            cursor = self._connection.cursor()
            # Detach references first so no ticket/panel points at a deleted row.
            cursor.execute(
                'UPDATE tickets SET ticket_category_id = NULL WHERE ticket_category_id = ?',
                (category_id,),
            )
            cursor.execute(
                'UPDATE ticket_panels SET ticket_category_id = NULL WHERE ticket_category_id = ?',
                (category_id,),
            )
            cursor.execute(
                'DELETE FROM ticket_categories WHERE category_id = ?',
                (category_id,),
            )
            self._connection.commit()
            self._invalidate_read_caches()
            return cursor.rowcount > 0

    def set_ticket_category(self, ticket_id: str, category_id: Optional[str]) -> bool:
        """Atomically change ONLY the category of an existing ticket.

        A single-column conditional UPDATE — every other field (claim info,
        transcript data, messages, timestamps, …) stays exactly as it was.
        """
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'UPDATE tickets SET ticket_category_id = ? WHERE ticket_id = ?',
                (category_id, ticket_id),
            )
            self._connection.commit()
            self._invalidate_read_caches()
            return cursor.rowcount > 0

    def set_panel_category(self, panel_id: str, category_id: Optional[str]) -> bool:
        """Atomically change ONLY the ticket category assigned to a panel."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'UPDATE ticket_panels SET ticket_category_id = ? WHERE panel_id = ?',
                (category_id, panel_id),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    async def async_load_ticket_categories(self, guild_id: int) -> List[Dict]:
        import asyncio
        return await asyncio.to_thread(self.load_ticket_categories, guild_id)

    async def async_load_ticket_category(self, category_id: str) -> Optional[Dict]:
        import asyncio
        return await asyncio.to_thread(self.load_ticket_category, category_id)

    async def async_set_ticket_category(self, ticket_id: str, category_id: Optional[str]) -> bool:
        import asyncio
        return await asyncio.to_thread(self.set_ticket_category, ticket_id, category_id)

    # === TICKETS ===
    def save_ticket(self, ticket: Dict) -> None:
        # Every known ticket column. _table_columns filters out any that this
        # particular database doesn't have yet. This fixes the bug where the
        # previous fixed-column INSERT OR REPLACE silently reset priority,
        # first_response_at, claim_count, escalation_count, staff_thread_id,
        # staff_responded_at (etc.) back to defaults on every save.
        fields = {
            'ticket_id': ticket.get('ticket_id'),
            'guild_id': ticket.get('guild_id'),
            'channel_id': ticket.get('channel_id'),
            'panel_id': ticket.get('panel_id'),
            'creator_id': ticket.get('creator_id'),
            'category': ticket.get('category'),
            'subject': ticket.get('subject'),
            'claimed_by': ticket.get('claimed_by'),
            'claimed_at': ticket.get('claimed_at'),
            'status': ticket.get('status', 'open'),
            'created_at': ticket.get('created_at'),
            'closed_at': ticket.get('closed_at'),
            'closed_by': ticket.get('closed_by'),
            'close_reason': ticket.get('close_reason'),
            'rating': ticket.get('rating'),
            'rating_feedback': ticket.get('rating_feedback'),
            'priority': ticket.get('priority', 'normal'),
            'first_response_at': ticket.get('first_response_at'),
            # Ticket Category (internal folder) this ticket belongs to.
            'ticket_category_id': ticket.get('ticket_category_id'),
            # Premium package columns (persisted only when present).
            'escalation_count': ticket.get('escalation_count'),
            'last_escalated_at': ticket.get('last_escalated_at'),
            'staff_responded_at': ticket.get('staff_responded_at'),
            'is_thread': ticket.get('is_thread'),
            'thread_id': ticket.get('thread_id'),
            'staff_thread_id': ticket.get('staff_thread_id'),
            'recycled_from_channel_id': ticket.get('recycled_from_channel_id'),
            'close_message_sent': ticket.get('close_message_sent'),
            'reopen_message_sent': ticket.get('reopen_message_sent'),
            'claim_count': ticket.get('claim_count'),
            # TicketTool /pause + /private state.
            'automation_paused': ticket.get('automation_paused', 0),
            'automation_paused_at': ticket.get('automation_paused_at'),
            'automation_paused_until': ticket.get('automation_paused_until'),
            'automation_resumed_at': ticket.get('automation_resumed_at'),
            'is_private': ticket.get('is_private', 0),
        }
        with self._lock:
            cursor = self._connection.cursor()
            cols = [c for c in fields if c in self._table_columns('tickets')]
            col_list = ', '.join(cols)
            placeholders = ', '.join('?' for _ in cols)
            cursor.execute(
                f'INSERT OR REPLACE INTO tickets ({col_list}) VALUES ({placeholders})',
                [fields[c] for c in cols],
            )
            self._connection.commit()
            self._invalidate_read_caches()

    def load_ticket(self, ticket_id: str) -> Optional[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM tickets WHERE ticket_id = ?', (ticket_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def load_ticket_by_channel(self, channel_id: int) -> Optional[Dict]:
        # PERFORMANCE (Phase 2): gateway hot path — called on EVERY message.
        # Served from the in-memory channel cache (negative entries cached
        # too: most channels are not tickets) and only falls back to SQLite
        # (idx_tickets_channel) on a miss. Returns a COPY so callers can
        # mutate + re-save without corrupting the cache.
        cached = self._channel_ticket_cache.get(channel_id, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return dict(cached) if cached is not None else None
        generation = self._read_caches_generation
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM tickets WHERE channel_id = ?', (channel_id,))
        row = cursor.fetchone()
        ticket = dict(row) if row else None
        # Install only if no invalidating write happened while querying.
        if generation == self._read_caches_generation:
            self._channel_ticket_cache[channel_id] = ticket
        return dict(ticket) if ticket else None

    def load_tickets_by_guild(self, guild_id: int, status: str = None) -> List[Dict]:
        cursor = self._connection.cursor()
        if status:
            cursor.execute('SELECT * FROM tickets WHERE guild_id = ? AND status = ?', (guild_id, status))
        else:
            cursor.execute('SELECT * FROM tickets WHERE guild_id = ?', (guild_id,))
        return [dict(row) for row in cursor.fetchall()]

    def load_tickets_by_creator(self, creator_id: int, guild_id: int = None) -> List[Dict]:
        cursor = self._connection.cursor()
        if guild_id:
            cursor.execute('SELECT * FROM tickets WHERE creator_id = ? AND guild_id = ? AND status = "open"', (creator_id, guild_id))
        else:
            cursor.execute('SELECT * FROM tickets WHERE creator_id = ? AND status = "open"', (creator_id,))
        return [dict(row) for row in cursor.fetchall()]

    def count_active_tickets_by_creator_and_panel(self, creator_id: int, guild_id: int, panel_id: str) -> int:
        """Count a user's ACTIVE tickets (open/pending) within ONE panel.

        Used for the per-panel ticket limit (TicketTool semantics: a panel's
        "open tickets per user" limit counts only that panel's tickets).
        """
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT COUNT(*) AS c FROM tickets WHERE creator_id = ? AND guild_id = ? '
            'AND panel_id = ? AND status IN ("open","pending")',
            (creator_id, guild_id, panel_id),
        )
        row = cursor.fetchone()
        return int(row['c']) if row else 0

    def count_closed_tickets_by_creator(self, creator_id: int, guild_id: int) -> int:
        """Count a user's CLOSED tickets (TicketTool closed-ticket limit).

        Checked at creation time — blocks users who constantly open + close
        tickets to farm fresh channels. 0/None disables the check.
        """
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT COUNT(*) AS c FROM tickets WHERE creator_id = ? AND guild_id = ? '
            'AND status = "closed"',
            (creator_id, guild_id),
        )
        row = cursor.fetchone()
        return int(row['c']) if row else 0

    def count_open_tickets_in_guild(self, guild_id: int) -> int:
        """Count ALL open/pending tickets in a guild (TicketTool "open
        tickets all users" limit)."""
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT COUNT(*) AS c FROM tickets WHERE guild_id = ? '
            'AND status IN ("open","pending")',
            (guild_id,),
        )
        row = cursor.fetchone()
        return int(row['c']) if row else 0

    def get_last_ticket_message_time(self, ticket_id: str) -> Optional[str]:
        """Return the created_at ISO string of the newest backed-up message,
        or None when the ticket has no backed-up messages. Used by the
        auto-close idle check."""
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT created_at FROM ticket_messages WHERE ticket_id = ? '
            'ORDER BY created_at DESC LIMIT 1',
            (ticket_id,),
        )
        row = cursor.fetchone()
        return row['created_at'] if row else None

    # === MULTI-PANELS (TicketTool Attached Panels / Dropdown Style) ===
    def save_multi_panel(self, row: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO multi_panels
                (message_id, guild_id, channel_id, style, panel_ids, per_row, placeholder, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                row.get('message_id'),
                row.get('guild_id'),
                row.get('channel_id'),
                row.get('style', 'buttons'),
                row.get('panel_ids'),
                row.get('per_row', 5),
                row.get('placeholder'),
                row.get('created_at'),
            ))
            self._connection.commit()

    def load_multi_panels_by_guild(self, guild_id: int) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM multi_panels WHERE guild_id = ?', (guild_id,))
        return [dict(row) for row in cursor.fetchall()]

    def delete_multi_panel(self, message_id: int) -> bool:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM multi_panels WHERE message_id = ?', (message_id,))
            self._connection.commit()
            return cursor.rowcount > 0

    # === REACTION PANELS (TicketTool reaction-based panels) ===
    def save_reaction_panel(self, row: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO reaction_panels
                (message_id, guild_id, channel_id, title, mapping, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                row.get('message_id'),
                row.get('guild_id'),
                row.get('channel_id'),
                row.get('title'),
                row.get('mapping'),
                row.get('created_at'),
            ))
            self._connection.commit()

    def load_reaction_panel(self, message_id: int) -> Optional[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM reaction_panels WHERE message_id = ?', (message_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def load_reaction_panels_by_guild(self, guild_id: int) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM reaction_panels WHERE guild_id = ?', (guild_id,))
        return [dict(row) for row in cursor.fetchall()]

    def delete_reaction_panel(self, message_id: int) -> bool:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM reaction_panels WHERE message_id = ?', (message_id,))
            self._connection.commit()
            return cursor.rowcount > 0

    def count_active_tickets_by_creator(self, creator_id: int, guild_id: int = None) -> int:
        """Count a user's ACTIVE tickets (status 'pending' OR 'open').

        This is the authoritative count for the ticket-limit check. Counting
        BOTH 'pending' and 'open' closes the race window where two concurrent
        create requests both passed a check that only counted 'open' tickets
        (the first request had inserted a 'pending' row that the second
        request's check ignored). Combined with the per-user creation lock in
        TicketToolSystem.create_ticket, the limit can no longer be bypassed.
        """
        cursor = self._connection.cursor()
        if guild_id:
            cursor.execute(
                'SELECT COUNT(*) AS c FROM tickets WHERE creator_id = ? AND guild_id = ? AND status IN ("open","pending")',
                (creator_id, guild_id),
            )
        else:
            cursor.execute(
                'SELECT COUNT(*) AS c FROM tickets WHERE creator_id = ? AND status IN ("open","pending")',
                (creator_id,),
            )
        row = cursor.fetchone()
        return int(row['c']) if row else 0

    # ------------------------------------------------------------------
    # Async-friendly wrappers.
    #
    # The DataManager uses synchronous sqlite3. Calling these directly from
    # async Discord handlers blocks the event loop on a busy server. The
    # async_* wrappers below run the same calls in a worker thread via
    # asyncio.to_thread, so the event loop is never blocked. Internally the
    # threading.Lock keeps concurrent access safe.
    # ------------------------------------------------------------------
    async def async_save_ticket(self, ticket: Dict) -> None:
        import asyncio
        await asyncio.to_thread(self.save_ticket, ticket)

    async def async_load_ticket(self, ticket_id: str) -> Optional[Dict]:
        import asyncio
        return await asyncio.to_thread(self.load_ticket, ticket_id)

    async def async_load_ticket_by_channel(self, channel_id: int) -> Optional[Dict]:
        import asyncio
        return await asyncio.to_thread(self.load_ticket_by_channel, channel_id)

    async def async_load_tickets_by_creator(self, creator_id: int, guild_id: int = None) -> List[Dict]:
        import asyncio
        return await asyncio.to_thread(self.load_tickets_by_creator, creator_id, guild_id)

    async def async_save_ticket_answer(self, answer: Dict) -> None:
        import asyncio
        await asyncio.to_thread(self.save_ticket_answer, answer)

    async def async_save_ticket_note(self, note: Dict) -> None:
        import asyncio
        await asyncio.to_thread(self.save_ticket_note, note)

    async def async_save_transcript(self, transcript: Dict) -> None:
        import asyncio
        await asyncio.to_thread(self.save_transcript, transcript)

    async def async_save_ticket_message(self, message: Dict) -> None:
        import asyncio
        await asyncio.to_thread(self.save_ticket_message, message)

    async def async_load_ticket_messages(self, ticket_id: str) -> List[Dict]:
        import asyncio
        return await asyncio.to_thread(self.load_ticket_messages, ticket_id)

    def atomic_claim_ticket(self, ticket_id: str, user_id: int) -> Tuple[bool, str]:
        """
        Atomically claim a ticket via a single conditional UPDATE.

        Returns (success, message). This fixes the TOCTOU race where two staff
        members could both pass the `claimed_by IS NULL` check before either
        save completed.
        """
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'UPDATE tickets SET claimed_by = ?, claimed_at = ? '
                'WHERE ticket_id = ? AND (claimed_by IS NULL OR claimed_by = 0)',
                (
                    user_id,
                    datetime.now(timezone.utc).isoformat(),
                    ticket_id,
                ),
            )
            self._connection.commit()
            self._invalidate_read_caches()
            if cursor.rowcount > 0:
                return True, "Ticket claimed."
            # Either the ticket doesn't exist or it's already claimed.
            cursor.execute('SELECT claimed_by FROM tickets WHERE ticket_id = ?', (ticket_id,))
            row = cursor.fetchone()
            if row is None:
                return False, "Ticket not found."
            existing = row['claimed_by'] if isinstance(row, dict) else row[0]
            if existing:
                return False, f"Already claimed by <@{existing}>."
            return False, "Could not claim ticket."

    def atomic_clear_claim(self, ticket_id: str) -> bool:
        """Atomically clear a ticket's claimed_by/claimed_at.

        Used by premium advanced-claiming's auto_replace_claimer feature: it
        clears the existing claim so a subsequent atomic_claim_ticket succeeds
        for the new claimer. Conditional on claimed_by being non-null so it
        won't clobber a concurrent unclaim that already cleared it.
        """
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'UPDATE tickets SET claimed_by = NULL, claimed_at = NULL '
                'WHERE ticket_id = ? AND claimed_by IS NOT NULL',
                (ticket_id,),
            )
            self._connection.commit()
            self._invalidate_read_caches()
            return cursor.rowcount > 0

    def atomic_begin_closing(self, ticket_id: str, closed_by: int, reason: str) -> bool:
        """Atomically transition a ticket from 'open' to 'closing'.

        Does a single conditional UPDATE so two near-simultaneous close
        requests can't both pass the `status == 'open'` check and both proceed
        to generate transcripts / delete the channel. Only the first UPDATE
        affects a row (rowcount == 1); the second is a no-op.

        Returns True if this caller won the race (the ticket is now 'closing'
        and owned by this closer), False if the ticket was already closing /
        closed / not found.
        """
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'UPDATE tickets SET status = ?, closed_by = ?, close_reason = ? '
                'WHERE ticket_id = ? AND status = "open"',
                ('closing', closed_by, reason, ticket_id),
            )
            self._connection.commit()
            self._invalidate_read_caches()
            return cursor.rowcount > 0

    def atomic_revert_closing(self, ticket_id: str) -> bool:
        """Revert a 'closing' ticket back to 'open'.

        Used when transcript generation fails after atomic_begin_closing
        succeeded, so the ticket can be retried instead of being stuck in
        'closing'. Conditional on status='closing' so it won't clobber a
        concurrent caller that already moved the ticket to 'closed'.
        """
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'UPDATE tickets SET status = "open", closed_by = NULL, close_reason = NULL '
                'WHERE ticket_id = ? AND status = "closing"',
                (ticket_id,),
            )
            self._connection.commit()
            self._invalidate_read_caches()
            return cursor.rowcount > 0

    def load_tickets_by_claimed(self, claimed_by: int, guild_id: int = None) -> List[Dict]:
        cursor = self._connection.cursor()
        if guild_id:
            cursor.execute('SELECT * FROM tickets WHERE claimed_by = ? AND guild_id = ? AND status = "open"', (claimed_by, guild_id))
        else:
            cursor.execute('SELECT * FROM tickets WHERE claimed_by = ? AND status = "open"', (claimed_by,))
        return [dict(row) for row in cursor.fetchall()]

    def load_all_open_tickets(self) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM tickets WHERE status = "open"')
        return [dict(row) for row in cursor.fetchall()]

    # === TICKET TRANSCRIPTS ===
    def save_transcript(self, transcript: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT INTO ticket_transcripts
                (transcript_id, ticket_id, guild_id, channel_id, creator_id, closed_by, 
                 claimed_by, category, created_at, closed_at, message_count, file_path, html_content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                transcript.get('transcript_id'),
                transcript.get('ticket_id'),
                transcript.get('guild_id'),
                transcript.get('channel_id'),
                transcript.get('creator_id'),
                transcript.get('closed_by'),
                transcript.get('claimed_by'),
                transcript.get('category'),
                transcript.get('created_at'),
                transcript.get('closed_at'),
                transcript.get('message_count', 0),
                transcript.get('file_path'),
                transcript.get('html_content')
            ))
            self._connection.commit()

    def load_transcripts_by_guild(self, guild_id: int, limit: int = 50) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_transcripts WHERE guild_id = ? ORDER BY closed_at DESC LIMIT ?', (guild_id, limit))
        return [dict(row) for row in cursor.fetchall()]

    def load_transcript(self, ticket_id: str) -> Optional[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_transcripts WHERE ticket_id = ?', (ticket_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    # === TICKET BLACKLIST ===
    def save_ticket_blacklist(self, blacklist: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO ticket_blacklist
                (blacklist_id, guild_id, user_id, reason, blacklisted_by, blacklisted_at, expires_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                blacklist.get('blacklist_id'),
                blacklist.get('guild_id'),
                blacklist.get('user_id'),
                blacklist.get('reason'),
                blacklist.get('blacklisted_by'),
                blacklist.get('blacklisted_at'),
                blacklist.get('expires_at'),
                blacklist.get('is_active', 1)
            ))
            self._connection.commit()

    def is_user_blacklisted(self, guild_id: int, user_id: int) -> Tuple[bool, Optional[str]]:
        cursor = self._connection.cursor()
        cursor.execute('''
            SELECT * FROM ticket_blacklist 
            WHERE guild_id = ? AND user_id = ? AND is_active = 1
        ''', (guild_id, user_id))
        row = cursor.fetchone()
        if row:
            # Check if expired
            if row['expires_at']:
                expires = datetime.fromisoformat(row['expires_at'])
                if datetime.now(timezone.utc) > expires:
                    return False, None
            return True, row['reason']
        return False, None

    def load_ticket_blacklist(self, guild_id: int) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_blacklist WHERE guild_id = ? AND is_active = 1', (guild_id,))
        return [dict(row) for row in cursor.fetchall()]

    def remove_ticket_blacklist(self, guild_id: int, user_id: int) -> bool:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('UPDATE ticket_blacklist SET is_active = 0 WHERE guild_id = ? AND user_id = ?', (guild_id, user_id))
            self._connection.commit()
            return cursor.rowcount > 0

    # === TICKET SETTINGS ===
    def save_ticket_settings(self, settings: Dict) -> None:
        # Column-aware upsert (same pattern as save_ticket/save_ticket_panel)
        # so newer columns (notes_channel_id, closed_category_id, log_events,
        # sla_hours) are persisted when present and ignored when the database
        # predates them.
        fields = {
            'guild_id': settings.get('guild_id'),
            'category_id': settings.get('category_id'),
            'transcripts_channel_id': settings.get('transcripts_channel_id'),
            'log_channel_id': settings.get('log_channel_id'),
            'support_role_id': settings.get('support_role_id'),
            'admin_role_id': settings.get('admin_role_id'),
            'max_tickets_per_user': settings.get('max_tickets_per_user', 3),
            'auto_close_hours': settings.get('auto_close_hours', 24),
            'mention_on_create': settings.get('mention_on_create', 1),
            'dm_transcripts': settings.get('dm_transcripts', 1),
            'require_claim': settings.get('require_claim', 0),
            'created_at': settings.get('created_at'),
            'updated_at': settings.get('updated_at'),
            'sla_hours': settings.get('sla_hours', 0),
            'notes_channel_id': settings.get('notes_channel_id'),
            'closed_category_id': settings.get('closed_category_id'),
            'log_events': settings.get('log_events'),
            'max_closed_tickets_per_user': settings.get('max_closed_tickets_per_user', 0),
            'max_open_tickets_all': settings.get('max_open_tickets_all', 0),
        }
        with self._lock:
            cursor = self._connection.cursor()
            cols = [c for c in fields if c in self._table_columns('ticket_settings')]
            col_list = ', '.join(cols)
            placeholders = ', '.join('?' for _ in cols)
            cursor.execute(
                f'INSERT OR REPLACE INTO ticket_settings ({col_list}) VALUES ({placeholders})',
                [fields[c] for c in cols],
            )
            self._connection.commit()
            self._invalidate_read_caches()

    def load_ticket_settings(self, guild_id: int) -> Optional[Dict]:
        # PERFORMANCE (Phase 2): read on nearly every ticket flow (create,
        # close, claim, panels) and by the 30/15-min loops. Cached copy;
        # invalidated by save_ticket_settings / purge_stale_data.
        cached = self._ticket_settings_cache.get(guild_id, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return dict(cached) if cached is not None else None
        generation = self._read_caches_generation
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_settings WHERE guild_id = ?', (guild_id,))
        row = cursor.fetchone()
        settings = dict(row) if row else None
        if generation == self._read_caches_generation:
            self._ticket_settings_cache[guild_id] = settings
        return dict(settings) if settings else None

    # === TICKET QUESTIONS ===
    def save_ticket_question(self, question: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO ticket_questions
                (question_id, panel_id, guild_id, question_text, question_type, required, placeholder, order_index, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                question.get('question_id'),
                question.get('panel_id'),
                question.get('guild_id'),
                question.get('question_text'),
                question.get('question_type', 'text'),
                question.get('required', 1),
                question.get('placeholder'),
                question.get('order_index', 0),
                question.get('created_at')
            ))
            self._connection.commit()

    def load_panel_questions(self, panel_id: str) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_questions WHERE panel_id = ? ORDER BY order_index', (panel_id,))
        return [dict(row) for row in cursor.fetchall()]

    def delete_ticket_question(self, question_id: str) -> bool:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM ticket_questions WHERE question_id = ?', (question_id,))
            self._connection.commit()
            return cursor.rowcount > 0

    # === TICKET ANSWERS ===
    def save_ticket_answer(self, answer: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT INTO ticket_answers (answer_id, ticket_id, question_id, user_id, answer_text, answered_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                answer.get('answer_id'),
                answer.get('ticket_id'),
                answer.get('question_id'),
                answer.get('user_id'),
                answer.get('answer_text'),
                answer.get('answered_at')
            ))
            self._connection.commit()

    def load_ticket_answers(self, ticket_id: str) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_answers WHERE ticket_id = ?', (ticket_id,))
        return [dict(row) for row in cursor.fetchall()]

    # === TICKET MESSAGES ===
    def save_ticket_message(self, message: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO ticket_messages
                (message_id, ticket_id, author_id, author_name, author_avatar, content, attachments, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                message.get('message_id'),
                message.get('ticket_id'),
                message.get('author_id'),
                message.get('author_name'),
                message.get('author_avatar'),
                message.get('content'),
                message.get('attachments'),
                message.get('created_at')
            ))
            self._connection.commit()

    def load_ticket_messages(self, ticket_id: str) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY created_at', (ticket_id,))
        return [dict(row) for row in cursor.fetchall()]

    def delete_ticket_messages(self, ticket_id: str) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM ticket_messages WHERE ticket_id = ?', (ticket_id,))
            self._connection.commit()

    # === TICKET NOTES ===
    def save_ticket_note(self, note: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO ticket_notes
                (note_id, ticket_id, guild_id, author_id, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                note.get('note_id'),
                note.get('ticket_id'),
                note.get('guild_id'),
                note.get('author_id'),
                note.get('content'),
                note.get('created_at'),
            ))
            self._connection.commit()

    def load_ticket_notes(self, ticket_id: str) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_notes WHERE ticket_id = ? ORDER BY created_at', (ticket_id,))
        return [dict(row) for row in cursor.fetchall()]

    def delete_ticket_note(self, note_id: str) -> bool:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM ticket_notes WHERE note_id = ?', (note_id,))
            self._connection.commit()
            return cursor.rowcount > 0

    # === TICKET STATISTICS ===
    def load_ticket_stats(self, guild_id: int) -> Dict:
        cursor = self._connection.cursor()
        cursor.execute('SELECT COUNT(*) as total FROM tickets WHERE guild_id = ?', (guild_id,))
        total = cursor.fetchone()['total']

        cursor.execute('SELECT COUNT(*) as total FROM tickets WHERE guild_id = ? AND status = "open"', (guild_id,))
        open_count = cursor.fetchone()['total']

        cursor.execute('SELECT COUNT(*) as total FROM tickets WHERE guild_id = ? AND status = "closed"', (guild_id,))
        closed_count = cursor.fetchone()['total']

        cursor.execute('''
            SELECT AVG(rating) as avg_rating FROM tickets
            WHERE guild_id = ? AND rating IS NOT NULL
        ''', (guild_id,))
        row = cursor.fetchone()
        avg_rating = round(row['avg_rating'], 2) if row['avg_rating'] else None

        # Average close time in hours
        cursor.execute('''
            SELECT created_at, closed_at FROM tickets
            WHERE guild_id = ? AND status = "closed" AND closed_at IS NOT NULL
        ''', (guild_id,))
        rows = cursor.fetchall()
        avg_hours = None
        if rows:
            durations = []
            for r in rows:
                try:
                    created = datetime.fromisoformat(r['created_at'].replace('Z', '+00:00'))
                    closed = datetime.fromisoformat(r['closed_at'].replace('Z', '+00:00'))
                    durations.append((closed - created).total_seconds() / 3600)
                except Exception:
                    pass
            if durations:
                avg_hours = round(sum(durations) / len(durations), 2)

        # Priority breakdown
        cursor.execute('''
            SELECT priority, COUNT(*) as cnt FROM tickets
            WHERE guild_id = ? AND status = "open"
            GROUP BY priority
        ''', (guild_id,))
        priority_rows = cursor.fetchall()
        priorities = {r['priority']: r['cnt'] for r in priority_rows}

        return {
            'total': total,
            'open': open_count,
            'closed': closed_count,
            'avg_rating': avg_rating,
            'avg_close_hours': avg_hours,
            'priorities': priorities,
        }

    def update_ticket_first_response(self, ticket_id: str) -> None:
        """Record first staff response time if not already set."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'UPDATE tickets SET first_response_at = ? WHERE ticket_id = ? AND first_response_at IS NULL',
                (datetime.now(timezone.utc).isoformat(), ticket_id)
            )
            self._connection.commit()
            self._invalidate_read_caches()

    def mark_ticket_sla_warned(self, ticket_id: str) -> None:
        """Record that the SLA loop already warned about this ticket.

        Deliberately separate from first_response_at: a bot-generated SLA
        alert is NOT a staff response, and faking one corrupted response
        analytics. This column only exists so the warn-once logic has
        persistent state.
        """
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'UPDATE tickets SET sla_warned_at = ? WHERE ticket_id = ?',
                (datetime.now(timezone.utc).isoformat(), ticket_id)
            )
            self._connection.commit()
            self._invalidate_read_caches()

    def purge_stale_data(
        self,
        valid_ticket_ids: set,
        valid_panel_ids: set,
        orphaned_open_ticket_ids: set,
        expired_blacklist_ids: set,
        closed_ticket_ids: set,
        purge_closed_ticket_metadata: bool = False,
    ) -> Dict[str, int]:
        """
        Delete rows that are no longer referenced, valid, or needed.
        Returns a dict of table -> rows deleted for the report.

        SAFETY CHANGE: by default (purge_closed_ticket_metadata=False) the
        answers/notes/messages belonging to CLOSED tickets are now PRESERVED,
        not deleted. The previous behavior permanently destroyed the full
        historical record of every closed ticket (only the HTML transcript
        survived). Set purge_closed_ticket_metadata=True to opt back into the
        aggressive cleanup once you're confident it's what you want.
        Transcripts are ALWAYS preserved regardless.
        """
        counts: Dict[str, int] = {}

        with self._lock:
            cursor = self._connection.cursor()

            # 1. Closed tickets. By default we only mark them as already-closed
            # (no-op) and preserve their child data. Only when explicitly opted
            # in do we cascade-delete answers/notes/messages/tickets rows.
            if closed_ticket_ids and purge_closed_ticket_metadata:
                ph = ','.join('?' * len(closed_ticket_ids))
                ids = list(closed_ticket_ids)

                cursor.execute(f'DELETE FROM ticket_answers    WHERE ticket_id IN ({ph})', ids)
                counts['closed_answers'] = cursor.rowcount

                cursor.execute(f'DELETE FROM ticket_notes      WHERE ticket_id IN ({ph})', ids)
                counts['closed_notes'] = cursor.rowcount

                cursor.execute(f'DELETE FROM ticket_messages   WHERE ticket_id IN ({ph})', ids)
                counts['closed_messages'] = cursor.rowcount

                # NOTE: Transcripts are intentionally PRESERVED for closed tickets
                # so staff can reference them later.
                counts['closed_transcripts'] = 0

                cursor.execute(f'DELETE FROM tickets           WHERE ticket_id IN ({ph})', ids)
                counts['closed_tickets'] = cursor.rowcount

                # Rebuild valid_ticket_ids after deleting closed ones
                valid_ticket_ids = valid_ticket_ids - closed_ticket_ids
            else:
                # SAFE DEFAULT: preserve closed-ticket metadata, only flag counts.
                counts['closed_tickets'] = 0
                counts['closed_answers'] = 0
                counts['closed_notes'] = 0
                counts['closed_messages'] = 0
                counts['closed_transcripts'] = 0
                if closed_ticket_ids:
                    logging.info(
                        f"[DataManager] purge_stale_data: preserving metadata for "
                        f"{len(closed_ticket_ids)} closed ticket(s) (opt-in to delete)"
                    )

            # 2. Orphaned child rows whose ticket_id does not exist at all.
            #    (NOT "belongs to a closed ticket" — those are real tickets and
            #    are now preserved by default.)
            if valid_ticket_ids:
                ph2 = ','.join('?' * len(valid_ticket_ids))
                ids2 = list(valid_ticket_ids)

                # Only delete child rows whose ticket_id is NOT a valid ticket
                # at all (truly orphaned), rather than "not in the open set".
                cursor.execute(f'DELETE FROM ticket_answers     WHERE ticket_id NOT IN (SELECT ticket_id FROM tickets)')
                counts['orphan_answers'] = cursor.rowcount

                cursor.execute(f'DELETE FROM ticket_notes       WHERE ticket_id NOT IN (SELECT ticket_id FROM tickets)')
                counts['orphan_notes'] = cursor.rowcount

                cursor.execute(f'DELETE FROM ticket_messages    WHERE ticket_id NOT IN (SELECT ticket_id FROM tickets)')
                counts['orphan_messages'] = cursor.rowcount

                # Transcripts are PRESERVED even when orphaned.
                counts['orphan_transcripts'] = 0
            else:
                # No tickets exist at all in the table — child rows are all
                # orphaned. This branch is the only one that bulk-deletes.
                for tbl in ('ticket_answers', 'ticket_notes', 'ticket_messages'):
                    cursor.execute(f'DELETE FROM {tbl}')
                    counts[f'orphan_{tbl.replace("ticket_", "")}'] = cursor.rowcount
                counts['orphan_transcripts'] = 0

            # 3. Orphaned questions for deleted panels
            if valid_panel_ids:
                ph3 = ','.join('?' * len(valid_panel_ids))
                cursor.execute(
                    f'DELETE FROM ticket_questions WHERE panel_id NOT IN ({ph3})',
                    list(valid_panel_ids)
                )
            else:
                cursor.execute('DELETE FROM ticket_questions')
            counts['orphan_questions'] = cursor.rowcount

            # 4. Mark open tickets as closed where the Discord channel is gone
            if orphaned_open_ticket_ids:
                ph4 = ','.join('?' * len(orphaned_open_ticket_ids))
                cursor.execute(
                    f'''UPDATE tickets SET status = "closed",
                        close_reason = "Channel deleted - cleaned by dbcleanup",
                        closed_at = ?
                        WHERE ticket_id IN ({ph4})''',
                    [datetime.now(timezone.utc).isoformat()] + list(orphaned_open_ticket_ids)
                )
                counts['orphaned_tickets_closed'] = cursor.rowcount
            else:
                counts['orphaned_tickets_closed'] = 0

            # 5. Remove expired / inactive blacklist entries
            if expired_blacklist_ids:
                ph5 = ','.join('?' * len(expired_blacklist_ids))
                cursor.execute(
                    f'DELETE FROM ticket_blacklist WHERE blacklist_id IN ({ph5})',
                    list(expired_blacklist_ids)
                )
                counts['ticket_blacklist'] = cursor.rowcount
            else:
                counts['ticket_blacklist'] = 0

            # 6. Remove deactivated warnings
            cursor.execute('DELETE FROM warnings WHERE is_active = 0')
            counts['warnings'] = cursor.rowcount

            self._connection.commit()
            # Ticket rows were deleted / status-flipped — drop all caches.
            self._invalidate_read_caches()

        return counts

    def load_all_tickets(self) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM tickets')
        return [dict(row) for row in cursor.fetchall()]

    def load_all_ticket_panels(self) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_panels')
        return [dict(row) for row in cursor.fetchall()]

    def load_all_ticket_blacklist(self) -> List[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM ticket_blacklist')
        return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # STICKY ROLES (Dyno premium — re-apply on rejoin)
    # =========================================================================
    def get_sticky_role_config(self, guild_id: int) -> Dict:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM sticky_role_config WHERE guild_id = ?', (guild_id,))
        row = cursor.fetchone()
        if not row:
            return {
                'guild_id': guild_id, 'enabled': 0,
                'eligible_role_ids': '[]', 'updated_at': None,
            }
        return dict(row)

    def save_sticky_role_config(self, cfg: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO sticky_role_config
                (guild_id, enabled, eligible_role_ids, updated_at)
                VALUES (?, ?, ?, ?)
            ''', (
                cfg.get('guild_id'),
                1 if cfg.get('enabled') else 0,
                cfg.get('eligible_role_ids', '[]'),
                cfg.get('updated_at') or datetime.now(timezone.utc).isoformat(),
            ))
            self._connection.commit()

    def save_sticky_roles(self, guild_id: int, user_id: int, role_ids: List[int]) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO sticky_roles
                (entry_id, guild_id, user_id, role_ids, saved_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                str(_uuid.uuid4()), guild_id, user_id,
                json.dumps(role_ids),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._connection.commit()

    def load_sticky_roles(self, guild_id: int, user_id: int) -> List[int]:
        cursor = self._connection.cursor()
        cursor.execute(
            'SELECT role_ids FROM sticky_roles WHERE guild_id = ? AND user_id = ?',
            (guild_id, user_id)
        )
        row = cursor.fetchone()
        if not row:
            return []
        try:
            return [int(r) for r in json.loads(row['role_ids'])]
        except Exception:
            return []

    def delete_sticky_roles(self, guild_id: int, user_id: int) -> bool:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'DELETE FROM sticky_roles WHERE guild_id = ? AND user_id = ?',
                (guild_id, user_id)
            )
            self._connection.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # FULL MESSAGE LOGGING (Dyno premium — edits + deletes with content)
    # =========================================================================
    def get_message_log_config(self, guild_id: int) -> Dict:
        # PERFORMANCE (Phase 2): gateway hot path — read on EVERY logged
        # message (on_message snapshot + edit/delete logging). Cached copy
        # (the "no row" default is cached too — negative caching matters
        # because most guilds never touch msglog settings); invalidated by
        # save_message_log_config.
        cached = self._msglog_config_cache.get(guild_id)
        if cached is not None:
            return dict(cached)
        generation = self._read_caches_generation
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM message_log_config WHERE guild_id = ?', (guild_id,))
        row = cursor.fetchone()
        if not row:
            cfg = {
                'guild_id': guild_id, 'enabled': 0, 'log_channel_id': None,
                'log_edits': 1, 'log_deletes': 1, 'ignore_bots': 1,
                'ignore_channels': '[]', 'updated_at': None,
            }
        else:
            cfg = dict(row)
        if generation == self._read_caches_generation:
            self._msglog_config_cache[guild_id] = cfg
        return dict(cfg)

    def save_message_log_config(self, cfg: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO message_log_config
                (guild_id, enabled, log_channel_id, log_edits, log_deletes,
                 ignore_bots, ignore_channels, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                cfg.get('guild_id'),
                1 if cfg.get('enabled') else 0,
                cfg.get('log_channel_id'),
                1 if cfg.get('log_edits', True) else 0,
                1 if cfg.get('log_deletes', True) else 0,
                1 if cfg.get('ignore_bots', True) else 0,
                cfg.get('ignore_channels', '[]'),
                cfg.get('updated_at') or datetime.now(timezone.utc).isoformat(),
            ))
            self._connection.commit()
            self._invalidate_read_caches()

    # PERFORMANCE (Phase 2): message_log_cache write-behind buffer.
    # Snapshots are written on EVERY message in logging-enabled guilds;
    # committing each one individually (an fsync'd transaction per message)
    # dominated write load and serialised every snapshot behind the same
    # connection lock. Rows are now buffered and committed in ONE
    # executemany transaction — at most once per _MSG_CACHE_FLUSH_INTERVAL
    # seconds, or immediately when _MSG_CACHE_FLUSH_ROWS have accumulated.
    # This table is best-effort recovery data for edit/delete logging, so
    # losing the final ≤2s window on a hard crash is acceptable; reads and
    # deletes below are buffer-aware so callers see identical behaviour.
    _MSG_CACHE_FLUSH_ROWS: int = 50
    _MSG_CACHE_FLUSH_INTERVAL: float = 2.0

    def cache_message(self, msg: Dict) -> None:
        row = (
            msg.get('message_id'), msg.get('guild_id'), msg.get('channel_id'),
            msg.get('author_id'), msg.get('author_name'),
            msg.get('content', ''), msg.get('attachments', '[]'),
            msg.get('created_at'),
        )
        with self._msg_cache_lock:
            self._msg_cache_buffer.append(row)
            full = len(self._msg_cache_buffer) >= self._MSG_CACHE_FLUSH_ROWS
        if full:
            # Buffer is at capacity — flush now instead of waiting for the
            # interval tick (bounds memory under message floods).
            self._msg_cache_wake.set()

    def _flush_msg_cache_buffer(self) -> None:
        """Commit every buffered snapshot in one transaction (worker thread)."""
        with self._msg_cache_lock:
            rows = self._msg_cache_buffer
            self._msg_cache_buffer = []
        if not rows:
            return
        try:
            with self._lock:
                cursor = self._connection.cursor()
                cursor.executemany('''
                    INSERT OR REPLACE INTO message_log_cache
                    (message_id, guild_id, channel_id, author_id, author_name,
                     content, attachments, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', rows)
                self._connection.commit()
        except Exception as exc:
            logging.warning(
                f"[DataManager] message-cache batch flush failed ({len(rows)} rows): {exc}"
            )

    def _msg_cache_flush_loop(self) -> None:
        """Background flusher: interval tick OR buffer-full wake."""
        while True:
            self._msg_cache_wake.wait(timeout=self._MSG_CACHE_FLUSH_INTERVAL)
            self._msg_cache_wake.clear()
            if self._msg_cache_flusher_stop.is_set():
                break
            self._flush_msg_cache_buffer()
        # Final drain so close() doesn't lose buffered rows.
        self._flush_msg_cache_buffer()

    def load_cached_message(self, message_id: int) -> Optional[Dict]:
        # Buffer-aware: a snapshot may not be committed yet (write-behind).
        # reversed() → newest snapshot wins when edits re-cached a message.
        with self._msg_cache_lock:
            for row in reversed(self._msg_cache_buffer):
                if row[0] == message_id:
                    return {
                        'message_id': row[0], 'guild_id': row[1],
                        'channel_id': row[2], 'author_id': row[3],
                        'author_name': row[4], 'content': row[5],
                        'attachments': row[6], 'created_at': row[7],
                    }
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM message_log_cache WHERE message_id = ?', (message_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def delete_cached_message(self, message_id: int) -> None:
        # Buffer-aware: drop any not-yet-committed snapshot so the flusher
        # can't resurrect a row the caller just deleted (log_delete reads
        # the snapshot BEFORE calling this, so content is already logged).
        with self._msg_cache_lock:
            if any(row[0] == message_id for row in self._msg_cache_buffer):
                self._msg_cache_buffer = [
                    row for row in self._msg_cache_buffer if row[0] != message_id
                ]
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM message_log_cache WHERE message_id = ?', (message_id,))
            self._connection.commit()

    def prune_message_cache(self, keep_recent: int = 5000) -> int:
        """Keep the message cache from growing without bound."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('SELECT COUNT(*) as c FROM message_log_cache')
            total = cursor.fetchone()['c']
            if total <= keep_recent:
                return 0
            cursor.execute('''
                DELETE FROM message_log_cache WHERE message_id IN (
                    SELECT message_id FROM message_log_cache
                    ORDER BY created_at ASC LIMIT ?
                )
            ''', (total - keep_recent,))
            self._connection.commit()
            return cursor.rowcount

    # CUSTOM BOT BRANDING (avatar / banner / embed footer override)
    def get_branding(self, guild_id: int) -> Dict:
        # PERFORMANCE (Phase 2): read by every branded embed build (commands,
        # tickets, msglog). Cached copy — the "no row" default is cached too;
        # invalidated by save_branding.
        cached = self._branding_cache.get(guild_id)
        if cached is not None:
            return dict(cached)
        generation = self._read_caches_generation
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM bot_branding WHERE guild_id = ?', (guild_id,))
        row = cursor.fetchone()
        if not row:
            branding = {
                'guild_id': guild_id, 'embed_footer': None, 'embed_color': None,
                'embed_thumbnail': None, 'embed_image': None,
                'avatar_url': None, 'banner_url': None, 'updated_at': None,
            }
        else:
            branding = dict(row)
        if generation == self._read_caches_generation:
            self._branding_cache[guild_id] = branding
        return dict(branding)

    def save_branding(self, branding: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO bot_branding
                (guild_id, embed_footer, embed_color, embed_thumbnail,
                 embed_image, avatar_url, banner_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                branding.get('guild_id'),
                branding.get('embed_footer'),
                branding.get('embed_color'),
                branding.get('embed_thumbnail'),
                branding.get('embed_image'),
                branding.get('avatar_url'),
                branding.get('banner_url'),
                branding.get('updated_at') or datetime.now(timezone.utc).isoformat(),
            ))
            self._connection.commit()
            self._invalidate_read_caches()

    def save_ows_panel_state(self, message_id: int, channel_id: int, owner_id: int, current_category: str) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO ows_active_panel (id, message_id, channel_id, owner_id, current_category)
                VALUES (1, ?, ?, ?, ?)
            ''', (message_id, channel_id, owner_id, current_category))
            self._connection.commit()

    def load_ows_panel_state(self) -> Optional[Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT message_id, channel_id, owner_id, current_category FROM ows_active_panel WHERE id = 1')
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def delete_ows_panel_state(self) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM ows_active_panel WHERE id = 1')
            self._connection.commit()

    # =========================================================================
    # NO-PURGE EXCLUSIONS (verification auto-purge protection)
    # Message IDs marked with !nopurge are never deleted by the auto-purge
    # system, !purge, or !purgeall. The set is cached in memory for O(1)
    # membership checks and hydrated once on startup from the DB.
    # =========================================================================
    _no_purge_cache: Set[int] = set()
    _no_purge_by_channel: Dict[int, Set[int]] = {}
    _no_purge_loaded: bool = False

    def load_all_no_purge_message_ids(self) -> Set[int]:
        """Load EVERY no-purge message ID into the in-memory cache.

        Called once on startup (setup_hook) so the cache is warm before the
        first purge / auto-purge check. Safe to call again to refresh.
        """
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('SELECT message_id, channel_id FROM no_purge_messages')
            ids: Set[int] = set()
            by_chan: Dict[int, Set[int]] = {}
            for row in cursor.fetchall():
                mid = int(row['message_id'])
                cid = int(row['channel_id'])
                ids.add(mid)
                by_chan.setdefault(cid, set()).add(mid)
        DataManager._no_purge_cache = ids
        DataManager._no_purge_by_channel = by_chan
        DataManager._no_purge_loaded = True
        return ids

    def is_no_purge(self, message_id: int) -> bool:
        """O(1) check used by every purge path (auto-purge, !purge, !purgeall)."""
        if not DataManager._no_purge_loaded:
            self.load_all_no_purge_message_ids()
        return message_id in DataManager._no_purge_cache

    def is_no_purge_in_channel(self, channel_id: int, message_id: int) -> bool:
        """Channel-scoped check — stricter; useful for logging."""
        if not DataManager._no_purge_loaded:
            self.load_all_no_purge_message_ids()
        chan_set = DataManager._no_purge_by_channel.get(channel_id)
        return bool(chan_set) and message_id in chan_set

    def save_no_purge_message(self, message_id: int, channel_id: int,
                             guild_id: int, marked_by: int) -> None:
        """Persist a no-purge marker so it survives restarts."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'INSERT OR IGNORE INTO no_purge_messages '
                '(message_id, channel_id, guild_id, marked_by, marked_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (message_id, channel_id, guild_id, marked_by,
                 datetime.now(timezone.utc).isoformat()),
            )
            self._connection.commit()
        # Keep cache in sync without a full reload.
        DataManager._no_purge_cache.add(message_id)
        DataManager._no_purge_by_channel.setdefault(channel_id, set()).add(message_id)

    def delete_no_purge_message(self, channel_id: int, message_id: int) -> bool:
        """Remove a no-purge marker (e.g. if the message was deleted anyway)."""
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                'DELETE FROM no_purge_messages WHERE channel_id = ? AND message_id = ?',
                (channel_id, message_id),
            )
            self._connection.commit()
            removed = cursor.rowcount > 0
        if removed:
            DataManager._no_purge_cache.discard(message_id)
            chan_set = DataManager._no_purge_by_channel.get(channel_id)
            if chan_set:
                chan_set.discard(message_id)
        return removed

    def load_no_purge_messages(self, channel_id: Optional[int] = None) -> List[Dict]:
        """Return no-purge rows, optionally filtered to a channel."""
        cursor = self._connection.cursor()
        if channel_id is None:
            cursor.execute(
                'SELECT message_id, channel_id, guild_id, marked_by, marked_at '
                'FROM no_purge_messages ORDER BY marked_at DESC'
            )
        else:
            cursor.execute(
                'SELECT message_id, channel_id, guild_id, marked_by, marked_at '
                'FROM no_purge_messages WHERE channel_id = ? ORDER BY marked_at DESC',
                (channel_id,),
            )
        return [dict(r) for r in cursor.fetchall()]

    # BOT CONFIG (Branding & Channels)
    def get_config_value(self, key: str) -> Optional[str]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT value FROM bot_config WHERE key = ?', (key,))
        row = cursor.fetchone()
        return row['value'] if row else None

    def set_config_value(self, key: str, value: str) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)', (key, value))
            self._connection.commit()

    # GIVEAWAYS
    def save_giveaway(self, giveaway: Dict) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO giveaways
                (giveaway_id, message_id, channel_id, guild_id, host_id, prize, winner_count,
                 entries, winners, status, created_at, ends_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                giveaway.get('giveaway_id'),
                giveaway.get('message_id'),
                giveaway.get('channel_id'),
                giveaway.get('guild_id'),
                giveaway.get('host_id'),
                giveaway.get('prize'),
                giveaway.get('winner_count', 1),
                json.dumps(giveaway.get('entries', [])),
                json.dumps(giveaway.get('winners', [])),
                giveaway.get('status', 'active'),
                giveaway.get('created_at'),
                giveaway.get('ends_at')
            ))
            self._connection.commit()

    def load_all_giveaways(self) -> Dict[str, Dict]:
        cursor = self._connection.cursor()
        cursor.execute('SELECT * FROM giveaways')
        result = {}
        for row in cursor.fetchall():
            result[row['giveaway_id']] = {
                'giveaway_id': row['giveaway_id'],
                'message_id': row['message_id'],
                'channel_id': row['channel_id'],
                'guild_id': row['guild_id'],
                'host_id': row['host_id'],
                'prize': row['prize'],
                'winner_count': row['winner_count'],
                'entries': json.loads(row['entries']) if row['entries'] else [],
                'winners': json.loads(row['winners']) if row['winners'] else [],
                'status': row['status'],
                'created_at': row['created_at'],
                'ends_at': row['ends_at']
            }
        return result

    def delete_giveaway(self, giveaway_id: str) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute('DELETE FROM giveaways WHERE giveaway_id = ?', (giveaway_id,))
            self._connection.commit()
