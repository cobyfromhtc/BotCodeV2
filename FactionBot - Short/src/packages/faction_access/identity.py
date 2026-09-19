# -*- coding: utf-8 -*-
'''
FactionAccess.identity — per-guild faction identity model + text branding.

A guild's identity is the trio the owner configures per allied faction:

    gang_tag       short abbreviation (e.g. "ALLY") — substitutes [GANG ABBR]
    gang_name      full name (e.g. "Ally Faction")  — substitutes [GANG NAME]
    display_name   the bot's NICKNAME in that guild (e.g. "ALLY Moderation")
                   — Discord nicknames are per-guild, so the same bot user
                   can present differently in every server

Resolution order used everywhere (see ``FactionAccessService.identity_for``):
    1. the guild's faction_identity row (allied override)
    2. the process-global config values (config.gang_name / abbr — the home
       faction's identity, which is also what DMs and the global presence use)
    3. safe placeholder defaults

``substitute()`` is the parameterized twin of ``core.helpers.brand_text``:
same sentinel technique (a configured name containing a legacy token can
never re-trigger a second replacement), but parameterized by the RESOLVED
per-guild names instead of reading the global config.
'''

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Placeholders substituted by substitute() — kept in sync with the global
# brand_text() tokens so templates work identically in both call sites.
PLACEHOLDER_FULL: str = "[GANG NAME]"
PLACEHOLDER_ABBR: str = "[GANG ABBR]"

# Legacy hardcoded names still found in old templates; they resolve to the
# per-guild gang name / abbreviation exactly like brand_text() does.
_LEGACY_FULL_TOKENS: tuple = ("Mask Off Society", "Shoot On Sight", "SOS")
_LEGACY_ABBR_TOKENS: tuple = ("MOS",)
_LEGACY_SERVER_TOKEN: str = "VPRP"

# Discord nickname hard limit.
MAX_NICKNAME_LENGTH: int = 32


@dataclass(frozen=True)
class FactionIdentity:
    """Immutable resolved identity for one guild (or the global fallback)."""

    guild_id: Optional[int]
    gang_tag: str
    gang_name: str
    display_name: Optional[str]
    is_override: bool = False   # True when a faction_identity row supplied it

    @property
    def tag(self) -> str:
        return self.gang_tag

    def qualified_name(self) -> str:
        """"[TAG] Full Name" — the signature string ("[ALLY] Ally Faction")."""
        if self.gang_tag and self.gang_name:
            return f"[{self.gang_tag}] {self.gang_name}"
        return self.gang_name or self.gang_tag or "FactionBot"

    def moderator_name(self) -> str:
        """"{Tag} Moderation" — the display-nickname convention."""
        base = self.gang_tag or (self.gang_name or "Faction")
        return f"{base} Moderation"


def clamp_nickname(nickname: Optional[str]) -> Optional[str]:
    """Trim a nickname to Discord's limit, or None when empty/whitespace."""
    if nickname is None:
        return None
    trimmed = str(nickname).strip()
    if not trimmed:
        return None
    return trimmed[:MAX_NICKNAME_LENGTH]


def substitute(text: Optional[str], gang_name: str, gang_abbr: str) -> Optional[str]:
    """Rebrand legacy names AND [GANG NAME]/[GANG ABBR] placeholders with the
    RESOLVED per-guild identity values.

    Uses unique sentinel placeholders so a configured name that itself
    contains a legacy token can never re-trigger another replacement and
    corrupt the text (same technique as core.helpers.brand_text).
    """
    if not text:
        return text

    # 1) Replace every token with a unique sentinel.
    sentinels_full = tuple(f"\x00\x01FA_FULL_{index}\x01\x00" for index in range(len(_LEGACY_FULL_TOKENS) + 1))
    sentinel_abbr = "\x00\x01FA_ABBR_0\x01\x00"
    sentinel_abbr_ph = "\x00\x01FA_ABBR_PH\x01\x00"
    sentinel_server = "\x00\x01FA_SERVER\x01\x00"

    result = text
    for token, sentinel in zip(_LEGACY_FULL_TOKENS, sentinels_full):
        result = result.replace(token, sentinel)
    result = result.replace(PLACEHOLDER_FULL, sentinels_full[-1])
    for token in _LEGACY_ABBR_TOKENS:
        result = result.replace(token, sentinel_abbr)
    result = result.replace(PLACEHOLDER_ABBR, sentinel_abbr_ph)
    result = result.replace(_LEGACY_SERVER_TOKEN, sentinel_server)

    # 2) Resolve sentinels to the per-guild values exactly once.
    for sentinel in sentinels_full:
        result = result.replace(sentinel, gang_name)
    result = result.replace(sentinel_abbr, gang_abbr)
    result = result.replace(sentinel_abbr_ph, gang_abbr)
    result = result.replace(sentinel_server, "Server")
    return result
