# -*- coding: utf-8 -*-
"""Shared helpers — escaping, level curve, branding text,
uptime/event logging. Import-order safe: only depends on core.*."""

# stdlib + discord.py
import discord
import html as _html
import logging
import re
import time
from datetime import timedelta
from typing import Optional
from urllib.parse import urlparse

from core.state import config, start_time

# package availability flags (always True — packages are real modules now)
PREMIUM_AVAILABLE = True
RR_AVAILABLE = True




def _esc(value) -> str:
    """HTML-escape user-controlled text for safe embedding in transcript HTML."""
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _safe_url(url) -> str:
    """Return the URL only if it parses to a safe http(s) scheme, else empty."""
    if not url:
        return ""
    try:
        parsed = urlparse(str(url))
    except Exception:
        return ""
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return _esc(str(url))
    return ""

try:
    import aiosqlite
    SQLITE_AVAILABLE = True
except ImportError:
    SQLITE_AVAILABLE = False

try:
    from flask import Flask
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False


# (setup_logging() was moved to the top of the file — line 64 — so all
# startup errors are visible. The old definition here has been removed.)


# =============================================================================
# SHARED HELPERS
# =============================================================================
# --- XP / Leveling curve (centralized so the curve can never drift) ---
# Gentler curve: level N requires (N ** 1.5) * 100 cumulative XP.
# At 5-15 XP/message this keeps leveling achievable instead of taking
# thousands of messages to reach mid levels.
XP_CURVE_BASE: int = 100
XP_CURVE_EXPONENT: float = 1.5


def xp_for_level(level: int) -> int:
    """Total cumulative XP required to REACH a given level (level 0 == 0 XP)."""
    if level <= 0:
        return 0
    return int(level ** XP_CURVE_EXPONENT * XP_CURVE_BASE)


def xp_for_next_level(level: int) -> int:
    """Total cumulative XP required to reach the level AFTER `level`."""
    return xp_for_level(level + 1)


def compute_level_from_xp(xp: int) -> int:
    """Given total XP, return the highest level the user has achieved."""
    level = 0
    while xp >= xp_for_next_level(level):
        level += 1
        # Safety valve to avoid an infinite loop on pathological input.
        if level > 10_000:
            break
    return level


def _is_negative_answer(text: str) -> bool:
    """
    Word-boundary-safe check for a negative / empty answer.
    Fixes the old substring bug where 'na' matched 'natural', 'banana',
    'nathan', 'not' matched 'noted', etc.
    """
    if text is None:
        return True
    normalized = re.sub(r"[^a-z0-9\s/]", " ", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return True

    negative_words = {
        "no", "none", "na", "n/a", "not", "idk", "nil", "nope",
        "nah", "negative", "nothing",
    }
    words = set(normalized.split())
    if words & negative_words:
        return True

    # Multi-word negative phrases.
    for phrase in ("not sure", "i dont know", "i don t know",
                   "none of the above", "dont know", "don t know"):
        if phrase in normalized:
            return True
    return False


def brand_text(text: str) -> str:
    """
    Rebrand hardcoded legacy names AND [GANG NAME]/[GANG ABBR] placeholders
    with the configured gang name / abbreviation.

    Uses unique sentinel placeholders so that a configured gang name or
    abbreviation containing a legacy token can never re-trigger another
    replacement and corrupt the text.
    """
    if not text:
        return text

    # Sentinels for legacy tokens
    PH_FULL_1 = "\x00\x01BRAND_FULL_1\x01\x00"   # "Mask Off Society"
    PH_FULL_2 = "\x00\x01BRAND_FULL_2\x01\x00"   # "Shoot On Sight"
    PH_FULL_3 = "\x00\x01BRAND_FULL_3\x01\x00"   # "SOS"
    PH_ABBR   = "\x00\x01BRAND_ABBR\x01\x00"     # "MOS"
    PH_SERVER = "\x00\x01BRAND_SERVER\x01\x00"   # "VPRP"
    # Sentinels for [GANG NAME] / [GANG ABBR] placeholders
    PH_PH_FULL = "\x00\x01BRAND_PH_FULL\x01\x00"
    PH_PH_ABBR = "\x00\x01BRAND_PH_ABBR\x01\x00"

    # 1) Replace every token with a unique sentinel.
    result = text.replace("Mask Off Society", PH_FULL_1)
    result = result.replace("Shoot On Sight", PH_FULL_2)
    result = result.replace("[GANG NAME]", PH_PH_FULL)       # NEW
    result = result.replace("[GANG ABBR]", PH_PH_ABBR)       # NEW
    result = result.replace("SOS", PH_FULL_3)
    result = result.replace("MOS", PH_ABBR)
    result = result.replace("VPRP", PH_SERVER)

    # 2) Resolve sentinels to configured values exactly once.
    result = result.replace(PH_FULL_1, config.gang_name)
    result = result.replace(PH_FULL_2, config.gang_name)
    result = result.replace(PH_FULL_3, config.gang_name)
    result = result.replace(PH_PH_FULL, config.gang_name)   # NEW
    result = result.replace(PH_PH_ABBR, config.gang_abbreviation)  # NEW
    result = result.replace(PH_ABBR, config.gang_abbreviation)
    result = result.replace(PH_SERVER, "Server")
    return result




# --- UTILITY FUNCTIONS ---
def get_uptime() -> str:
    return str(timedelta(seconds=int(time.time() - start_time)))


def log_event(event_type: str, user: discord.User, details: Optional[str] = None) -> None:
    log_message = f"{event_type} - User: {user} (ID: {user.id})"
    if details:
        log_message += f" | Details: {details}"
    logging.info(log_message)
