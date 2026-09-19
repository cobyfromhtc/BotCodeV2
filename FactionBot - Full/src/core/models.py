# -*- coding: utf-8 -*-
"""Domain models — enums + persistence dataclasses (Warning, Ticket,
Giveaway, UserLevel)."""

# stdlib + discord.py
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import List

from core.helpers import compute_level_from_xp, xp_for_level



# Roblox in-game verification support removed; server-only verification flow is used.


# --- ENUMERATIONS ---
class VerificationStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    PENDING_INFO = "pending_info"


class TicketStatus(Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class GiveawayStatus(Enum):
    ACTIVE = "active"
    ENDED = "ended"
    CANCELLED = "cancelled"


class WarningType(Enum):
    SPAM = "spam"
    HARASSMENT = "harassment"
    TOXIC = "toxic"
    RAID = "raid"
    ADVERTISING = "advertising"
    NSFW = "nsfw"
    CUSTOM = "custom"

# --- DATA MODELS ---
@dataclass
class Warning:
    warning_id: str = ""
    user_id: int = 0
    guild_id: int = 0
    moderator_id: int = 0
    warning_type: WarningType = WarningType.CUSTOM
    reason: str = ""
    points: int = 1
    created_at: datetime = None
    expires_at: datetime = None
    is_active: bool = True
    
    def __post_init__(self):
        if not self.warning_id:
            import uuid
            self.warning_id = str(uuid.uuid4())[:8]
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)


@dataclass
class Ticket:
    ticket_id: str = ""
    channel_id: int = 0
    guild_id: int = 0
    creator_id: int = 0
    category: str = "general"
    subject: str = ""
    status: TicketStatus = TicketStatus.OPEN
    assigned_to: int = None
    created_at: datetime = None
    
    def __post_init__(self):
        if not self.ticket_id:
            import uuid
            self.ticket_id = str(uuid.uuid4())[:8]
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)


@dataclass
class Giveaway:
    giveaway_id: str = ""
    message_id: int = 0
    channel_id: int = 0
    guild_id: int = 0
    host_id: int = 0
    prize: str = ""
    winner_count: int = 1
    entries: List[int] = None
    winners: List[int] = None
    status: GiveawayStatus = GiveawayStatus.ACTIVE
    created_at: datetime = None
    ends_at: datetime = None
    
    def __post_init__(self):
        if not self.giveaway_id:
            import uuid
            self.giveaway_id = str(uuid.uuid4())[:8]
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)
        if self.entries is None:
            self.entries = []
        if self.winners is None:
            self.winners = []


@dataclass
class UserLevel:
    user_id: int = 0
    guild_id: int = 0
    xp: int = 0
    level: int = 0
    total_messages: int = 0
    last_xp_gain: datetime = None
    
    @property
    def xp_for_next_level(self) -> int:
        # Delegates to the centralized curve helper so the curve never drifts.
        return xp_for_next_level(self.level)

    @property
    def xp_progress(self) -> float:
        xp_needed = self.xp_for_next_level
        current_level_xp = xp_for_level(self.level)
        xp_in_current_level = self.xp - current_level_xp
        xp_for_next = xp_needed - current_level_xp
        return min(100.0, (xp_in_current_level / xp_for_next) * 100) if xp_for_next > 0 else 100.0

    def add_xp(self, amount: int) -> bool:
        self.xp += amount
        self.total_messages += 1
        # Recompute the level from total XP so multiple level-ups in one gain
        # are handled correctly.
        new_level = compute_level_from_xp(self.xp)
        if new_level > self.level:
            self.level = new_level
            return True
        return False
