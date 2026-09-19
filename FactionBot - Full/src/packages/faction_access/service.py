# -*- coding: utf-8 -*-
'''
FactionAccess.service — the licensing business-logic service.

One instance is created in ``FactionAccess.wiring.on_setup_hook`` and stashed
as ``bot.faction_access`` (plus ``state.faction_access`` so lower layers can
resolve per-guild identity without importing this package). It owns:

    * settings            home guild id + license-authority allowlist
    * license lifecycle   pending → licensed → suspended/revoked/left
    * feature grants      per-guild bundle enablement
    * identity resolution per-guild gang tag / name / bot nickname
    * notifications       DMs to the license authority (join requests,
                          access requests, expiry suspensions)
    * audit trail         every mutation appended via FactionAccessDB

Everything that mutates state goes through this service so the audit trail
can never be bypassed.
'''

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord

from . import catalog
from .db import (
    FactionAccessDB,
    STATUS_HOME,
    STATUS_PENDING,
    STATUS_LICENSED,
    STATUS_SUSPENDED,
    STATUS_REVOKED,
    STATUS_LEFT,
    ALL_STATUSES,
)
from .identity import FactionIdentity, clamp_nickname, substitute as substitute_text

# How long a "not licensed / not granted" notice is suppressed per
# (guild, user) so a denied command can't be used to spam the channel.
NOTICE_THROTTLE_SECONDS: float = 10.0

# OAuth scopes/permissions the bot asks an allied guild to grant. Explicit
# least-privilege-ish set: everything the feature suites need, no Administrator.
INVITE_PERMISSIONS = discord.Permissions()
INVITE_PERMISSIONS.update(
    view_channel=True,
    send_messages=True,
    send_messages_in_threads=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
    add_reactions=True,
    use_external_emojis=True,
    manage_messages=True,
    manage_channels=True,
    manage_roles=True,
    manage_nicknames=True,
    kick_members=True,
    ban_members=True,
    moderate_members=True,
)


def now_iso() -> str:
    """UTC timestamp in the same ISO format used across the faction tables."""
    return datetime.now(timezone.utc).isoformat()


def parse_duration(text: str) -> Optional[int]:
    """Parse '30d', '12h', '45m', '7d12h' into seconds (None when invalid)."""
    import re
    total = 0
    matched = False
    for amount, unit in re.findall(r"(\d+)\s*(d|h|m)", str(text).strip().lower()):
        matched = True
        seconds = int(amount) * {"d": 86400, "h": 3600, "m": 60}[unit]
        total += seconds
    return total if matched else None


