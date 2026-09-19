# -*- coding: utf-8 -*-
"""botkit — shared helpers for FactionBot modules.

Every cog uses this module for:
  * DB access (same SQLite file as the core DataManager: src/data/bot_data.db,
    WAL journal, busy_timeout — safe alongside the main bot's connection)
  * consistent embed styling
  * small utilities (time formatting, JSON list columns, hierarchy checks)

Modules must NEVER import from src/bot.py (circular import) — anything they
need from the core is either in here or stashed on the bot instance
(bot.embed_builder, bot.ticket_tool, ...).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import discord

# ---------------------------------------------------------------------------
# Paths & DB
# ---------------------------------------------------------------------------
# Anchor every path to this file's real location so the cogs and the core
# bot agree on ONE database file regardless of the launch CWD. Previously
# `Bot.py` used a *relative* "data/bot_data.db"; if the process was started
# from a different directory, the two paths pointed at two different files.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
DB_PATH: Path = PROJECT_ROOT / "src" / "data" / "bot_data.db"

_brand_footer: Optional[str] = None  # cached footer text (set by core at load)

# PERFORMANCE (Phase 2): per-thread connection cache.
# get_conn() used to open a NEW connection + run 3 pragmas + close it for
# EVERY run()/fetchall()/fetchone() call — cogs hit that on every command
# and loop tick. Threads here are the event-loop thread plus asyncio
# to_thread workers; a sqlite3 connection created with check_same_thread
# semantics (default) must only be used from its owning thread, which a
# thread-local cache guarantees by construction. Connections stay open
# for the process lifetime — WAL + busy_timeout keep cross-connection
# behaviour identical to the previous per-call connections.
_THREAD_LOCAL = threading.local()


def get_conn() -> sqlite3.Connection:
    """Return this thread's cached SQLite connection with standard pragmas.

    The connection is created lazily on first use per thread and reused for
    that thread's lifetime (see _THREAD_LOCAL note above). run()/fetchall()
    helpers no longer close it.
    """
    conn = getattr(_THREAD_LOCAL, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _THREAD_LOCAL.conn = conn
    return conn


def run(sql: str, args: Sequence[Any] = ()) -> None:
    conn = get_conn()
    conn.execute(sql, tuple(args))
    conn.commit()


def fetchall(sql: str, args: Sequence[Any] = ()) -> List[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(sql, tuple(args)).fetchall()


def fetchone(sql: str, args: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(sql, tuple(args)).fetchone()


def create_tables(statements: Sequence[str]) -> None:
    """Idempotently create tables (each statement must be IF NOT EXISTS)."""
    conn = get_conn()
    for s in statements:
        conn.execute(s)
    conn.commit()


# ---------------------------------------------------------------------------
# JSON column helpers (the DB stores lists/dicts as JSON text)
# ---------------------------------------------------------------------------
def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def jload(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    days = seconds // 86400
    return f"{days}d {(seconds % 86400) // 3600}h"


def fmt_dt(value: Optional[datetime]) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return f"<t:{int(value.timestamp())}:R>"


# ---------------------------------------------------------------------------
# Embeds (consistent FactionBot styling)
# ---------------------------------------------------------------------------
COLOR_SUCCESS = discord.Color.from_str("#2ECC71")
COLOR_ERROR = discord.Color.from_str("#E74C3C")
COLOR_INFO = discord.Color.from_str("#3498DB")
COLOR_WARNING = discord.Color.from_str("#F39C12")
COLOR_NEUTRAL = discord.Color.from_str("#95A5A6")
COLOR_BRAND = discord.Color.from_str("#9B59B6")


def set_footer(text: Optional[str]) -> None:
    global _brand_footer
    _brand_footer = text


def _base(title: str, description: str, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color,
                          timestamp=datetime.now(timezone.utc))
    if _brand_footer:
        embed.set_footer(text=_brand_footer)
    return embed


def success(title: str, description: str = "") -> discord.Embed:
    return _base(f"✅ {title}" if title else title, description, COLOR_SUCCESS)


def error(title: str, description: str = "") -> discord.Embed:
    return _base(f"❌ {title}" if title else title, description, COLOR_ERROR)


def info(title: str, description: str = "") -> discord.Embed:
    return _base(f"ℹ️ {title}" if title else title, description, COLOR_INFO)


def warning(title: str, description: str = "") -> discord.Embed:
    return _base(f"⚠️ {title}" if title else title, description, COLOR_WARNING)


def neutral(title: str, description: str = "") -> discord.Embed:
    return _base(title, description, COLOR_NEUTRAL)


# ---------------------------------------------------------------------------
# Permission / hierarchy helpers
# ---------------------------------------------------------------------------
def role_ids_for(member: discord.Member) -> set:
    return {r.id for r in member.roles}


def is_exempt(member: discord.Member, *,
              channels: Sequence[int] = (),
              channel_id: int = 0,
              roles: Sequence[int] = (),
              users: Sequence[int] = ()) -> bool:
    """True when the member/channel is exempt from a rule."""
    if member.bot:
        return True
    if member.id in set(users):
        return True
    if member.guild.owner_id == member.id:
        return True
    if member.guild_permissions.administrator:
        return True
    if roles and (role_ids_for(member) & set(roles)):
        return True
    if channel_id and channel_id in set(channels):
        return True
    return False


def can_act_on(actor: discord.Member, target: discord.Member) -> tuple:
    """(ok, reason) — validate moderation hierarchy for actor → target."""
    if target.bot and target.id != actor.guild.me.id:
        return False, "I can't moderate other bots."
    if target.id == actor.id:
        return False, "You can't target yourself."
    if target.id == target.guild.owner_id:
        return False, "That member owns this server."
    if target.top_role >= actor.top_role and actor.id != target.guild.owner_id:
        return False, "Your role is not high enough to target that member."
    if target.top_role >= target.guild.me.top_role:
        return False, "My role is not high enough to act on that member."
    return True, ""


def bot_can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return me.guild_permissions.manage_roles and role < me.top_role


def parse_duration(text: str) -> Optional[int]:
    """Parse '10m', '2h', '1d', '45s', or plain seconds → seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*(s|m|h|d|w)?\s*", text.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2) or "s"
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return n * mult
