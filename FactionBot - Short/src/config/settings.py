# -*- coding: utf-8 -*-
"""Configuration — dataclass sub-configs (channel/role/timing/limits) plus the
Config facade that loads/saves them (SQLite-backed).

Extracted from the former single-file Bot.py during the SaaS restructure;
the class body is otherwise unchanged. ``data_manager`` references inside
the load/save methods resolve through deferred imports (cycle-safe).
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Callable, List

from core.paths import _DATA_DIR

# --- CONFIGURATION ---
@dataclass
class ChannelConfig:
    invite: int = 1471663457287012497
    welcome: int = 1471663455017767107
    rules: int = 1471663457287012497
    log: int = 1529562857602289776
    auto_scan: int = 1471663527247741001
    verification_main: int = 1471663452677476520
    reports: int = 1329157448564609056
    tickets: int = 1471663451561525522 # TICKET CATEGORY (DO NOT CHANGE)
    transcripts: int = 1471663530238546093 # TRANSCRIPTS CHANNEL


@dataclass
class RoleConfig:
    invite_manager: int = 1471663385568350282
    member: int = 1471663405877166090
    staff: int = 1471663388848423087
    verified: int = 1411440990408937642
    verification_ping: int = 1476162221632389220
    muted: int = 1471663391067078777
    ticket_support: int = 1476162221632389220
    server_tester: int = 1479065086420193392  # Set this to your Server Tester role ID


@dataclass
class TimingConfig:
    auto_scan_interval_hours: int = 2
    report_message_interval_minutes: int = 30
    report_timeout_seconds: int = 300


@dataclass
class LimitsConfig:
    min_account_age_days: int = 6
    min_blacklist_keyword_length: int = 2
    min_poll_options: int = 2
    max_poll_options: int = 10
    min_poll_duration: int = 30
    max_poll_duration: int = 86400
    max_warnings_before_ban: int = 3
    max_tickets_per_user: int = 3


class Config:
    def __init__(self):
        self.channels = ChannelConfig()
        self.roles = RoleConfig()
        self.timing = TimingConfig()
        self.limits = LimitsConfig()

        # Live timing-loop applier registry — populated by src/bot.py once
        # the @tasks.loop objects exist (see _register_timing_appliers) and
        # iterated by apply_timing_to_loops() on every !timingsetup save.
        self.timing_appliers: List[Callable[[], None]] = []

        # Runtime paths anchor to THIS variant's src/data so each
        # version keeps its own fully separate database, JSON data, and
        # lock file regardless of the launch directory (same convention as
        # utils.botkit.PROJECT_ROOT).
        self.data_dir = _DATA_DIR
        self.json_dir = os.path.join(self.data_dir, "JsonData")
        self.db_file = os.path.join(self.data_dir, "bot_data.db")
        self.lock_file = os.path.join(self.data_dir, "bot_busy.lock")

        self.tickets_data_file = os.path.join(self.json_dir, "tickets_data.json")
        self.branding_file = os.path.join(self.json_dir, "branding.json")
        self.channel_config_file = os.path.join(self.json_dir, "channel_config.json")

        self.enable_leveling = True
        self.enable_tickets = True
        self.enable_warnings = True

        self.command_prefix = "!"
        self.bot_status = "[GANG NAME] On Top"
        self.debug_mode = False
        self.gang_name = "[GANG NAME]"
        self.gang_abbreviation = "GANG"

    def ensure_directories(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.json_dir, exist_ok=True)

    def load_branding_settings(self) -> None:
        from core.state import data_manager  # deferred import (cycle-safe)
        try:
            payload = data_manager.get_config_value("branding")
            if not payload:
                return
            data = json.loads(payload)
            if isinstance(data, dict):
                self.gang_name = str(data.get("gang_name") or self.gang_name)
                self.gang_abbreviation = str(data.get("gang_abbreviation") or self.gang_abbreviation).upper()
                self.bot_status = str(data.get("bot_status") or self.bot_status)
        except Exception as exc:
            logging.warning(f"[Branding] Could not load branding config: {exc}")

    def save_branding_settings(self) -> None:
        from core.state import data_manager  # deferred import (cycle-safe)
        try:
            payload = json.dumps({
                "gang_name": self.gang_name,
                "gang_abbreviation": self.gang_abbreviation,
                "bot_status": self.bot_status,
            })
            data_manager.set_config_value("branding", payload)
        except Exception as exc:
            logging.warning(f"[Branding] Could not save branding config: {exc}")

    def load_channel_settings(self) -> None:
        from core.state import data_manager  # deferred import (cycle-safe)
        try:
            payload = data_manager.get_config_value("channels")
            if not payload:
                return
            data = json.loads(payload)
            if not isinstance(data, dict):
                return
            # NOTE: unknown keys are skipped on purpose. The old code also
            # probed self.servers here — an attribute this Config does not
            # have — so the first stray key raised AttributeError, the outer
            # except swallowed it, and every channel key AFTER the stray one
            # (reports / tickets / transcripts) was silently dropped.
            for key, value in data.items():
                if not isinstance(value, int):
                    continue
                if hasattr(self.channels, key):
                    setattr(self.channels, key, value)
        except Exception as exc:
            logging.warning(f"[Channels] Could not load channel config: {exc}")

    def save_channel_settings(self) -> None:
        from core.state import data_manager  # deferred import (cycle-safe)
        try:
            payload = json.dumps({
                "invite": self.channels.invite,
                "welcome": self.channels.welcome,
                "rules": self.channels.rules,
                "log": self.channels.log,
                "auto_scan": self.channels.auto_scan,
                "verification_main": self.channels.verification_main,
                "reports": self.channels.reports,
                "tickets": self.channels.tickets,
                "transcripts": self.channels.transcripts,
            })
            data_manager.set_config_value("channels", payload)
        except Exception as exc:
            logging.warning(f"[Channels] Could not save channel config: {exc}")

    # ------------------------------------------------------------------
    # ROLE CONFIG persistence (!rolesetup).
    # Stores every int role ID on RoleConfig under the "roles" bot_config
    # key. vars(self.roles) is used so newly-added RoleConfig fields are
    # picked up automatically without editing this method.
    # ------------------------------------------------------------------
    def load_role_settings(self) -> None:
        from core.state import data_manager  # deferred import (cycle-safe)
        try:
            payload = data_manager.get_config_value("roles")
            if not payload:
                return
            data = json.loads(payload)
            if not isinstance(data, dict):
                return
            for key, value in data.items():
                if isinstance(value, int) and hasattr(self.roles, key):
                    setattr(self.roles, key, value)
        except Exception as exc:
            logging.warning(f"[Roles] Could not load role config: {exc}")

    def save_role_settings(self) -> None:
        from core.state import data_manager  # deferred import (cycle-safe)
        try:
            payload = json.dumps(
                {k: v for k, v in vars(self.roles).items() if isinstance(v, int)}
            )
            data_manager.set_config_value("roles", payload)
        except Exception as exc:
            logging.warning(f"[Roles] Could not save role config: {exc}")

    # ------------------------------------------------------------------
    # TIMING CONFIG persistence (!timingsetup).
    # ------------------------------------------------------------------
    def load_timing_settings(self) -> None:
        from core.state import data_manager  # deferred import (cycle-safe)
        try:
            payload = data_manager.get_config_value("timing")
            if not payload:
                return
            data = json.loads(payload)
            if not isinstance(data, dict):
                return
            for key, value in data.items():
                if isinstance(value, int) and hasattr(self.timing, key):
                    setattr(self.timing, key, value)
        except Exception as exc:
            logging.warning(f"[Timing] Could not load timing config: {exc}")

    def save_timing_settings(self) -> None:
        from core.state import data_manager  # deferred import (cycle-safe)
        try:
            payload = json.dumps(
                {k: v for k, v in vars(self.timing).items() if isinstance(v, int)}
            )
            data_manager.set_config_value("timing", payload)
        except Exception as exc:
            logging.warning(f"[Timing] Could not save timing config: {exc}")

    def apply_timing_to_loops(self) -> None:
        """Re-apply the current `timing` values to the live task loops.

        `@tasks.loop(minutes=X)` captures X at decoration time, so changing
        the value in `config.timing` had no effect until restart. This method
        rebuilds the loops' intervals in-place. Safe to call from sync or
        async context — the underlying `change_interval` is a sync method.
        """
        try:
            # Appliers are registered on this instance by src/bot.py (see
            # _register_timing_appliers) once the task loops exist, so this
            # method never needs to import the entry module.
            for applier in self.timing_appliers:
                try:
                    applier()
                except Exception as exc:
                    logging.warning(f"[Timing] apply callback failed: {exc}")
        except Exception as exc:
            # Defensive: never let a timing refresh break a settings save.
            logging.warning(f"[Timing] apply_timing_to_loops failed: {exc}")

    # ------------------------------------------------------------------
    # LIMITS CONFIG persistence (!limitssetup).
    # All LimitsConfig fields are integers read at command-runtime, so
    # every change here applies immediately (no restart needed).
    # ------------------------------------------------------------------
    def load_limits_settings(self) -> None:
        from core.state import data_manager  # deferred import (cycle-safe)
        try:
            payload = data_manager.get_config_value("limits")
            if not payload:
                return
            data = json.loads(payload)
            if not isinstance(data, dict):
                return
            for key, value in data.items():
                if isinstance(value, int) and hasattr(self.limits, key):
                    setattr(self.limits, key, value)
        except Exception as exc:
            logging.warning(f"[Limits] Could not load limits config: {exc}")

    def save_limits_settings(self) -> None:
        from core.state import data_manager  # deferred import (cycle-safe)
        try:
            payload = json.dumps(
                {k: v for k, v in vars(self.limits).items() if isinstance(v, int)}
            )
            data_manager.set_config_value("limits", payload)
        except Exception as exc:
            logging.warning(f"[Limits] Could not save limits config: {exc}")