class FactionAccessService:
    """Stateful licensing service (one instance per bot process)."""

    def __init__(self, bot, db: FactionAccessDB, config) -> None:
        self._bot = bot
        self._db = db
        self._config = config          # core.state.config (global identity fallback)
        # settings (hydrated from the faction_access bot_config key)
        self._home_guild_id: Optional[int] = None
        self._authority: List[int] = []
        # application owner id (cached after the first fetch)
        self._app_owner_id: Optional[int] = None
        # command classification maps (built once in wiring.on_ready_hook)
        self._top_map: Dict[str, str] = {}
        self._qualified_map: Dict[str, str] = {}
        self._map_ready: bool = False
        # notice throttle: (guild_id, user_id) -> monotonic ts
        self._notice_ts: Dict[Tuple[int, int], float] = {}

    # =================================================================
    # Hydration + settings
    # =================================================================

    def hydrate(self) -> None:
        """Load settings + rows into the DB caches and mirror them here."""
        settings = self._db.hydrate()
        home = settings.get("home_guild_id")
        self._home_guild_id = int(home) if isinstance(home, int) else None
        authority = settings.get("authority")
        self._authority = [int(a) for a in authority if isinstance(a, int)] if isinstance(authority, list) else []
        logging.info(
            "[faction_access] hydrated: home_guild_id=%s, authority_allowlist=%d member(s), %d guild row(s)",
            self._home_guild_id, len(self._authority), len(self._db.list_guilds()),
        )

    def _settings_payload(self) -> Dict[str, Any]:
        return {
            "home_guild_id": self._home_guild_id,
            "authority": list(self._authority),
        }

    def _save_settings(self) -> None:
        self._db.save_settings(self._settings_payload())

    # =================================================================
    # Authority
    # =================================================================

    async def ensure_app_owner(self) -> Optional[int]:
        """Fetch + cache the Discord application owner's user id."""
        if self._app_owner_id is not None:
            return self._app_owner_id
        try:
            info = await self._bot.application_info()
            if info is not None and info.owner is not None:
                self._app_owner_id = info.owner.id
                return self._app_owner_id
        except (discord.HTTPException, discord.Forbidden) as exc:
            logging.warning(f"[faction_access] application_info failed: {exc}")
        return None

    async def is_authority(self, user) -> bool:
        """True for the application owner or an allowlisted license authority.

        The allowlist lives in the DB (``!license authority add``), never in
        code — adding a second person with license authority is a settings
        change, not a deployment.
        """
        if user is None or getattr(user, "bot", False):
            return False
        if user.id in self._authority:
            return True
        owner_id = await self.ensure_app_owner()
        return owner_id is not None and user.id == owner_id

    def authority_ids(self) -> List[int]:
        """Every authority id known WITHOUT a network call (owner may be
        unresolved until the first ensure_app_owner())."""
        ids = list(self._authority)
        if self._app_owner_id is not None and self._app_owner_id not in ids:
            ids.append(self._app_owner_id)
        return ids

    def authority_add(self, user_id: int, actor_id: Optional[int]) -> bool:
        if user_id in self._authority:
            return False
        self._authority.append(int(user_id))
        self._save_settings()
        self.audit(actor_id, "authority_add", None, f"user {user_id}")
        return True

    def authority_remove(self, user_id: int, actor_id: Optional[int]) -> bool:
        if user_id not in self._authority:
            return False
        self._authority.remove(int(user_id))
        self._save_settings()
        self.audit(actor_id, "authority_remove", None, f"user {user_id}")
        return True

    # =================================================================
    # Home guild
    # =================================================================

    @property
    def home_guild_id(self) -> Optional[int]:
        return self._home_guild_id

    def is_home(self, guild_id: Optional[int]) -> bool:
        if guild_id is None or self._home_guild_id is None:
            return False
        return int(guild_id) == int(self._home_guild_id)

    def set_home(self, guild_id: int, actor_id: Optional[int]) -> None:
        previous = self._home_guild_id
        self._home_guild_id = int(guild_id)
        self._save_settings()
        # Demote any stale home row back to a normal licensed row.
        if previous is not None and previous != guild_id:
            row = self._db.get_guild(previous)
            if row is not None and row.get("status") == STATUS_HOME:
                self._db.upsert_guild(previous, status=STATUS_LICENSED,
                                      licensed_by=actor_id, updated_at=now_iso())
        self._db.upsert_guild(guild_id, status=STATUS_HOME,
                              licensed_by=actor_id, updated_at=now_iso())
        self.audit(actor_id, "home_set", guild_id,
                   f"previous home: {previous if previous is not None else 'unset'}")

    def adopt_home(self) -> Optional[int]:
        """Determine + persist the home guild when no explicit setting exists.

        Precedence (mirrors how the bot's own config is bound to the home
        faction): the configured gang server id when the bot is in it, then
        the guild that owns the largest member count (single-guild installs
        resolve here immediately). Never guessed silently — the choice is
        audited and adjustable with ``!license home``.
        """
        if self._home_guild_id is not None:
            self._db.upsert_guild(self._home_guild_id, status=STATUS_HOME, updated_at=now_iso())
            return self._home_guild_id
        guilds = list(self._bot.guilds)
        if not guilds:
            return None

        chosen = None
        servers_cfg = getattr(self._config, "servers", None)
        gang_server_id = getattr(servers_cfg, "gang_server_id", None) if servers_cfg is not None else None
        if gang_server_id is not None:
            for guild in guilds:
                if guild.id == int(gang_server_id):
                    chosen = guild
                    break
        if chosen is None:
            chosen = max(guilds, key=lambda g: g.member_count or 0)

        self._home_guild_id = chosen.id
        self._save_settings()
        self._db.upsert_guild(chosen.id, status=STATUS_HOME,
                              joined_at=now_iso(), updated_at=now_iso())
        self.audit(None, "home_adopted", chosen.id,
                   f"auto-adopted ({chosen.name}, {chosen.member_count or '?'} members)")
        logging.info(f"[faction_access] home guild adopted: {chosen.name} ({chosen.id})")
        return chosen.id

    # =================================================================
    # License lifecycle
    # =================================================================

    def status(self, guild_id: Optional[int]) -> Optional[str]:
        if guild_id is None:
            return None
        if self.is_home(guild_id):
            return STATUS_HOME
        row = self._db.get_guild(int(guild_id))
        return row.get("status") if row is not None else None

    def status_label(self, guild_id: Optional[int]) -> str:
        status = self.status(guild_id)
        if status == STATUS_HOME:
            return "🏠 Home faction (full access)"
        labels = {
            STATUS_PENDING: "🕓 Pending approval",
            STATUS_LICENSED: "✅ Licensed",
            STATUS_SUSPENDED: "⏸️ Suspended",
            STATUS_REVOKED: "🚫 Revoked",
            STATUS_LEFT: "👋 Left (re-approval required)",
        }
        return labels.get(status, "❔ Unregistered")

    def guild_is_licensed(self, guild_id: Optional[int]) -> bool:
        return self.status(guild_id) in (STATUS_HOME, STATUS_LICENSED)

    def register_join(self, guild: discord.Guild) -> Dict:
        """Record a guild join (on_guild_join or startup sweep of unknowns)."""
        row = self._db.get_guild(guild.id)
        if row is None:
            row = self._db.upsert_guild(
                guild.id, status=STATUS_PENDING, joined_at=now_iso(), updated_at=now_iso(),
            )
            self.audit(None, "guild_joined", guild.id,
                       f"{guild.name} — {guild.member_count or '?'} members")
        else:
            # Known guild re-invited (e.g. previously 'left'): keep history,
            # but require a fresh approval before anything unlocks again.
            if row.get("status") == STATUS_LEFT:
                self._db.upsert_guild(guild.id, status=STATUS_PENDING, updated_at=now_iso())
                self.audit(None, "guild_rejoined", guild.id, guild.name)
        return self._db.get_guild(guild.id) or row

    def register_leave(self, guild_id: int, guild_name: str) -> None:
        row = self._db.get_guild(guild_id)
        if row is None or row.get("status") == STATUS_HOME:
            return
        self._db.upsert_guild(guild_id, status=STATUS_LEFT, updated_at=now_iso())
        self.audit(None, "guild_left", guild_id, guild_name)

    def approve(self, guild_id: int, actor_id: Optional[int],
                bundles: Optional[List[str]] = None,
                expires_at: Optional[str] = None) -> Dict:
        """Approve a pending (or suspended/revoked) guild; optionally grant
        bundles in the same action."""
        self._db.upsert_guild(
            guild_id, status=STATUS_LICENSED,
            licensed_by=actor_id, licensed_at=now_iso(),
            expires_at=expires_at, updated_at=now_iso(),
        )
        granted: List[str] = []
        for bundle in (bundles or []):
            self._db.set_feature(guild_id, bundle, True, actor_id, now_iso())
            granted.append(bundle)
        self.audit(actor_id, "approve", guild_id,
                   f"bundles: {', '.join(granted) if granted else 'none'}"
                   + (f"; expires {expires_at}" if expires_at else ""))
        return self._db.get_guild(guild_id) or {}

    def deny(self, guild_id: int, actor_id: Optional[int], reason: Optional[str]) -> None:
        """Reject a pending join request."""
        self._db.upsert_guild(guild_id, status=STATUS_REVOKED,
                              notes=reason, updated_at=now_iso())
        self.audit(actor_id, "deny", guild_id, reason or "")

    def revoke(self, guild_id: int, actor_id: Optional[int], reason: Optional[str]) -> int:
        """Revoke a license outright: status flips and every grant is removed."""
        self._db.upsert_guild(guild_id, status=STATUS_REVOKED,
                              notes=reason, expires_at=None, updated_at=now_iso())
        removed = self._db.drop_features(guild_id)
        self.audit(actor_id, "revoke", guild_id, f"{reason or ''} (dropped {removed} grant(s))".strip())
        return removed

    def suspend(self, guild_id: int, actor_id: Optional[int], reason: Optional[str]) -> None:
        self._db.upsert_guild(guild_id, status=STATUS_SUSPENDED,
                              notes=reason, updated_at=now_iso())
        self.audit(actor_id, "suspend", guild_id, reason or "")

    def resume(self, guild_id: int, actor_id: Optional[int]) -> None:
        self._db.upsert_guild(guild_id, status=STATUS_LICENSED, updated_at=now_iso())
        self.audit(actor_id, "resume", guild_id, "")

    def set_expiry(self, guild_id: int, actor_id: Optional[int],
                   seconds: Optional[int]) -> Optional[str]:
        """Set (or clear with seconds=None) a license expiry; returns the ISO
        value now in force."""
        expires = None
        if seconds is not None and seconds > 0:
            expires = datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat()
        self._db.upsert_guild(guild_id, expires_at=expires, updated_at=now_iso())
        self.audit(actor_id, "expiry", guild_id, expires or "off")
        return expires

    def sweep_expiries(self) -> List[Dict]:
        """Suspend every licensed guild whose expiry has passed; returns them."""
        due = self._db.guilds_with_expiry_due(now_iso())
        for row in due:
            self._db.upsert_guild(int(row["guild_id"]), status=STATUS_SUSPENDED,
                                  notes="license expired", updated_at=now_iso())
            self.audit(None, "expiry_suspend", int(row["guild_id"]), "automatic")
        return due

    # =================================================================
    # Feature grants + gating helpers
    # =================================================================

    def grant(self, guild_id: int, bundles: List[str], actor_id: Optional[int]) -> List[str]:
        granted: List[str] = []
        for bundle in bundles:
            if bundle not in catalog.FEATURE_BUNDLES:
                continue
            self._db.set_feature(guild_id, bundle, True, actor_id, now_iso())
            granted.append(bundle)
        if granted:
            self.audit(actor_id, "grant", guild_id, ", ".join(granted))
        return granted

    def ungrant(self, guild_id: int, bundles: List[str], actor_id: Optional[int]) -> List[str]:
        removed: List[str] = []
        for bundle in bundles:
            if bundle not in catalog.FEATURE_BUNDLES:
                continue
            self._db.set_feature(guild_id, bundle, False, actor_id, now_iso())
            removed.append(bundle)
        if removed:
            self.audit(actor_id, "ungrant", guild_id, ", ".join(removed))
        return removed

    def feature_enabled(self, guild_id: Optional[int], bundle_key: str) -> bool:
        """May this guild use commands classified under `bundle_key`?

        Home guild: everything. Licensed guild: only explicitly granted
        bundles. Everything else (pending/suspended/revoked/unregistered):
        nothing — the caller already filtered ALWAYS_AVAILABLE commands.
        """
        if guild_id is None:
            return False
        if self.is_home(guild_id):
            return True
        if self.status(guild_id) != STATUS_LICENSED:
            return False
        return self._db.get_features(int(guild_id)).get(bundle_key, False)

    def set_command_map(self, top_map: Dict[str, str], qualified_map: Dict[str, str]) -> None:
        self._top_map = top_map
        self._qualified_map = qualified_map
        self._map_ready = True

    @property
    def map_ready(self) -> bool:
        return self._map_ready

    def classify(self, command) -> Optional[str]:
        """Bundle key for the invoked command (qualified overrides first)."""
        qualified = getattr(command, "qualified_name", None)
        if qualified is not None and qualified in self._qualified_map:
            return self._qualified_map[qualified]
        node = command
        while getattr(node, "parent", None) is not None:
            node = node.parent
        return self._top_map.get(node.name, catalog.HOME_BUNDLE)

    def automation_allowed(self, guild_id: Optional[int],
                           bundle_key: Optional[str] = None) -> bool:
        """Gate for NON-command automations (on_message XP, join scans, …).

        ``bundle_key=None`` means home-guild-only automation (welcome
        messages, blacklist scans, anything bound to the global channel/role
        config); a bundle key means the automation follows that grant.
        A missing service (pre-setup) must behave like the old single-server
        bot, so callers treat None-service as allow — this method is only
        called when the service exists.
        """
        if guild_id is None:
            return True   # DMs and global contexts were never guild-scoped
        if self.is_home(guild_id):
            return True
        if bundle_key is None:
            return False
        return self.feature_enabled(guild_id, bundle_key)

    def notice_throttled(self, guild_id: int, user_id: int) -> bool:
        """True when a denial notice was sent too recently (anti-spam)."""
        key = (int(guild_id), int(user_id))
        now = time.monotonic()
        last = self._notice_ts.get(key)
        if last is not None and (now - last) < NOTICE_THROTTLE_SECONDS:
            return True
        self._notice_ts[key] = now
        # Opportunistic cleanup so the dict can't grow unbounded.
        if len(self._notice_ts) > 512:
            cutoff = now - NOTICE_THROTTLE_SECONDS
            self._notice_ts = {k: ts for k, ts in self._notice_ts.items() if ts > cutoff}
        return False

    # =================================================================
    # Identity
    # =================================================================

    def identity_for(self, guild_id: Optional[int]) -> FactionIdentity:
        """Resolve the effective identity for a guild (DB override → global
        config → placeholder defaults)."""
        fallback_name = getattr(self._config, "gang_name", None) or "[GANG NAME]"
        fallback_abbr = getattr(self._config, "gang_abbreviation", None) or "GANG"
        if guild_id is None:
            return FactionIdentity(None, fallback_abbr, fallback_name, None, False)
        row = self._db.get_identity(int(guild_id))
        if row is not None and (row.get("gang_tag") or row.get("gang_name") or row.get("display_name")):
            return FactionIdentity(
                guild_id=int(guild_id),
                gang_tag=str(row.get("gang_tag") or fallback_abbr),
                gang_name=str(row.get("gang_name") or fallback_name),
                display_name=clamp_nickname(row.get("display_name")),
                is_override=True,
            )
        return FactionIdentity(int(guild_id), fallback_abbr, fallback_name, None, False)

    def gang_name_for(self, guild_id: Optional[int]) -> str:
        return self.identity_for(guild_id).gang_name

    def gang_tag_for(self, guild_id: Optional[int]) -> str:
        return self.identity_for(guild_id).gang_tag

    def display_name_for(self, guild_id: Optional[int]) -> Optional[str]:
        return self.identity_for(guild_id).display_name

    def resolve_text(self, text: Optional[str], guild_id: Optional[int]) -> Optional[str]:
        """brand_text() with per-guild names: substitute [GANG NAME] /
        [GANG ABBR] / legacy tokens using THIS guild's identity."""
        identity = self.identity_for(guild_id)
        return substitute_text(text, identity.gang_name, identity.gang_tag)

    def set_identity(self, guild_id: int, gang_tag: Optional[str], gang_name: Optional[str],
                     display_name: Optional[str], actor_id: Optional[int]) -> FactionIdentity:
        """Write an identity override row. None values clear their field."""
        self._db.save_identity(int(guild_id), gang_tag, gang_name,
                               clamp_nickname(display_name), actor_id, now_iso())
        self.audit(actor_id, "identity_set", guild_id,
                   f"tag={gang_tag!r} name={gang_name!r} display={display_name!r}")
        return self.identity_for(int(guild_id))

    def reset_identity(self, guild_id: int, actor_id: Optional[int]) -> bool:
        removed = self._db.delete_identity(int(guild_id))
        if removed:
            self.audit(actor_id, "identity_reset", guild_id, "")
        return removed

    async def apply_nickname(self, guild: discord.Guild) -> bool:
        """Set the bot's nickname in a guild to the identity display name.

        Discord nicknames are per-guild by nature — this is what makes one
        bot user appear under the home faction's display name at home and
        under each allied faction's display name (e.g. "ALLY Moderation")
        in its guild. Requires the Change Nickname permission; failure
        is logged, never raised (cosmetic feature).
        """
        if guild is None or guild.me is None:
            return False
        identity = self.identity_for(guild.id)
        desired = identity.display_name
        if not desired:
            return False
        try:
            if not guild.me.guild_permissions.change_nickname:
                logging.warning(f"[faction_access] missing Change Nickname in '{guild.name}' — identity '{desired}' not applied")
                return False
            if guild.me.nick == desired:
                return True
            await guild.me.edit(nick=desired, reason="FactionAccess guild identity")
            logging.info(f"[faction_access] nickname set to '{desired}' in '{guild.name}'")
            return True
        except discord.Forbidden:
            logging.warning(f"[faction_access] forbidden while setting nickname in '{guild.name}'")
        except discord.HTTPException as exc:
            logging.warning(f"[faction_access] nickname edit failed in '{guild.name}': {exc}")
        return False

    # =================================================================
    # Notifications + requests
    # =================================================================

    async def notify_authority(self, embed: discord.Embed) -> int:
        """DM every authority member; returns how many DMs actually landed."""
        sent = 0
        ids = self.authority_ids()
        owner_id = await self.ensure_app_owner()
        if owner_id is not None and owner_id not in ids:
            ids.append(owner_id)
        for user_id in ids:
            try:
                user = self._bot.get_user(user_id)
                if user is None:
                    user = await self._bot.fetch_user(user_id)
                if user is None or user.bot:
                    continue
                await user.send(embed=embed)
                sent += 1
            except (discord.HTTPException, discord.Forbidden, discord.NotFound) as exc:
                logging.warning(f"[faction_access] authority DM to {user_id} failed: {exc}")
        return sent

    async def announce_join_request(self, guild: discord.Guild) -> None:
        identity_default = self.identity_for(guild.id)
        embed = discord.Embed(
            title="🛡️ FactionAccess — Guild Join Request",
            description=(
                f"**{guild.name}** (`{guild.id}`) just added the bot and is "
                f"waiting for a license decision.\n\n"
                f"Members: **{guild.member_count or '?'}** · Owner: "
                f"{guild.owner.mention if guild.owner else 'unknown'}\n\n"
                f"Approve with:\n`!license approve {guild.id} [bundle …]`"
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Default identity in force", value=identity_default.qualified_name(), inline=True)
        embed.set_footer(text="FactionAccess · allied faction licensing")
        await self.notify_authority(embed)

    async def announce_expiry_suspension(self, rows: List[Dict]) -> None:
        names = ", ".join(f"`{row['guild_id']}`" for row in rows)
        embed = discord.Embed(
            title="⏳ FactionAccess — Licenses Expired",
            description=(
                f"{len(rows)} guild(s) were auto-suspended because their "
                f"license expired: {names}\n\n"
                f"Resume with `!license resume <guild>` or extend with "
                f"`!license expiry <guild> <duration>`."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="FactionAccess · allied faction licensing")
        await self.notify_authority(embed)

    async def submit_request(self, member: discord.Member, note: str) -> int:
        """Relay an allied leader's access request to the license authority."""
        status = self.status(member.guild.id)
        features = self.licensed_features_text(member.guild.id)
        embed = discord.Embed(
            title="📨 FactionAccess — Access Request",
            description=(
                f"**{member.guild.name}** (`{member.guild.id}`)\n"
                f"Requested by: {member.mention} (`{member.id}`)\n\n"
                f"Current status: {self.status_label(member.guild.id)}\n"
                f"Granted bundles: {features or 'none'}\n\n"
                f"**Note:** {note[:1000] if note else '(no note)'}"
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Reply via !license grant / !license approve")
        return await self.notify_authority(embed)

    # =================================================================
    # Invite link + helpers
    # =================================================================

    def invite_link(self, guild_id: Optional[int] = None) -> str:
        """OAuth2 authorization URL for adding the bot to another guild.

        Passing a guild_id locks the link to that one guild — the allied
        leader can't redirect it somewhere else. Permissions are the
        explicit INVITE_PERMISSIONS set (no Administrator).
        """
        client_id = self._bot.user.id if self._bot.user is not None else 0
        url = (
            "https://discord.com/oauth2/2/authorize"
            f"?client_id={client_id}&scope=bot&permissions={INVITE_PERMISSIONS.value}"
        )
        if guild_id is not None:
            url += f"&guild_id={int(guild_id)}"
        return url

    def licensed_features_text(self, guild_id: int) -> str:
        features = self._db.get_features(int(guild_id))
        enabled = sorted(k for k, on in features.items() if on)
        return ", ".join(catalog.bundle_label(k) for k in enabled) if enabled else ""

    def audit(self, actor_id: Optional[int], action: str,
              guild_id: Optional[int], detail: Optional[str]) -> None:
        try:
            self._db.add_audit(now_iso(), actor_id, action, guild_id, detail)
        except Exception as exc:
            logging.warning(f"[faction_access] audit write failed ({action}): {exc}")

    def audit_entries(self, guild_id: Optional[int] = None, limit: int = 15) -> List[Dict]:
        return self._db.list_audit(guild_id=guild_id, limit=limit)

    # Status vocabulary exposure for commands/UI.
    ALL_STATUSES = ALL_STATUSES
