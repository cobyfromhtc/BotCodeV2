# -*- coding: utf-8 -*-
'''
TicketTool.db — Tier 1 database schema additions.

All schema changes are IDEMPOTENT: every new table uses CREATE TABLE IF NOT
EXISTS, and every new column uses a guarded ALTER TABLE (try/except) so it
is safe to call on a brand-new DB *and* on an existing DB that already has
some of the columns.

Called from TicketTool.wiring.on_setup_hook() AFTER DataManager._create_tables()
has run, so the base ticket_* tables already exist.

Design rules (matching Bot.py's DataManager conventions):
  - synchronous sqlite3 + self._connection (we reuse the DataManager's conn)
  - threading.Lock already held by callers for writes; reads are fine without
    the lock because sqlite3 in serialized mode handles concurrent reads.
  - JSON-encoded list/dict columns (TEXT) instead of multiple columns when
    the shape is variable (e.g. role-id lists, automation actions).
'''

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import uuid as _uuid
except Exception:  # pragma: no cover
    _uuid = None


# =====================================================================
# SCHEMA INSTALL (idempotent)
# =====================================================================

def install_premium_schema(conn: sqlite3.Connection) -> None:
    '''Create every Tier 1 table and add every Tier 1 column.

    Safe to call repeatedly. Runs inside DataManager._create_tables() context
    via TicketTool.wiring.on_setup_hook().
    '''
    cursor = conn.cursor()

    # -----------------------------------------------------------------
    # TICKET NAMING TEMPLATES (Feature 5 + 8)
    # One row per panel. Stores open/closed/claimed name templates and the
    # numeric padding width (e.g. 4 -> #0057).
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_naming (
            panel_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            open_template TEXT,
            closed_template TEXT,
            claimed_template TEXT,
            number_padding INTEGER DEFAULT 0,
            updated_at TEXT
        )
    ''')

    # Global ticket counter per guild — used by {ticket.count} and number
    # padding. Incremented atomically on ticket creation. Persisted so the
    # counter survives restarts (the old system just used the ticket uuid).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_counter (
            guild_id INTEGER PRIMARY KEY,
            next_number INTEGER DEFAULT 1
        )
    ''')

    # -----------------------------------------------------------------
    # TICKET SCHEDULING / BUSINESS HOURS (Feature 4)
    # One row per panel. periods is a JSON list of {day, start, end} dicts.
    # bypass_role_ids is a JSON list of role ids that ignore the schedule.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_schedules (
            schedule_id TEXT PRIMARY KEY,
            panel_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            timezone TEXT DEFAULT 'UTC',
            periods TEXT,
            unavailable_message TEXT,
            bypass_role_ids TEXT,
            enabled INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_sched_panel ON ticket_schedules (panel_id)'
    )

    # -----------------------------------------------------------------
    # ROLE AUTOMATION (Feature 3)
    # Per-panel role add/remove rules for open/close/claim/unclaim events.
    # Each *_roles column is a JSON list of role ids.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_role_automation (
            panel_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            open_add_roles TEXT,
            open_remove_roles TEXT,
            close_add_roles TEXT,
            close_remove_roles TEXT,
            claim_add_roles TEXT,
            claim_remove_roles TEXT,
            unclaim_add_roles TEXT,
            unclaim_remove_roles TEXT,
            updated_at TEXT
        )
    ''')

    # -----------------------------------------------------------------
    # ADVANCED CLAIMING CONFIG (Feature 2)
    # Per-panel advanced claim behavior. Booleans stored as INTEGER 0/1.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_claiming_config (
            panel_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            only_claimer_unclaim INTEGER DEFAULT 1,
            claimer_and_owner_only_actions INTEGER DEFAULT 0,
            auto_replace_claimer INTEGER DEFAULT 0,
            allow_owner_claim INTEGER DEFAULT 0,
            rename_on_claim TEXT,
            move_category_on_claim INTEGER,
            hide_from_other_staff INTEGER DEFAULT 0,
            change_support_perms_on_claim INTEGER DEFAULT 0,
            claimed_message TEXT,
            unclaimed_message TEXT,
            updated_at TEXT
        )
    ''')

    # -----------------------------------------------------------------
    # AUTOMATION ENGINE (Feature 1)
    # Each row is one automation rule attached to a panel.
    # trigger_type: created | closed | reopened | owner_left | close_request
    #               | delayed | no_response | claim | unclaim
    # conditions: JSON list of {field, op, value} dicts
    # actions:    JSON list of {type, ...} dicts (close, delete, claim,
    #             unclaim, add_role, remove_role, send_message, rename,
    #             move, escalate, execute_command, start_automation,
    #             stop_automation)
    # delay_seconds: only used for trigger_type='delayed'
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_automations (
            automation_id TEXT PRIMARY KEY,
            panel_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            name TEXT,
            trigger_type TEXT NOT NULL,
            delay_seconds INTEGER DEFAULT 0,
            conditions TEXT,
            actions TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_auto_panel ON ticket_automations (panel_id)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_auto_trigger ON ticket_automations (trigger_type)'
    )

    # Tracks running delayed-automation timers so they survive restarts.
    # When the bot starts, any row whose fire_at has passed fires immediately;
    # any future row is re-armed with asyncio.sleep.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_automation_timers (
            timer_id TEXT PRIMARY KEY,
            automation_id TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            fire_at TEXT NOT NULL,
            fired INTEGER DEFAULT 0,
            created_at TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_atim_fire ON ticket_automation_timers (fire_at)'
    )

    # -----------------------------------------------------------------
    # ADVANCED TRANSCRIPT CONFIG (Feature 6)
    # Per-guild transcript behavior overrides.
    # save_mode: 'on_close' | 'on_delete' | 'both' | 'never'
    # auto_dm:   0/1 — always DM the creator a copy
    # custom_message: the embed description sent alongside the transcript
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_transcript_config (
            guild_id INTEGER PRIMARY KEY,
            save_mode TEXT DEFAULT 'on_close',
            auto_dm INTEGER DEFAULT 0,
            custom_message TEXT,
            custom_title TEXT,
            auto_save_channel_id INTEGER,
            enabled INTEGER DEFAULT 1,
            updated_at TEXT
        )
    ''')

    # -----------------------------------------------------------------
    # ADVANCED SLA (Feature 9)
    # Per-guild SLA targets. first_response_hours / resolution_hours both
    # optional (0 = disabled). escalation_role_id is pinged on breach.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_sla_config (
            guild_id INTEGER PRIMARY KEY,
            first_response_hours REAL DEFAULT 0,
            resolution_hours REAL DEFAULT 0,
            urgent_first_response_hours REAL DEFAULT 0,
            urgent_resolution_hours REAL DEFAULT 0,
            escalation_role_id INTEGER,
            warn_before_breach_pct INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            updated_at TEXT
        )
    ''')

    # Per-ticket SLA tracking. breach_state: 'ok' | 'warning' | 'breached'
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_sla_state (
            ticket_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            first_response_due_at TEXT,
            first_response_met_at TEXT,
            resolution_due_at TEXT,
            resolution_met_at TEXT,
            breach_state TEXT DEFAULT 'ok',
            last_notified_at TEXT,
            updated_at TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_sla_guild ON ticket_sla_state (guild_id)'
    )

    # -----------------------------------------------------------------
    # TICKET ESCALATION (Feature 10)
    # Per-guild escalation routes (from_panel -> to_panel) + history.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_escalation_routes (
            route_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            from_panel_id TEXT NOT NULL,
            to_panel_id TEXT NOT NULL,
            notify_role_id INTEGER,
            auto_escalate_hours REAL DEFAULT 0,
            auto_escalate_priority TEXT,
            created_at TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_escalation_history (
            escalation_id TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            from_panel_id TEXT,
            to_panel_id TEXT,
            escalated_by INTEGER,
            reason TEXT,
            escalated_at TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_esc_hist_ticket ON ticket_escalation_history (ticket_id)'
    )

    # -----------------------------------------------------------------
    # CSAT / ANALYTICS (Features 7 + 15)
    # Per-staff CSAT aggregation cache (rebuilt from tickets table). The
    # tickets table already stores rating + rating_feedback + claimed_by,
    # so most analytics are computed on the fly; this table caches the
    # expensive per-staff aggregates for fast dashboard responses.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_staff_stats (
            guild_id INTEGER NOT NULL,
            staff_id INTEGER NOT NULL,
            tickets_claimed INTEGER DEFAULT 0,
            tickets_closed INTEGER DEFAULT 0,
            ratings_count INTEGER DEFAULT 0,
            ratings_sum INTEGER DEFAULT 0,
            avg_rating REAL,
            avg_first_response_minutes REAL,
            avg_resolution_minutes REAL,
            updated_at TEXT,
            PRIMARY KEY (guild_id, staff_id)
        )
    ''')

    # -----------------------------------------------------------------
    # MIGRATIONS: add new columns to EXISTING tables (idempotent)
    # -----------------------------------------------------------------
    migrations = [
        # tickets: escalation + SLA bookkeeping
        'ALTER TABLE tickets ADD COLUMN escalation_count INTEGER DEFAULT 0',
        'ALTER TABLE tickets ADD COLUMN last_escalated_at TEXT',
        'ALTER TABLE tickets ADD COLUMN staff_responded_at TEXT',
        # ticket_panels: optional per-panel SLA override
        'ALTER TABLE ticket_panels ADD COLUMN naming_template TEXT',
        'ALTER TABLE ticket_panels ADD COLUMN schedule_enabled INTEGER DEFAULT 0',
        # === Tier 2 columns ===
        # tickets: thread-ticket + staff-thread + recycle bookkeeping
        'ALTER TABLE tickets ADD COLUMN is_thread INTEGER DEFAULT 0',
        'ALTER TABLE tickets ADD COLUMN thread_id INTEGER',
        'ALTER TABLE tickets ADD COLUMN staff_thread_id INTEGER',
        'ALTER TABLE tickets ADD COLUMN recycled_from_channel_id INTEGER',
        # ticket_panels: thread-ticket + staff-thread + flow + branded config
        'ALTER TABLE ticket_panels ADD COLUMN use_threads INTEGER DEFAULT 0',
        'ALTER TABLE ticket_panels ADD COLUMN thread_parent_channel_id INTEGER',
        'ALTER TABLE ticket_panels ADD COLUMN allow_user_invite_in_thread INTEGER DEFAULT 0',
        'ALTER TABLE ticket_panels ADD COLUMN create_staff_thread INTEGER DEFAULT 0',
        'ALTER TABLE ticket_panels ADD COLUMN recycle_channels INTEGER DEFAULT 0',
        'ALTER TABLE ticket_panels ADD COLUMN flow_id TEXT',
        'ALTER TABLE ticket_panels ADD COLUMN branded_replies_enabled INTEGER DEFAULT 0',
        'ALTER TABLE ticket_panels ADD COLUMN branded_webhook_id INTEGER',
    ]
    for sql in migrations:
        try:
            cursor.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists

    # =================================================================
    # TIER 2 TABLES
    # =================================================================

    # -----------------------------------------------------------------
    # KNOWLEDGE BASE (Features 12 + 13)
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS kb_articles (
            article_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            category TEXT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT,
            keywords TEXT,
            staff_only INTEGER DEFAULT 0,
            attachment_urls TEXT,
            view_count INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT,
            updated_at TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_kb_guild ON kb_articles (guild_id)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_kb_category ON kb_articles (guild_id, category)'
    )

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS kb_categories (
            category_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            staff_only INTEGER DEFAULT 0,
            created_at TEXT
        )
    ''')

    # FTS-style search helper (we use LIKE since sqlite FTS5 may not be
    # compiled in on all Python builds; the kb module falls back to
    # Python-side scoring if needed).
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_kb_title ON kb_articles (title)'
    )

    # -----------------------------------------------------------------
    # CHANNEL RECYCLING (Feature 24)
    # Pool of "soft-deleted" ticket channels that can be reused instead of
    # creating new ones (avoids Discord's 500-channels-per-guild limit).
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recycled_channels (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            panel_id TEXT,
            category_id INTEGER,
            recycled_at TEXT,
            used_count INTEGER DEFAULT 0
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_recycle_panel ON recycled_channels (panel_id)'
    )

    # -----------------------------------------------------------------
    # LOCALIZATION (Feature 28)
    # Per-guild language + custom string overrides.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS guild_locales (
            guild_id INTEGER PRIMARY KEY,
            language TEXT DEFAULT 'en',
            timezone TEXT DEFAULT 'UTC',
            updated_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS custom_strings (
            guild_id INTEGER NOT NULL,
            string_key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT,
            PRIMARY KEY (guild_id, string_key)
        )
    ''')

    # -----------------------------------------------------------------
    # BRANDED REPLIES (Feature 29)
    # Per-panel webhook config for anonymous/branded staff replies.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS branded_reply_config (
            panel_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            enabled INTEGER DEFAULT 0,
            webhook_id INTEGER,
            webhook_url TEXT,
            display_name TEXT,
            avatar_url TEXT,
            updated_at TEXT
        )
    ''')

    # -----------------------------------------------------------------
    # SUPPORT FLOWS (Feature 30)
    # Branching question flows per panel. steps is a JSON list of:
    #   {id, question, type: 'text'|'choice'|'paragraph', choices: [...],
    #    next: {choice_value: step_id}, default_next: step_id,
    #    route_to_panel: panel_id, route_to_role: role_id}
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_flows (
            flow_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            steps TEXT,
            start_step_id TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_flows_guild ON ticket_flows (guild_id)'
    )

    # Per-ticket flow state (where the user is in a branching flow).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_flow_state (
            ticket_id TEXT PRIMARY KEY,
            flow_id TEXT NOT NULL,
            current_step_id TEXT,
            answers TEXT,
            started_at TEXT,
            completed_at TEXT
        )
    ''')

    # -----------------------------------------------------------------
    # CUSTOM COMMANDS (Feature 36)
    # Per-guild custom commands that run automation action sequences.
    # actions is JSON (same shape as ticket_automations.actions).
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS custom_commands (
            command_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            actions TEXT,
            required_role_id INTEGER,
            ticket_only INTEGER DEFAULT 0,
            cooldown_seconds INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT,
            updated_at TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_ccmd_guild ON custom_commands (guild_id)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_ccmd_name ON custom_commands (guild_id, name)'
    )

    # Cooldown tracker (per user per command).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS custom_command_cooldowns (
            command_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            last_used_at TEXT,
            PRIMARY KEY (command_id, user_id)
        )
    ''')

    # =================================================================
    # TIER 3 TABLES
    # =================================================================

    # -----------------------------------------------------------------
    # MULTI-EMBED PANEL MESSAGES (Feature #10)
    # Replaces the single embed_title/embed_description on ticket_panels.
    # Each panel can have up to 10 embeds, each with up to 25 fields.
    # The `embed_data` column is a JSON blob with the full embed structure.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS panel_embeds (
            embed_id TEXT PRIMARY KEY,
            panel_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            order_index INTEGER DEFAULT 0,
            embed_data TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_pembeds_panel ON panel_embeds (panel_id)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_pembeds_order ON panel_embeds (panel_id, order_index)'
    )

    # -----------------------------------------------------------------
    # ADVANCED MODERATOR MESSAGES (Feature #9)
    # Customizable messages shown on close/reopen/delete/claim events.
    # Each row is one message config for one (panel, event) pair.
    # `embeds` is a JSON list of embed structures (multi-embed support).
    # `buttons` is a JSON list of button configs.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS moderator_messages (
            message_id TEXT PRIMARY KEY,
            panel_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            content TEXT,
            embeds TEXT,
            buttons TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_modmsg_panel ON moderator_messages (panel_id)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_modmsg_event ON moderator_messages (panel_id, event_type)'
    )

    # -----------------------------------------------------------------
    # FLOW REVIEW / APPROVAL CONFIG (Feature #32)
    # Extends ticket_flows with review/approval workflow support.
    # A flow marked as 'application' type routes completed submissions to
    # a review queue instead of creating a normal ticket.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS flow_review_config (
            flow_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            review_channel_id INTEGER,
            reviewer_role_id INTEGER,
            auto_approve_minutes INTEGER DEFAULT 0,
            auto_reject_minutes INTEGER DEFAULT 0,
            approved_panel_id TEXT,
            rejected_panel_id TEXT,
            updated_at TEXT
        )
    ''')

    # Pending review submissions (one per completed application flow).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS flow_review_queue (
            review_id TEXT PRIMARY KEY,
            flow_id TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            applicant_id INTEGER NOT NULL,
            answers TEXT,
            submitted_at TEXT,
            status TEXT DEFAULT 'pending',
            reviewed_by INTEGER,
            reviewed_at TEXT,
            review_notes TEXT
        )
    ''')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_review_guild ON flow_review_queue (guild_id)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_review_status ON flow_review_queue (status)'
    )

    # -----------------------------------------------------------------
    # CANNED REPLIES (Ticket Tool /canned — staff-saved reusable responses)
    # Per-guild named snippets with usage tracking; content supports the
    # TicketTool.variables template engine.
    # -----------------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS canned_replies (
            reply_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            content TEXT NOT NULL,
            created_by INTEGER,
            uses INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')
    cursor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_canned_guild_name ON canned_replies (guild_id, name)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_canned_guild ON canned_replies (guild_id)'
    )

    # -----------------------------------------------------------------
    # TIER 3 COLUMN MIGRATIONS
    # -----------------------------------------------------------------
    tier3_migrations = [
        # tickets: extra metadata for moderator messages + multi-embed
        'ALTER TABLE tickets ADD COLUMN close_message_sent INTEGER DEFAULT 0',
        'ALTER TABLE tickets ADD COLUMN reopen_message_sent INTEGER DEFAULT 0',
        'ALTER TABLE tickets ADD COLUMN claim_count INTEGER DEFAULT 0',
        # ticket_panels: multi-embed flag (use panel_embeds table instead of
        # the single embed_title/embed_description columns)
        'ALTER TABLE ticket_panels ADD COLUMN use_multi_embed INTEGER DEFAULT 0',
        # ticket_transcript_config: Tier 3 additions (disable HTML attachment,
        # save-on-delete toggle, transcript format)
        'ALTER TABLE ticket_transcript_config ADD COLUMN disable_html_attachment INTEGER DEFAULT 0',
        'ALTER TABLE ticket_transcript_config ADD COLUMN save_on_delete INTEGER DEFAULT 1',
        'ALTER TABLE ticket_transcript_config ADD COLUMN transcript_format TEXT DEFAULT "html"',
        # ticket_flows: mark flows as application-type (review/approval)
        'ALTER TABLE ticket_flows ADD COLUMN is_application INTEGER DEFAULT 0',
        # flow_review_queue: the review message posted in the review channel,
        # so decisions made outside the buttons (command / auto timer) can
        # update that message and remove its Approve/Reject buttons.
        'ALTER TABLE flow_review_queue ADD COLUMN review_message_id INTEGER',
    ]
    for sql in tier3_migrations:
        try:
            cursor.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()
    logging.info("[tickettool.db] Tier 1 + Tier 2 + Tier 3 schema installed (idempotent).")


# =====================================================================
# GENERIC JSON-LIST HELPERS
# Used by every feature module so role-id lists are stored/loaded
# consistently. Kept here so there's exactly one definition.
# =====================================================================

def _json_dumps(value) -> str:
    '''Serialize a value to a JSON string.

    IDempotent for JSON strings: callers throughout the package pass
    conditions/actions as already-encoded JSON text (e.g. /automate
    parameters), so re-encoding them produced double-encoded rows
    ('\"[]\"') that _json_loads_list then read back as plain strings —
    breaking condition evaluation. A str input is passed through when it
    already parses as JSON.
    '''
    if value is None:
        return '[]'
    if isinstance(value, str):
        text = value.strip() or '[]'
        try:
            json.loads(text)
            return text
        except (ValueError, TypeError):
            return '[]'
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return '[]'


def _json_loads_list(raw: Optional[str]) -> List:
    """Parse a JSON list column, tolerating the empty/null common cases.

    Fast-paths the two values we see most (`''` and `'[]'`) so the
    `json.loads` call — and the surrounding isinstance checks — only run
    when the string could actually contain JSON.
    """
    if not raw:
        return []
    stripped = raw.strip() if isinstance(raw, str) else raw
    if stripped == '' or stripped == '[]':
        return []
    # Only bother parsing if the string looks like a list or a quoted
    # string (the latter handles rows written double-encoded).
    if not isinstance(stripped, str) or not (stripped.startswith('[') or stripped.startswith('"')):
        return []
    try:
        data = json.loads(stripped)
        if isinstance(data, str):
            # Self-heal rows written double-encoded (see _json_dumps).
            data = json.loads(data)
        return data if isinstance(data, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _json_loads_dict(raw: Optional[str]) -> Dict:
    """Parse a JSON object column, tolerating the empty/null common cases.

    Same fast-path strategy as `_json_loads_list`.
    """
    if not raw:
        return {}
    stripped = raw.strip() if isinstance(raw, str) else raw
    if stripped == '' or stripped == '{}':
        return {}
    if not isinstance(stripped, str) or not (stripped.startswith('{') or stripped.startswith('"')):
        return {}
    try:
        data = json.loads(stripped)
        if isinstance(data, str):
            # Self-heal rows written double-encoded (see _json_dumps).
            data = json.loads(data)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


# =====================================================================
# GENERIC CONFIG ACCESSORS
# Each feature module owns its own get/set, but they all follow the same
# "load row -> dict -> mutate -> save row" pattern. These helpers do the
# sqlite plumbing so the feature modules stay readable.
# =====================================================================

class PremiumDB:
    '''Thin accessor over the DataManager's sqlite3 connection.

    Every method takes the DataManager (so we reuse its connection + lock)
    and the panel/guild id. We deliberately do NOT take a separate lock —
    DataManager._lock already serializes writes, and reads are safe on the
    shared connection because sqlite3 is in serialized mode.
    '''

    def __init__(self, data_manager):
        self.dm = data_manager

    @property
    def _conn(self) -> sqlite3.Connection:
        return self.dm._connection

    # ---------- NAMING ----------
    def get_naming(self, panel_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_naming WHERE panel_id = ?', (panel_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_naming(self, cfg: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_naming
                (panel_id, guild_id, open_template, closed_template, claimed_template,
                 number_padding, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(panel_id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  open_template=excluded.open_template,
                  closed_template=excluded.closed_template,
                  claimed_template=excluded.claimed_template,
                  number_padding=excluded.number_padding,
                  updated_at=excluded.updated_at
            ''', (
                cfg.get('panel_id'), cfg.get('guild_id'),
                cfg.get('open_template'), cfg.get('closed_template'),
                cfg.get('claimed_template'), int(cfg.get('number_padding', 0)),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()

    # atomic next-ticket-number for a guild
    def reserve_ticket_number(self, guild_id: int) -> int:
        '''Atomically reserve and return the next ticket number for a guild.'''
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute(
                'INSERT INTO ticket_counter (guild_id, next_number) VALUES (?, 1) '
                'ON CONFLICT(guild_id) DO UPDATE SET next_number = next_number + 1',
                (guild_id,),
            )
            # Read back the value we just caused. Use MAX to be safe.
            cur.execute('SELECT next_number FROM ticket_counter WHERE guild_id = ?', (guild_id,))
            row = cur.fetchone()
            self._conn.commit()
            return int(row['next_number']) if row else 1

    # ---------- SCHEDULING ----------
    def get_schedule(self, panel_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_schedules WHERE panel_id = ? AND enabled = 1', (panel_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_schedule_raw(self, panel_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_schedules WHERE panel_id = ?', (panel_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_schedule(self, cfg: Dict) -> str:
        schedule_id = cfg.get('schedule_id') or (str(_uuid.uuid4())[:8] if _uuid else f"sch-{int(datetime.now(timezone.utc).timestamp())}")
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_schedules
                (schedule_id, panel_id, guild_id, timezone, periods, unavailable_message,
                 bypass_role_ids, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(schedule_id) DO UPDATE SET
                  panel_id=excluded.panel_id,
                  guild_id=excluded.guild_id,
                  timezone=excluded.timezone,
                  periods=excluded.periods,
                  unavailable_message=excluded.unavailable_message,
                  bypass_role_ids=excluded.bypass_role_ids,
                  enabled=excluded.enabled,
                  updated_at=excluded.updated_at
            ''', (
                schedule_id, cfg.get('panel_id'), cfg.get('guild_id'),
                cfg.get('timezone', 'UTC'), _json_dumps(cfg.get('periods', [])),
                cfg.get('unavailable_message'), _json_dumps(cfg.get('bypass_role_ids', [])),
                int(bool(cfg.get('enabled', 0))),
                cfg.get('created_at') or datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()
        return schedule_id

    def delete_schedule(self, panel_id: str) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('DELETE FROM ticket_schedules WHERE panel_id = ?', (panel_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- ROLE AUTOMATION ----------
    def get_role_automation(self, panel_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_role_automation WHERE panel_id = ?', (panel_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_role_automation(self, cfg: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_role_automation
                (panel_id, guild_id, open_add_roles, open_remove_roles,
                 close_add_roles, close_remove_roles, claim_add_roles,
                 claim_remove_roles, unclaim_add_roles, unclaim_remove_roles, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(panel_id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  open_add_roles=excluded.open_add_roles,
                  open_remove_roles=excluded.open_remove_roles,
                  close_add_roles=excluded.close_add_roles,
                  close_remove_roles=excluded.close_remove_roles,
                  claim_add_roles=excluded.claim_add_roles,
                  claim_remove_roles=excluded.claim_remove_roles,
                  unclaim_add_roles=excluded.unclaim_add_roles,
                  unclaim_remove_roles=excluded.unclaim_remove_roles,
                  updated_at=excluded.updated_at
            ''', (
                cfg.get('panel_id'), cfg.get('guild_id'),
                _json_dumps(cfg.get('open_add_roles', [])),
                _json_dumps(cfg.get('open_remove_roles', [])),
                _json_dumps(cfg.get('close_add_roles', [])),
                _json_dumps(cfg.get('close_remove_roles', [])),
                _json_dumps(cfg.get('claim_add_roles', [])),
                _json_dumps(cfg.get('claim_remove_roles', [])),
                _json_dumps(cfg.get('unclaim_add_roles', [])),
                _json_dumps(cfg.get('unclaim_remove_roles', [])),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()

    # ---------- CLAIMING CONFIG ----------
    def get_claiming_config(self, panel_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_claiming_config WHERE panel_id = ?', (panel_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_claiming_config(self, cfg: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_claiming_config
                (panel_id, guild_id, only_claimer_unclaim, claimer_and_owner_only_actions,
                 auto_replace_claimer, allow_owner_claim, rename_on_claim,
                 move_category_on_claim, hide_from_other_staff,
                 change_support_perms_on_claim, claimed_message, unclaimed_message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(panel_id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  only_claimer_unclaim=excluded.only_claimer_unclaim,
                  claimer_and_owner_only_actions=excluded.claimer_and_owner_only_actions,
                  auto_replace_claimer=excluded.auto_replace_claimer,
                  allow_owner_claim=excluded.allow_owner_claim,
                  rename_on_claim=excluded.rename_on_claim,
                  move_category_on_claim=excluded.move_category_on_claim,
                  hide_from_other_staff=excluded.hide_from_other_staff,
                  change_support_perms_on_claim=excluded.change_support_perms_on_claim,
                  claimed_message=excluded.claimed_message,
                  unclaimed_message=excluded.unclaimed_message,
                  updated_at=excluded.updated_at
            ''', (
                cfg.get('panel_id'), cfg.get('guild_id'),
                int(bool(cfg.get('only_claimer_unclaim', 1))),
                int(bool(cfg.get('claimer_and_owner_only_actions', 0))),
                int(bool(cfg.get('auto_replace_claimer', 0))),
                int(bool(cfg.get('allow_owner_claim', 0))),
                cfg.get('rename_on_claim'),
                cfg.get('move_category_on_claim'),
                int(bool(cfg.get('hide_from_other_staff', 0))),
                int(bool(cfg.get('change_support_perms_on_claim', 0))),
                cfg.get('claimed_message'),
                cfg.get('unclaimed_message'),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()

    # ---------- AUTOMATIONS ----------
    def list_automations(self, panel_id: str) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_automations WHERE panel_id = ? ORDER BY created_at', (panel_id,))
        return [dict(r) for r in cur.fetchall()]

    def list_automations_by_trigger(self, panel_id: str, trigger_type: str) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT * FROM ticket_automations WHERE panel_id = ? AND trigger_type = ? AND enabled = 1',
            (panel_id, trigger_type),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_automation(self, automation_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_automations WHERE automation_id = ?', (automation_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_automation(self, cfg: Dict) -> str:
        aid = cfg.get('automation_id') or (str(_uuid.uuid4())[:8] if _uuid else f"au-{int(datetime.now(timezone.utc).timestamp())}")
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_automations
                (automation_id, panel_id, guild_id, name, trigger_type, delay_seconds,
                 conditions, actions, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(automation_id) DO UPDATE SET
                  panel_id=excluded.panel_id,
                  guild_id=excluded.guild_id,
                  name=excluded.name,
                  trigger_type=excluded.trigger_type,
                  delay_seconds=excluded.delay_seconds,
                  conditions=excluded.conditions,
                  actions=excluded.actions,
                  enabled=excluded.enabled,
                  updated_at=excluded.updated_at
            ''', (
                aid, cfg.get('panel_id'), cfg.get('guild_id'),
                cfg.get('name'), cfg.get('trigger_type'),
                int(cfg.get('delay_seconds', 0) or 0),
                _json_dumps(cfg.get('conditions', [])),
                _json_dumps(cfg.get('actions', [])),
                int(bool(cfg.get('enabled', 1))),
                cfg.get('created_at') or datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()
        return aid

    def delete_automation(self, automation_id: str) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('DELETE FROM ticket_automations WHERE automation_id = ?', (automation_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- AUTOMATION TIMERS ----------
    def save_automation_timer(self, timer: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT OR REPLACE INTO ticket_automation_timers
                (timer_id, automation_id, ticket_id, guild_id, fire_at, fired, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                timer.get('timer_id'), timer.get('automation_id'),
                timer.get('ticket_id'), timer.get('guild_id'),
                timer.get('fire_at'), int(bool(timer.get('fired', 0))),
                timer.get('created_at') or datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()

    def load_pending_timers(self) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_automation_timers WHERE fired = 0 ORDER BY fire_at')
        return [dict(r) for r in cur.fetchall()]

    def mark_timer_fired(self, timer_id: str) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('UPDATE ticket_automation_timers SET fired = 1 WHERE timer_id = ?', (timer_id,))
            self._conn.commit()

    def cancel_timers_for_ticket(self, ticket_id: str) -> int:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('UPDATE ticket_automation_timers SET fired = 1 WHERE ticket_id = ? AND fired = 0', (ticket_id,))
            self._conn.commit()
            return cur.rowcount

    # ---------- TRANSCRIPT CONFIG ----------
    def get_transcript_config(self, guild_id: int) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_transcript_config WHERE guild_id = ?', (guild_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_transcript_config(self, cfg: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_transcript_config
                (guild_id, save_mode, auto_dm, custom_message, custom_title,
                 auto_save_channel_id, enabled, updated_at,
                 disable_html_attachment, save_on_delete, transcript_format)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                  save_mode=excluded.save_mode,
                  auto_dm=excluded.auto_dm,
                  custom_message=excluded.custom_message,
                  custom_title=excluded.custom_title,
                  auto_save_channel_id=excluded.auto_save_channel_id,
                  enabled=excluded.enabled,
                  updated_at=excluded.updated_at,
                  disable_html_attachment=excluded.disable_html_attachment,
                  save_on_delete=excluded.save_on_delete,
                  transcript_format=excluded.transcript_format
            ''', (
                cfg.get('guild_id'), cfg.get('save_mode', 'on_close'),
                int(bool(cfg.get('auto_dm', 0))), cfg.get('custom_message'),
                cfg.get('custom_title'), cfg.get('auto_save_channel_id'),
                int(bool(cfg.get('enabled', 1))),
                datetime.now(timezone.utc).isoformat(),
                int(bool(cfg.get('disable_html_attachment', 0))),
                int(bool(cfg.get('save_on_delete', 1))),
                cfg.get('transcript_format', 'html'),
            ))
            self._conn.commit()

    # ---------- SLA ----------
    def get_sla_config(self, guild_id: int) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_sla_config WHERE guild_id = ?', (guild_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_sla_config(self, cfg: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_sla_config
                (guild_id, first_response_hours, resolution_hours,
                 urgent_first_response_hours, urgent_resolution_hours,
                 escalation_role_id, warn_before_breach_pct, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                  first_response_hours=excluded.first_response_hours,
                  resolution_hours=excluded.resolution_hours,
                  urgent_first_response_hours=excluded.urgent_first_response_hours,
                  urgent_resolution_hours=excluded.urgent_resolution_hours,
                  escalation_role_id=excluded.escalation_role_id,
                  warn_before_breach_pct=excluded.warn_before_breach_pct,
                  enabled=excluded.enabled,
                  updated_at=excluded.updated_at
            ''', (
                cfg.get('guild_id'), float(cfg.get('first_response_hours', 0) or 0),
                float(cfg.get('resolution_hours', 0) or 0),
                float(cfg.get('urgent_first_response_hours', 0) or 0),
                float(cfg.get('urgent_resolution_hours', 0) or 0),
                cfg.get('escalation_role_id'),
                int(cfg.get('warn_before_breach_pct', 0) or 0),
                int(bool(cfg.get('enabled', 1))),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()

    def get_sla_state(self, ticket_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_sla_state WHERE ticket_id = ?', (ticket_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_sla_state(self, cfg: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_sla_state
                (ticket_id, guild_id, first_response_due_at, first_response_met_at,
                 resolution_due_at, resolution_met_at, breach_state, last_notified_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  first_response_due_at=excluded.first_response_due_at,
                  first_response_met_at=excluded.first_response_met_at,
                  resolution_due_at=excluded.resolution_due_at,
                  resolution_met_at=excluded.resolution_met_at,
                  breach_state=excluded.breach_state,
                  last_notified_at=excluded.last_notified_at,
                  updated_at=excluded.updated_at
            ''', (
                cfg.get('ticket_id'), cfg.get('guild_id'),
                cfg.get('first_response_due_at'), cfg.get('first_response_met_at'),
                cfg.get('resolution_due_at'), cfg.get('resolution_met_at'),
                cfg.get('breach_state', 'ok'), cfg.get('last_notified_at'),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()

    def list_open_sla_states(self, guild_id: int) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT * FROM ticket_sla_state WHERE guild_id = ? AND resolution_met_at IS NULL',
            (guild_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ---------- ESCALATION ----------
    def list_escalation_routes(self, guild_id: int) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_escalation_routes WHERE guild_id = ?', (guild_id,))
        return [dict(r) for r in cur.fetchall()]

    def get_escalation_route(self, from_panel_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_escalation_routes WHERE from_panel_id = ?', (from_panel_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_escalation_route(self, cfg: Dict) -> str:
        rid = cfg.get('route_id') or (str(_uuid.uuid4())[:8] if _uuid else f"er-{int(datetime.now(timezone.utc).timestamp())}")
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_escalation_routes
                (route_id, guild_id, from_panel_id, to_panel_id, notify_role_id,
                 auto_escalate_hours, auto_escalate_priority, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  from_panel_id=excluded.from_panel_id,
                  to_panel_id=excluded.to_panel_id,
                  notify_role_id=excluded.notify_role_id,
                  auto_escalate_hours=excluded.auto_escalate_hours,
                  auto_escalate_priority=excluded.auto_escalate_priority
            ''', (
                rid, cfg.get('guild_id'), cfg.get('from_panel_id'),
                cfg.get('to_panel_id'), cfg.get('notify_role_id'),
                float(cfg.get('auto_escalate_hours', 0) or 0),
                cfg.get('auto_escalate_priority'),
                cfg.get('created_at') or datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()
        return rid

    def delete_escalation_route(self, route_id: str) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('DELETE FROM ticket_escalation_routes WHERE route_id = ?', (route_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def add_escalation_history(self, hist: Dict) -> None:
        eid = hist.get('escalation_id') or (str(_uuid.uuid4())[:8] if _uuid else f"eh-{int(datetime.now(timezone.utc).timestamp())}")
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_escalation_history
                (escalation_id, ticket_id, guild_id, from_panel_id, to_panel_id,
                 escalated_by, reason, escalated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                eid, hist.get('ticket_id'), hist.get('guild_id'),
                hist.get('from_panel_id'), hist.get('to_panel_id'),
                hist.get('escalated_by'), hist.get('reason'),
                hist.get('escalated_at') or datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()

    def load_escalation_history(self, ticket_id: str) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT * FROM ticket_escalation_history WHERE ticket_id = ? ORDER BY escalated_at',
            (ticket_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ---------- STAFF STATS (cached) ----------
    def upsert_staff_stats(self, stats: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_staff_stats
                (guild_id, staff_id, tickets_claimed, tickets_closed,
                 ratings_count, ratings_sum, avg_rating,
                 avg_first_response_minutes, avg_resolution_minutes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, staff_id) DO UPDATE SET
                  tickets_claimed=excluded.tickets_claimed,
                  tickets_closed=excluded.tickets_closed,
                  ratings_count=excluded.ratings_count,
                  ratings_sum=excluded.ratings_sum,
                  avg_rating=excluded.avg_rating,
                  avg_first_response_minutes=excluded.avg_first_response_minutes,
                  avg_resolution_minutes=excluded.avg_resolution_minutes,
                  updated_at=excluded.updated_at
            ''', (
                stats.get('guild_id'), stats.get('staff_id'),
                int(stats.get('tickets_claimed', 0)),
                int(stats.get('tickets_closed', 0)),
                int(stats.get('ratings_count', 0)),
                int(stats.get('ratings_sum', 0)),
                float(stats.get('avg_rating', 0) or 0),
                float(stats.get('avg_first_response_minutes', 0) or 0),
                float(stats.get('avg_resolution_minutes', 0) or 0),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()

    def load_staff_stats(self, guild_id: int) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT * FROM ticket_staff_stats WHERE guild_id = ? ORDER BY avg_rating DESC, tickets_closed DESC',
            (guild_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    # =================================================================
    # TIER 2 ACCESSORS
    # =================================================================

    # ---------- KNOWLEDGE BASE ----------
    def save_kb_article(self, article: Dict) -> str:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT OR REPLACE INTO kb_articles
                (article_id, guild_id, category, title, content, summary,
                 keywords, staff_only, attachment_urls, view_count,
                 created_by, created_at, updated_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                article.get('article_id'),
                article.get('guild_id'),
                article.get('category'),
                article.get('title'),
                article.get('content'),
                article.get('summary'),
                article.get('keywords'),
                int(bool(article.get('staff_only', 0))),
                int(article.get('view_count', 0) or 0),
                article.get('created_by'),
                article.get('created_at') or datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                int(bool(article.get('is_active', 1))),
            ))
            self._conn.commit()
        return article.get('article_id')

    def get_kb_article(self, article_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM kb_articles WHERE article_id = ? AND is_active = 1', (article_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_kb_articles(self, guild_id: int, category: Optional[str] = None,
                         include_staff_only: bool = True) -> List[Dict]:
        cur = self._conn.cursor()
        if category:
            if include_staff_only:
                cur.execute(
                    'SELECT * FROM kb_articles WHERE guild_id = ? AND category = ? AND is_active = 1 ORDER BY title',
                    (guild_id, category),
                )
            else:
                cur.execute(
                    'SELECT * FROM kb_articles WHERE guild_id = ? AND category = ? AND is_active = 1 AND staff_only = 0 ORDER BY title',
                    (guild_id, category),
                )
        else:
            if include_staff_only:
                cur.execute(
                    'SELECT * FROM kb_articles WHERE guild_id = ? AND is_active = 1 ORDER BY category, title',
                    (guild_id,),
                )
            else:
                cur.execute(
                    'SELECT * FROM kb_articles WHERE guild_id = ? AND is_active = 1 AND staff_only = 0 ORDER BY category, title',
                    (guild_id,),
                )
        return [dict(r) for r in cur.fetchall()]

    def search_kb_articles(self, guild_id: int, query: str,
                            include_staff_only: bool = True) -> List[Dict]:
        cur = self._conn.cursor()
        like = f'%{query}%'
        if include_staff_only:
            cur.execute(
                '''SELECT * FROM kb_articles WHERE guild_id = ? AND is_active = 1 AND
                   (title LIKE ? OR content LIKE ? OR keywords LIKE ? OR summary LIKE ?)
                   ORDER BY title''',
                (guild_id, like, like, like, like),
            )
        else:
            cur.execute(
                '''SELECT * FROM kb_articles WHERE guild_id = ? AND is_active = 1 AND staff_only = 0 AND
                   (title LIKE ? OR content LIKE ? OR keywords LIKE ? OR summary LIKE ?)
                   ORDER BY title''',
                (guild_id, like, like, like, like),
            )
        return [dict(r) for r in cur.fetchall()]

    def delete_kb_article(self, article_id: str) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('UPDATE kb_articles SET is_active = 0 WHERE article_id = ?', (article_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def increment_kb_view(self, article_id: str) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('UPDATE kb_articles SET view_count = view_count + 1 WHERE article_id = ?', (article_id,))
            self._conn.commit()

    def list_kb_categories(self, guild_id: int) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM kb_categories WHERE guild_id = ? ORDER BY name', (guild_id,))
        return [dict(r) for r in cur.fetchall()]

    def save_kb_category(self, cat: Dict) -> str:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT OR REPLACE INTO kb_categories
                (category_id, guild_id, name, description, staff_only, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                cat.get('category_id'),
                cat.get('guild_id'),
                cat.get('name'),
                cat.get('description'),
                int(bool(cat.get('staff_only', 0))),
                cat.get('created_at') or datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()
        return cat.get('category_id')

    # ---------- CHANNEL RECYCLING ----------
    def get_recycled_channel(self, panel_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT * FROM recycled_channels WHERE panel_id = ? ORDER BY recycled_at ASC LIMIT 1',
            (panel_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def add_recycled_channel(self, channel_id: int, guild_id: int,
                              panel_id: str, category_id: Optional[int]) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT OR REPLACE INTO recycled_channels
                (channel_id, guild_id, panel_id, category_id, recycled_at, used_count)
                VALUES (?, ?, ?, ?, ?, COALESCE((SELECT used_count FROM recycled_channels WHERE channel_id = ?), 0) + 1)
            ''', (channel_id, guild_id, panel_id, category_id,
                  datetime.now(timezone.utc).isoformat(), channel_id))
            self._conn.commit()

    def remove_recycled_channel(self, channel_id: int) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('DELETE FROM recycled_channels WHERE channel_id = ?', (channel_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def count_recycled_channels(self, panel_id: Optional[str] = None) -> int:
        cur = self._conn.cursor()
        if panel_id:
            cur.execute('SELECT COUNT(*) AS c FROM recycled_channels WHERE panel_id = ?', (panel_id,))
        else:
            cur.execute('SELECT COUNT(*) AS c FROM recycled_channels')
        row = cur.fetchone()
        return int(row['c']) if row else 0

    # ---------- LOCALIZATION ----------
    def get_locale(self, guild_id: int) -> Dict:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM guild_locales WHERE guild_id = ?', (guild_id,))
        row = cur.fetchone()
        if not row:
            return {'guild_id': guild_id, 'language': 'en', 'timezone': 'UTC', 'updated_at': None}
        return dict(row)

    def upsert_locale(self, guild_id: int, language: str, timezone: str = 'UTC') -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO guild_locales (guild_id, language, timezone, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                  language=excluded.language,
                  timezone=excluded.timezone,
                  updated_at=excluded.updated_at
            ''', (guild_id, language, timezone, datetime.now(timezone.utc).isoformat()))
            self._conn.commit()

    def get_custom_string(self, guild_id: int, string_key: str) -> Optional[str]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT value FROM custom_strings WHERE guild_id = ? AND string_key = ?',
            (guild_id, string_key),
        )
        row = cur.fetchone()
        return row['value'] if row else None

    def set_custom_string(self, guild_id: int, string_key: str, value: str) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO custom_strings (guild_id, string_key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, string_key) DO UPDATE SET
                  value=excluded.value,
                  updated_at=excluded.updated_at
            ''', (guild_id, string_key, value, datetime.now(timezone.utc).isoformat()))
            self._conn.commit()

    def delete_custom_string(self, guild_id: int, string_key: str) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute(
                'DELETE FROM custom_strings WHERE guild_id = ? AND string_key = ?',
                (guild_id, string_key),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_custom_strings(self, guild_id: int) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM custom_strings WHERE guild_id = ? ORDER BY string_key', (guild_id,))
        return [dict(r) for r in cur.fetchall()]

    # ---------- BRANDED REPLIES ----------
    def get_branded_reply_config(self, panel_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM branded_reply_config WHERE panel_id = ?', (panel_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_branded_reply_config(self, cfg: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO branded_reply_config
                (panel_id, guild_id, enabled, webhook_id, webhook_url,
                 display_name, avatar_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(panel_id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  enabled=excluded.enabled,
                  webhook_id=excluded.webhook_id,
                  webhook_url=excluded.webhook_url,
                  display_name=excluded.display_name,
                  avatar_url=excluded.avatar_url,
                  updated_at=excluded.updated_at
            ''', (
                cfg.get('panel_id'), cfg.get('guild_id'),
                int(bool(cfg.get('enabled', 0))),
                cfg.get('webhook_id'), cfg.get('webhook_url'),
                cfg.get('display_name'), cfg.get('avatar_url'),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()

    # ---------- SUPPORT FLOWS ----------
    def list_flows(self, guild_id: int) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_flows WHERE guild_id = ? ORDER BY name', (guild_id,))
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r['steps'] = _json_loads_list(r.get('steps'))
        return rows

    def get_flow(self, flow_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_flows WHERE flow_id = ?', (flow_id,))
        row = cur.fetchone()
        if not row:
            return None
        r = dict(row)
        r['steps'] = _json_loads_list(r.get('steps'))
        return r

    def upsert_flow(self, cfg: Dict) -> str:
        fid = cfg.get('flow_id') or (str(_uuid.uuid4())[:8] if _uuid else f"fl-{int(datetime.now(timezone.utc).timestamp())}")
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_flows
                (flow_id, guild_id, name, description, steps, start_step_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(flow_id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  name=excluded.name,
                  description=excluded.description,
                  steps=excluded.steps,
                  start_step_id=excluded.start_step_id,
                  updated_at=excluded.updated_at
            ''', (
                fid, cfg.get('guild_id'), cfg.get('name'), cfg.get('description'),
                _json_dumps(cfg.get('steps', [])),
                cfg.get('start_step_id'),
                cfg.get('created_at') or datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()
        return fid

    def delete_flow(self, flow_id: str) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('DELETE FROM ticket_flows WHERE flow_id = ?', (flow_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def get_flow_state(self, ticket_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM ticket_flow_state WHERE ticket_id = ?', (ticket_id,))
        row = cur.fetchone()
        if not row:
            return None
        r = dict(row)
        r['answers'] = _json_loads_dict(r.get('answers'))
        return r

    def upsert_flow_state(self, cfg: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO ticket_flow_state
                (ticket_id, flow_id, current_step_id, answers, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                  flow_id=excluded.flow_id,
                  current_step_id=excluded.current_step_id,
                  answers=excluded.answers,
                  completed_at=excluded.completed_at
            ''', (
                cfg.get('ticket_id'), cfg.get('flow_id'),
                cfg.get('current_step_id'),
                _json_dumps(cfg.get('answers', {})),
                cfg.get('started_at'),
                cfg.get('completed_at'),
            ))
            self._conn.commit()

    # ---------- CUSTOM COMMANDS ----------
    def list_custom_commands(self, guild_id: int) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT * FROM custom_commands WHERE guild_id = ? AND is_active = 1 ORDER BY name',
            (guild_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r['actions'] = _json_loads_list(r.get('actions'))
        return rows

    def get_custom_command(self, guild_id: int, name: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT * FROM custom_commands WHERE guild_id = ? AND name = ? AND is_active = 1',
            (guild_id, name.lower()),
        )
        row = cur.fetchone()
        if not row:
            return None
        r = dict(row)
        r['actions'] = _json_loads_list(r.get('actions'))
        return r

    def get_custom_command_by_id(self, command_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM custom_commands WHERE command_id = ?', (command_id,))
        row = cur.fetchone()
        if not row:
            return None
        r = dict(row)
        r['actions'] = _json_loads_list(r.get('actions'))
        return r

    def upsert_custom_command(self, cfg: Dict) -> str:
        cid = cfg.get('command_id') or (str(_uuid.uuid4())[:8] if _uuid else f"cc-{int(datetime.now(timezone.utc).timestamp())}")
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO custom_commands
                (command_id, guild_id, name, description, actions, required_role_id,
                 ticket_only, cooldown_seconds, created_by, created_at, updated_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(command_id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  name=excluded.name,
                  description=excluded.description,
                  actions=excluded.actions,
                  required_role_id=excluded.required_role_id,
                  ticket_only=excluded.ticket_only,
                  cooldown_seconds=excluded.cooldown_seconds,
                  updated_at=excluded.updated_at
            ''', (
                cid, cfg.get('guild_id'),
                str(cfg.get('name', '')).lower(),
                cfg.get('description'),
                _json_dumps(cfg.get('actions', [])),
                cfg.get('required_role_id'),
                int(bool(cfg.get('ticket_only', 0))),
                int(cfg.get('cooldown_seconds', 0) or 0),
                cfg.get('created_by'),
                cfg.get('created_at') or datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                int(bool(cfg.get('is_active', 1))),
            ))
            self._conn.commit()
        return cid

    def delete_custom_command(self, command_id: str) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('UPDATE custom_commands SET is_active = 0 WHERE command_id = ?', (command_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def check_custom_command_cooldown(self, command_id: str, user_id: int,
                                        cooldown_seconds: int) -> Tuple[bool, int]:
        '''Returns (can_run, remaining_seconds).'''
        cur = self._conn.cursor()
        cur.execute(
            'SELECT last_used_at FROM custom_command_cooldowns WHERE command_id = ? AND user_id = ?',
            (command_id, user_id),
        )
        row = cur.fetchone()
        if not row or not row['last_used_at']:
            return True, 0
        try:
            last = datetime.fromisoformat(row['last_used_at'].replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return True, 0
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        if elapsed >= cooldown_seconds:
            return True, 0
        return False, int(cooldown_seconds - elapsed)

    def record_custom_command_use(self, command_id: str, user_id: int) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO custom_command_cooldowns (command_id, user_id, last_used_at)
                VALUES (?, ?, ?)
                ON CONFLICT(command_id, user_id) DO UPDATE SET last_used_at=excluded.last_used_at
            ''', (command_id, user_id, datetime.now(timezone.utc).isoformat()))
            self._conn.commit()

    # =================================================================
    # TIER 3 ACCESSORS
    # =================================================================

    # ---------- MULTI-EMBED PANEL MESSAGES ----------
    def list_panel_embeds(self, panel_id: str) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            '''SELECT * FROM panel_embeds WHERE panel_id = ? AND is_active = 1
               ORDER BY order_index''',
            (panel_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r['embed_data'] = _json_loads_dict(r.get('embed_data'))
        return rows

    def save_panel_embed(self, embed: Dict) -> str:
        eid = embed.get('embed_id') or (str(_uuid.uuid4())[:8] if _uuid else f"em-{int(datetime.now(timezone.utc).timestamp())}")
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO panel_embeds
                (embed_id, panel_id, guild_id, order_index, embed_data,
                 created_at, updated_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(embed_id) DO UPDATE SET
                  order_index=excluded.order_index,
                  embed_data=excluded.embed_data,
                  updated_at=excluded.updated_at
            ''', (
                eid, embed.get('panel_id'), embed.get('guild_id'),
                int(embed.get('order_index', 0) or 0),
                _json_dumps(embed.get('embed_data', {})),
                embed.get('created_at') or datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                int(bool(embed.get('is_active', 1))),
            ))
            self._conn.commit()
        return eid

    def delete_panel_embed(self, embed_id: str) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('UPDATE panel_embeds SET is_active = 0 WHERE embed_id = ?', (embed_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def reorder_panel_embeds(self, panel_id: str, embed_order: List[str]) -> None:
        '''Reorder embeds by passing a list of embed_ids in the new order.'''
        with self.dm._lock:
            cur = self._conn.cursor()
            for idx, eid in enumerate(embed_order):
                cur.execute(
                    'UPDATE panel_embeds SET order_index = ? WHERE embed_id = ? AND panel_id = ?',
                    (idx, eid, panel_id),
                )
            self._conn.commit()

    # ---------- MODERATOR MESSAGES ----------
    def get_moderator_message(self, panel_id: str, event_type: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            '''SELECT * FROM moderator_messages
               WHERE panel_id = ? AND event_type = ? AND enabled = 1''',
            (panel_id, event_type),
        )
        row = cur.fetchone()
        if not row:
            return None
        r = dict(row)
        r['embeds'] = _json_loads_list(r.get('embeds'))
        r['buttons'] = _json_loads_list(r.get('buttons'))
        return r

    def list_moderator_messages(self, panel_id: str) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT * FROM moderator_messages WHERE panel_id = ? ORDER BY event_type',
            (panel_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r['embeds'] = _json_loads_list(r.get('embeds'))
            r['buttons'] = _json_loads_list(r.get('buttons'))
        return rows

    def upsert_moderator_message(self, cfg: Dict) -> str:
        mid = cfg.get('message_id') or (str(_uuid.uuid4())[:8] if _uuid else f"mm-{int(datetime.now(timezone.utc).timestamp())}")
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO moderator_messages
                (message_id, panel_id, guild_id, event_type, content,
                 embeds, buttons, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                  content=excluded.content,
                  embeds=excluded.embeds,
                  buttons=excluded.buttons,
                  enabled=excluded.enabled,
                  updated_at=excluded.updated_at
            ''', (
                mid, cfg.get('panel_id'), cfg.get('guild_id'),
                cfg.get('event_type'), cfg.get('content'),
                _json_dumps(cfg.get('embeds', [])),
                _json_dumps(cfg.get('buttons', [])),
                int(bool(cfg.get('enabled', 1))),
                cfg.get('created_at') or datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()
        return mid

    def delete_moderator_message(self, message_id: str) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('DELETE FROM moderator_messages WHERE message_id = ?', (message_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- FLOW REVIEW CONFIG ----------
    def get_flow_review_config(self, flow_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM flow_review_config WHERE flow_id = ?', (flow_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_flow_review_config(self, cfg: Dict) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO flow_review_config
                (flow_id, guild_id, review_channel_id, reviewer_role_id,
                 auto_approve_minutes, auto_reject_minutes,
                 approved_panel_id, rejected_panel_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(flow_id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  review_channel_id=excluded.review_channel_id,
                  reviewer_role_id=excluded.reviewer_role_id,
                  auto_approve_minutes=excluded.auto_approve_minutes,
                  auto_reject_minutes=excluded.auto_reject_minutes,
                  approved_panel_id=excluded.approved_panel_id,
                  rejected_panel_id=excluded.rejected_panel_id,
                  updated_at=excluded.updated_at
            ''', (
                cfg.get('flow_id'), cfg.get('guild_id'),
                cfg.get('review_channel_id'), cfg.get('reviewer_role_id'),
                int(cfg.get('auto_approve_minutes', 0) or 0),
                int(cfg.get('auto_reject_minutes', 0) or 0),
                cfg.get('approved_panel_id'), cfg.get('rejected_panel_id'),
                datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()

    def add_review_submission(self, sub: Dict) -> str:
        rid = sub.get('review_id') or (str(_uuid.uuid4())[:8] if _uuid else f"rv-{int(datetime.now(timezone.utc).timestamp())}")
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO flow_review_queue
                (review_id, flow_id, ticket_id, guild_id, applicant_id,
                 answers, submitted_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            ''', (
                rid, sub.get('flow_id'), sub.get('ticket_id'),
                sub.get('guild_id'), sub.get('applicant_id'),
                _json_dumps(sub.get('answers', {})),
                sub.get('submitted_at') or datetime.now(timezone.utc).isoformat(),
            ))
            self._conn.commit()
        return rid

    def get_review_submission(self, review_id: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM flow_review_queue WHERE review_id = ?', (review_id,))
        row = cur.fetchone()
        if not row:
            return None
        r = dict(row)
        r['answers'] = _json_loads_dict(r.get('answers'))
        return r

    def list_pending_reviews(self, guild_id: int) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            '''SELECT * FROM flow_review_queue
               WHERE guild_id = ? AND status = 'pending'
               ORDER BY submitted_at''',
            (guild_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r['answers'] = _json_loads_dict(r.get('answers'))
        return rows

    def update_review_status(self, review_id: str, status: str,
                               reviewed_by: Optional[int], notes: Optional[str] = None) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                UPDATE flow_review_queue
                SET status = ?, reviewed_by = ?, reviewed_at = ?, review_notes = ?
                WHERE review_id = ? AND status = 'pending'
            ''', (status, reviewed_by,
                  datetime.now(timezone.utc).isoformat(), notes, review_id))
            self._conn.commit()
            return cur.rowcount > 0

    def set_review_message_id(self, review_id: str, message_id: int) -> bool:
        '''Record which review-channel message carries this submission's buttons.'''
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute(
                'UPDATE flow_review_queue SET review_message_id = ? WHERE review_id = ?',
                (message_id, review_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # === CANNED REPLIES (Ticket Tool /canned) ===

    def get_canned_reply(self, guild_id: int, name: str) -> Optional[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT * FROM canned_replies WHERE guild_id = ? AND name = ? AND is_active = 1',
            (guild_id, name),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_canned_replies(self, guild_id: int) -> List[Dict]:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT * FROM canned_replies WHERE guild_id = ? AND is_active = 1 ORDER BY name',
            (guild_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def upsert_canned_reply(self, reply: Dict) -> Tuple[str, bool]:
        '''Insert or update a canned reply by (guild_id, name).

        Returns (reply_id, created_new).'''
        existing = self.get_canned_reply(reply['guild_id'], reply['name'])
        now = datetime.now(timezone.utc).isoformat()
        if existing:
            with self.dm._lock:
                cur = self._conn.cursor()
                cur.execute(
                    'UPDATE canned_replies SET content = ?, updated_at = ? WHERE reply_id = ?',
                    (reply.get('content'), now, existing['reply_id']),
                )
                self._conn.commit()
            return existing['reply_id'], False
        reply_id = reply.get('reply_id') or ('cr-' + (_uuid.uuid4().hex[:10] if _uuid else str(int(datetime.now(timezone.utc).timestamp()))))
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute('''
                INSERT INTO canned_replies
                (reply_id, guild_id, name, content, created_by, uses, created_at, updated_at, is_active)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, 1)
            ''', (
                reply_id, reply['guild_id'], reply['name'], reply['content'],
                reply.get('created_by'), now, now,
            ))
            self._conn.commit()
        return reply_id, True

    def delete_canned_reply(self, guild_id: int, name: str) -> bool:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute(
                'DELETE FROM canned_replies WHERE guild_id = ? AND name = ?',
                (guild_id, name),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def record_canned_reply_use(self, reply_id: str) -> None:
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute(
                'UPDATE canned_replies SET uses = uses + 1 WHERE reply_id = ?',
                (reply_id,),
            )
            self._conn.commit()

    def shift_timers_for_ticket(self, ticket_id: str, seconds: int) -> int:
        '''Push every pending automation timer for a ticket into the future
        by `seconds` (used when a paused ticket resumes: the time already
        elapsed during the pause does NOT count toward the timer).

        Returns the number of timers shifted.'''
        if seconds <= 0:
            return 0
        from datetime import timedelta as _td
        now = datetime.now(timezone.utc)
        with self.dm._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT timer_id, fire_at FROM ticket_automation_timers "
                "WHERE ticket_id = ? AND fired = 0",
                (ticket_id,),
            )
            rows = cur.fetchall()
            shifted = 0
            for row in rows:
                try:
                    fire_at = datetime.fromisoformat(
                        str(row['fire_at']).replace('Z', '+00:00'))
                except (ValueError, TypeError):
                    continue
                new_fire = fire_at + _td(seconds=seconds)
                # Never schedule into the past even for already-overdue timers.
                if new_fire <= now:
                    new_fire = now + _td(seconds=seconds)
                cur.execute(
                    'UPDATE ticket_automation_timers SET fire_at = ? WHERE timer_id = ?',
                    (new_fire.isoformat(), row['timer_id']),
                )
                shifted += 1
            self._conn.commit()
            return shifted
