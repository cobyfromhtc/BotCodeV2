# -*- coding: utf-8 -*-
"""automod — FactionBot AutoMod rules engine.

A data-driven rules engine (not scattered if-statements):

  * Rules live in the ``automod_rules`` table (11 rule types), are cached
    per guild, sorted by priority, and evaluated against every message.
  * Actions (delete / warn / strike / timeout / kick / ban / notify) can be
    combined comma-separated per rule; the first *punishing* rule per message
    acts, later matches are logged only.
  * Strikes accumulate in ``automod_strikes`` and escalate automatically
    (timeout / kick / ban) once ``escalate_after`` is reached.

All shared infrastructure (DB, embeds, exemptions, hierarchy checks, time
helpers) comes from :mod:`utils.botkit` — this module never imports Bot.py.
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
import re
from collections import deque
from datetime import timedelta
from typing import Any, Deque, Dict, List, Literal, Optional, Sequence, Tuple

import discord
from discord.ext import commands, tasks

from utils import botkit

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RULE_TYPES: Dict[str, str] = {
    "words": "comma-separated words/phrases, * wildcard (e.g. `badword, dumb*`)",
    "regex": "python regex searched case-insensitively (e.g. `dumb\\w*`)",
    "invites": "discord invite links; pattern = allowed invite codes (comma list)",
    "links": "any http(s) link; pattern = allowed domains (comma list)",
    "spam": "N messages within a timespan (pattern like `5/10s`)",
    "duplicates": "same message repeated N times within a timespan (`3/30s`)",
    "mentions": "total user/role/everyone mentions >= threshold",
    "caps": "uppercase percentage >= threshold (pattern = min letters, default 15)",
    "emojis": "custom + unicode emoji count >= threshold",
    "chars": "a single character repeated >= threshold times (e.g. aaaaaaaa)",
    "newlines": "newline count >= threshold (wall-of-text spam)",
}

VALID_ACTIONS: Dict[str, str] = {
    "delete": "delete the offending message",
    "warn": "warn in channel + DM (notice auto-deletes after 15s)",
    "strike": "add an automod strike (escalates after N strikes)",
    "timeout": "timeout the member for duration_seconds",
    "kick": "kick the member",
    "ban": "ban the member (no message purge)",
    "notify": "ping the configured notify role in the log channel",
}

ESCALATE_ACTIONS = ("timeout", "kick", "ban", "none")

#: per-rule, per-user cooldown so one spam wave isn't punished 50 times
RULE_COOLDOWN_SECONDS = 10.0

#: timeout fallback when a rule's duration_seconds was never configured
DEFAULT_TIMEOUT_SECONDS = 600

# --- matchers ---------------------------------------------------------------
INVITE_RE = re.compile(
    r"(discord\.(gg|com/invite)|discordapp\.com/invite)[/\w-]+", re.IGNORECASE
)
LINK_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"  # alchemical symbols
    "\U0001F780-\U0001F7FF"  # geometric shapes extended
    "\U0001F800-\U0001F8FF"  # supplemental arrows-c
    "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols & pictographs extended-a
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U00002B00-\U00002BFF"  # misc symbols & arrows (⭐ …)
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "]"
)
_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z_]+")
_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")

#: rule types whose bare-numeric pattern means "threshold"
_THRESHOLD_TYPES = {"spam", "duplicates", "mentions", "emojis", "chars", "newlines"}

_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS automod_rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        name TEXT,
        rule_type TEXT,
        pattern TEXT,
        threshold INTEGER DEFAULT 1,
        timespan_seconds INTEGER DEFAULT 5,
        action TEXT DEFAULT 'delete',
        action_config TEXT,
        duration_seconds INTEGER DEFAULT 0,
        priority INTEGER DEFAULT 100,
        enabled INTEGER DEFAULT 1,
        exempt_channels TEXT,
        exempt_roles TEXT,
        exempt_users TEXT,
        hits INTEGER DEFAULT 0,
        created_at TEXT,
        created_by INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS automod_violations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        user_id INTEGER,
        rule_id INTEGER,
        rule_name TEXT,
        snippet TEXT,
        action_taken TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS automod_strikes(
        guild_id INTEGER,
        user_id INTEGER,
        strikes INTEGER DEFAULT 0,
        last_violation TEXT,
        PRIMARY KEY(guild_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS automod_config(
        guild_id INTEGER PRIMARY KEY,
        log_channel_id INTEGER DEFAULT 0,
        escalate_after INTEGER DEFAULT 3,
        escalate_action TEXT DEFAULT 'timeout',
        escalate_duration INTEGER DEFAULT 3600,
        notify_role_id INTEGER DEFAULT 0,
        xp_penalty INTEGER DEFAULT 0
    )
    """,
)

_CONFIG_DEFAULTS: Dict[str, Any] = {
    "log_channel_id": 0,
    "escalate_after": 3,
    "escalate_action": "timeout",
    "escalate_duration": 3600,
    "notify_role_id": 0,
    "xp_penalty": 0,
}


# ---------------------------------------------------------------------------
# Pure matching helpers (module-level so they are trivially unit-testable)
# ---------------------------------------------------------------------------
def _split_pattern_words(pattern: str) -> List[str]:
    """Split a words-pattern into entries (comma first, whitespace second).

    ``"badword, dumb phrase, noob*"`` -> ``["badword", "dumb phrase", "noob*"]``
    ``"badword noob*"``               -> ``["badword", "noob*"]``
    """
    if "," in pattern:
        entries = (p.strip().lower() for p in pattern.split(","))
    else:
        entries = (p.strip().lower() for p in pattern.split())
    return [e for e in entries if e]


def _wildcard_regex(entry: str) -> Optional[re.Pattern[str]]:
    """Compile ``foo*bar`` into ``foo.*bar`` (for wildcard phrases)."""
    try:
        return re.compile(".*".join(re.escape(p) for p in entry.split("*")))
    except re.error:
        return None


def match_words(content: str, pattern: str) -> Optional[str]:
    """words rule: token/wildcard match for single words, substring for phrases.

    Returns the matched pattern entry, or None.
    """
    entries = _split_pattern_words(pattern)
    if not entries:
        return None
    lowered = (content or "").lower()
    tokens = [t for t in _TOKEN_SPLIT_RE.split(lowered) if t]
    for entry in entries:
        has_wild = "*" in entry or "?" in entry
        if " " in entry:  # multi-word phrase → substring (wildcard-aware)
            if has_wild:
                rx = _wildcard_regex(entry)
                if rx is not None and rx.search(lowered):
                    return entry
            elif entry in lowered:
                return entry
        else:  # single word → exact token match (fnmatch wildcards allowed)
            if has_wild:
                if any(fnmatch.fnmatchcase(tok, entry) for tok in tokens):
                    return entry
            elif entry in tokens:
                return entry
    return None


def match_regex(content: str, pattern: str) -> Optional[str]:
    """regex rule: ``re.search`` IGNORECASE; invalid patterns return None."""
    if not pattern:
        return None
    try:
        found = re.search(pattern, content or "", re.IGNORECASE)
    except re.error:
        return None
    if not found:
        return None
    return f"regex `{pattern}` matched “{found.group(0)[:60]}”"


def _invite_code(url: str) -> str:
    """Extract the invite code from a matched invite URL."""
    code = url.rsplit("/", 1)[-1]
    return code.lower()


def match_invites(content: str, allowed_codes: Sequence[str]) -> Optional[str]:
    """invites rule; ``allowed_codes`` whitelists specific invite codes."""
    matched = [m.group(0) for m in INVITE_RE.finditer(content or "")]
    if not matched:
        return None
    allowed = {str(c).strip().lower() for c in allowed_codes if str(c).strip()}
    bad = [u for u in matched if _invite_code(u) not in allowed]
    if not bad:
        return None
    return f"invite link `{bad[0][:80]}`"


def _link_domain(url: str) -> str:
    domain = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    domain = domain.split("/", 1)[0].split(":", 1)[0]
    return domain.lower().lstrip(".")


def match_links(content: str, allowed_domains: Sequence[str]) -> Optional[str]:
    """links rule; ``allowed_domains`` whitelists whole domains (+subdomains)."""
    matched = [m.group(0) for m in LINK_RE.finditer(content or "")]
    if not matched:
        return None
    allowed = {str(d).strip().lower().lstrip(".") for d in allowed_domains if str(d).strip()}
    bad = [
        u
        for u in matched
        if not any(_link_domain(u) == d or _link_domain(u).endswith("." + d) for d in allowed)
    ]
    if not bad:
        return None
    return f"link `{bad[0][:100]}`"


def match_mentions(mention_count: int, threshold: int) -> Optional[str]:
    """mentions rule: total user + role + everyone mentions >= threshold."""
    if threshold > 0 and mention_count >= threshold:
        return f"{mention_count} mentions"
    return None


def caps_percent(content: str, min_chars: int) -> Optional[int]:
    """Percentage of uppercase letters, or None when the text is too short."""
    letters = [c for c in (content or "") if c.isalpha()]
    if not letters or len(letters) < max(1, min_chars):
        return None
    upper = sum(1 for c in letters if c.isupper())
    return (upper * 100) // len(letters)


def emoji_count(content: str) -> int:
    """Custom + unicode emoji count."""
    custom = len(CUSTOM_EMOJI_RE.findall(content or ""))
    unicode_ = len(UNICODE_EMOJI_RE.findall(content or ""))
    return custom + unicode_


def match_chars(content: str, threshold: int) -> Optional[str]:
    """chars rule: one character repeated >= threshold times consecutively."""
    n = max(2, threshold)
    m = re.search(rf"(.)\1{{{n - 1},}}", content or "", re.DOTALL)
    if not m:
        return None
    return f"repeated “{m.group(0)[:12]}” ×{len(m.group(0))}"


def match_newlines(content: str, threshold: int) -> Optional[str]:
    """newlines rule: wall-of-text detection."""
    count = (content or "").count("\n")
    if threshold > 0 and count >= threshold:
        return f"{count} newlines"
    return None


def content_hash(content: str) -> str:
    """Stable hash of normalized content for duplicate detection."""
    return hashlib.sha1((content or "").strip().lower().encode("utf-8", "ignore")).hexdigest()[:16]


def _extract_id(text: str) -> Optional[int]:
    """Pull a snowflake out of a raw ID or a <#id> / <@id> / <@&id> mention."""
    text = (text or "").strip()
    for rx in (r"^<#(\d+)>$", r"^<@&?(\d+)>$", r"^(\d+)$"):
        m = re.match(rx, text)
        if m:
            return int(m.group(1))
    return None


class _StubMessage:
    """Minimal message stand-in used by `automod test` (dry-run only)."""

    __slots__ = ("guild", "channel", "author", "content", "mentions", "role_mentions", "mention_everyone")

    def __init__(self, guild: discord.Guild, channel: Any, author: discord.Member, content: str) -> None:
        self.guild = guild
        self.channel = channel
        self.author = author
        self.content = content
        self.mentions = _USER_MENTION_RE.findall(content)  # list of ids — len() works
        self.role_mentions = _ROLE_MENTION_RE.findall(content)
        self.mention_everyone = "@everyone" in content or "@here" in content

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<_StubMessage content={self.content!r}>"


class _ViolationPaginator(discord.ui.View):
    """Button paginator over recent violations (10 per page, 120s timeout)."""

    def __init__(self, cog: "AutoModCog", ctx: commands.Context, entries: List[Dict[str, Any]]) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.entries = entries
        self.page = 0
        self.message: Optional[discord.Message] = None
        self._sync_buttons()

    # -- helpers ------------------------------------------------------------
    @property
    def max_page(self) -> int:
        return max(0, (len(self.entries) - 1) // 10)

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.max_page

    def _build_embed(self) -> discord.Embed:
        start = self.page * 10
        chunk = self.entries[start : start + 10]
        embed = botkit.info(
            "AutoMod Violations",
            f"Page {self.page + 1}/{self.max_page + 1} — {len(self.entries)} total",
        )
        for e in chunk:
            when = botkit.parse_iso(e.get("created_at"))
            ts = botkit.fmt_dt(when)
            embed.add_field(
                name=f"#{e.get('id', '?')} — {e.get('rule_name', 'unknown')}",
                value=(
                    f"<@{e.get('user_id', 0)}> • {ts}\n"
                    f"Action: `{e.get('action_taken', '—')}`\n"
                    f"Snippet: {str(e.get('snippet') or '—')[:180]}"
                ),
                inline=False,
            )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only the invoker may flip pages.
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=botkit.error("Only the command invoker can use these buttons."),
                ephemeral=True,
            )
            return False
        return True

    # -- buttons ------------------------------------------------------------
    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.page = min(self.max_page, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def on_timeout(self) -> None:
        for button in self.children:
            if isinstance(button, discord.ui.Button):
                button.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class AutoModCog(commands.Cog, name="AutoMod"):
    """AutoMod rules engine — configure rules, actions, strikes and escalation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # rule cache: guild_id -> list[dict] sorted by priority
        self._rules_cache: Dict[int, List[Dict[str, Any]]] = {}
        # spam tracker: (guild_id, user_id) -> deque[timestamps]
        self._msg_times: Dict[Tuple[int, int], Deque[float]] = {}
        # duplicate tracker: (guild_id, user_id) -> deque[(timestamp, content_hash)]
        self._content_hashes: Dict[Tuple[int, int], Deque[Tuple[float, str]]] = {}
        # per-rule per-user punishment cooldowns: (gid, uid, rule_id) -> ts
        self._rule_cooldowns: Dict[Tuple[int, int, int], float] = {}
        # rule ids whose regex pattern failed to compile (logged once)
        self._bad_regex_logged: set = set()
        try:
            botkit.create_tables(_TABLE_STATEMENTS)
        except Exception:  # pragma: no cover - DB unavailable at load
            log.exception("automod: failed to create tables")

    # ------------------------------------------------------------------
    # Lifecycle + cache
    # ------------------------------------------------------------------
    async def cog_load(self) -> None:
        try:
            for row in botkit.fetchall("SELECT DISTINCT guild_id FROM automod_rules"):
                await self._load_rules(int(row["guild_id"]))
        except Exception:
            log.exception("automod: initial rule cache load failed")
        if not self._cache_refresh.is_running():
            self._cache_refresh.start()

    async def cog_unload(self) -> None:
        self._cache_refresh.cancel()

    def _invalidate(self, guild_id: Optional[int] = None) -> None:
        """Drop cached rules for one guild (or all guilds) after a mutation."""
        if guild_id is None:
            self._rules_cache.clear()
        else:
            self._rules_cache.pop(guild_id, None)

    async def _load_rules(self, guild_id: int) -> List[Dict[str, Any]]:
        """Load and cache this guild's automod rules.

        `botkit.fetchall` is a synchronous SQLite read that opens a new
        connection. Running it directly inside an `async def` blocked the
        event loop while the periodic cache refresh visited every guild —
        on a 500-guild bot that's 500 blocking reads in one tick. We push
        the read to a worker thread so the loop stays responsive.
        """
        rows = await asyncio.to_thread(
            botkit.fetchall,
            "SELECT * FROM automod_rules WHERE guild_id=? ORDER BY priority ASC, id ASC",
            (guild_id,),
        )
        rules: List[Dict[str, Any]] = []
        for row in rows:
            rule = dict(row)
            rule["exempt_channels"] = botkit.jload(rule.get("exempt_channels"), []) or []
            rule["exempt_roles"] = botkit.jload(rule.get("exempt_roles"), []) or []
            rule["exempt_users"] = botkit.jload(rule.get("exempt_users"), []) or []
            rule["action_list"] = [
                a.strip().lower()
                for a in str(rule.get("action") or "delete").split(",")
                if a.strip()
            ] or ["delete"]
            rules.append(rule)
        self._rules_cache[guild_id] = rules
        return rules

    async def _get_rules(self, guild_id: int) -> List[Dict[str, Any]]:
        cached = self._rules_cache.get(guild_id)
        if cached is None:
            return await self._load_rules(guild_id)
        return cached

    @tasks.loop(minutes=5)
    async def _cache_refresh(self) -> None:
        """Periodically refresh the rule cache + prune in-memory trackers."""
        try:
            guild_ids = {int(r["guild_id"]) for r in botkit.fetchall("SELECT DISTINCT guild_id FROM automod_rules")}
            guild_ids |= set(self._rules_cache.keys())
            for guild_id in guild_ids:
                await self._load_rules(guild_id)
        except Exception:
            log.exception("automod: cache refresh failed")
        self._prune_memory()

    @_cache_refresh.before_loop
    async def _wait_until_ready(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except Exception:  # pragma: no cover - bot never connects during tests
            pass

    def _prune_memory(self) -> None:
        """Keep the in-memory deques/cooldowns bounded."""
        now = botkit.now_ts()
        for key, dq in list(self._msg_times.items()):
            if not dq or dq[-1] < now - 900:
                self._msg_times.pop(key, None)
        for key, dq in list(self._content_hashes.items()):
            if not dq or dq[-1][0] < now - 900:
                self._content_hashes.pop(key, None)
        if len(self._rule_cooldowns) > 4096:
            cutoff = now - 60
            for key, ts in list(self._rule_cooldowns.items()):
                if ts < cutoff:
                    self._rule_cooldowns.pop(key, None)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    async def _get_config(self, guild_id: int) -> Dict[str, Any]:
        cfg = dict(_CONFIG_DEFAULTS)
        try:
            row = botkit.fetchone("SELECT * FROM automod_config WHERE guild_id=?", (guild_id,))
            if row is not None:
                for key in cfg:
                    if key in row.keys() and row[key] is not None:
                        cfg[key] = row[key]
        except Exception:
            log.exception("automod: failed to load config for guild %s", guild_id)
        action = str(cfg.get("escalate_action") or "timeout").lower()
        cfg["escalate_action"] = action if action in ESCALATE_ACTIONS else "timeout"
        return cfg

    def _resolve_log_channel(self, cfg: Dict[str, Any]) -> Optional[discord.abc.Messageable]:
        """automod_config log_channel first, then bot.fb_config channels.log."""
        channel_id = int(cfg.get("log_channel_id") or 0)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            fb_config = getattr(self.bot, "fb_config", None)
            channels = getattr(fb_config, "channels", None) if fb_config is not None else None
            fallback_id = int(getattr(channels, "log", 0) or 0) if channels is not None else 0
            if fallback_id:
                channel = self.bot.get_channel(fallback_id)
        if channel is not None and hasattr(channel, "send"):
            return channel
        return None

    # ------------------------------------------------------------------
    # Rule evaluation (single entry point for all rule types)
    # ------------------------------------------------------------------
    async def _evaluate(
        self, message: Any, rule: Dict[str, Any], *, record: bool = True
    ) -> Optional[str]:
        """Evaluate one rule against one message.

        Returns a human-readable match description, or None. ``record=False``
        is used by the dry-run `test` command so history trackers are not
        mutated.
        """
        content = getattr(message, "content", "") or ""
        rule_type = str(rule.get("rule_type") or "").lower()
        pattern = str(rule.get("pattern") or "")
        threshold = max(1, int(rule.get("threshold") or 1))
        timespan = max(1, int(rule.get("timespan_seconds") or 5))
        guild_id = message.guild.id
        user_id = message.author.id

        if rule_type == "words":
            return match_words(content, pattern)

        if rule_type == "regex":
            if not pattern:
                return None
            if rule["id"] not in self._bad_regex_logged:
                try:
                    re.compile(pattern)
                except re.error:
                    self._bad_regex_logged.add(rule["id"])
                    log.warning(
                        "automod: rule %s (%s) has an invalid regex, skipping",
                        rule.get("name"), guild_id,
                    )
                    return None
            return match_regex(content, pattern)

        if rule_type == "invites":
            allowed = [c for c in re.split(r"[,\s]+", pattern) if c]
            return match_invites(content, allowed)

        if rule_type == "links":
            allowed = [d for d in re.split(r"[,\s]+", pattern) if d]
            return match_links(content, allowed)

        if rule_type == "spam":
            return self._eval_spam(guild_id, user_id, threshold, timespan, record=record)

        if rule_type == "duplicates":
            if not content.strip():
                return None
            return self._eval_duplicates(guild_id, user_id, content, threshold, timespan, record=record)

        if rule_type == "mentions":
            count = (
                len(getattr(message, "mentions", ()) or ())
                + len(getattr(message, "role_mentions", ()) or ())
                + (1 if getattr(message, "mention_everyone", False) else 0)
            )
            return match_mentions(count, threshold)

        if rule_type == "caps":
            min_chars = 15
            if str(pattern).strip().isdigit():
                min_chars = int(str(pattern).strip())
            percent = caps_percent(content, min_chars)
            if percent is not None and percent >= threshold:
                return f"{percent}% uppercase"
            return None

        if rule_type == "emojis":
            count = emoji_count(content)
            if count >= threshold:
                return f"{count} emojis"
            return None

        if rule_type == "chars":
            return match_chars(content, threshold)

        if rule_type == "newlines":
            return match_newlines(content, threshold)

        return None

    def _eval_spam(
        self, guild_id: int, user_id: int, threshold: int, timespan: int, *, record: bool
    ) -> Optional[str]:
        key = (guild_id, user_id)
        dq = self._msg_times.setdefault(key, deque())
        now = botkit.now_ts()
        if record:
            dq.append(now)
        while dq and dq[0] <= now - timespan:
            dq.popleft()
        count = len(dq) + (0 if record else 1)
        if count >= threshold:
            return f"{count} messages in {botkit.fmt_duration(timespan)}"
        return None

    def _eval_duplicates(
        self,
        guild_id: int,
        user_id: int,
        content: str,
        threshold: int,
        timespan: int,
        *,
        record: bool,
    ) -> Optional[str]:
        key = (guild_id, user_id)
        dq = self._content_hashes.setdefault(key, deque())
        now = botkit.now_ts()
        digest = content_hash(content)
        if record:
            dq.append((now, digest))
        while dq and dq[0][0] <= now - timespan:
            dq.popleft()
        occurrences = sum(1 for _, h in dq if h == digest)
        count = occurrences + (0 if record else 1)
        if count >= threshold:
            return f"duplicate message ×{count}"
        return None

    # ------------------------------------------------------------------
    # Engine
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        author = message.author
        if author is None or author.bot:
            return
        if getattr(message, "webhook_id", None):
            return
        if not isinstance(author, discord.Member):
            return
        content = message.content or ""
        prefix = getattr(self.bot, "command_prefix", "!")
        if callable(prefix):
            prefix = "!"
        try:
            if str(prefix) and content.startswith(str(prefix)):
                return
        except Exception:  # pragma: no cover - defensive
            return
        # admins / owners / bots are globally exempt
        if botkit.is_exempt(author):
            return
        try:
            rules = await self._get_rules(message.guild.id)
        except Exception:
            log.exception("automod: failed to load rules for guild %s", message.guild.id)
            return
        if not rules:
            return
        try:
            await self._run_engine(message, rules)
        except Exception:
            log.exception("automod: engine error in guild %s", message.guild.id)

    async def _run_engine(
        self, message: Any, rules: List[Dict[str, Any]], *, dry_run: bool = False
    ) -> List[Tuple[Dict[str, Any], str]]:
        """Evaluate every rule; first punishing rule acts, the rest only log.

        Returns the list of ``(rule, match_description)`` matches (used by the
        dry-run `test` command).
        """
        matched: List[Tuple[Dict[str, Any], str]] = []
        acted = False
        member = message.author
        channel_id = message.channel.id if message.channel is not None else 0
        for rule in rules:
            if not rule.get("enabled"):
                continue
            try:
                if botkit.is_exempt(
                    member,
                    channels=rule["exempt_channels"],
                    channel_id=channel_id,
                    roles=rule["exempt_roles"],
                    users=rule["exempt_users"],
                ):
                    continue
            except Exception:
                log.exception("automod: exemption check failed for rule %s", rule.get("name"))
                continue

            try:
                match_desc = await self._evaluate(message, rule, record=not dry_run)
            except Exception:
                log.exception("automod: evaluation failed for rule %s", rule.get("name"))
                continue
            if not match_desc:
                continue
            matched.append((rule, match_desc))

            if dry_run:
                continue

            cooldown_key = (message.guild.id, member.id, rule["id"])
            now = botkit.now_ts()
            on_cooldown = (now - self._rule_cooldowns.get(cooldown_key, 0.0)) < RULE_COOLDOWN_SECONDS
            if not acted and not on_cooldown:
                acted = True
                self._rule_cooldowns[cooldown_key] = now
                try:
                    await self._apply_action(message, rule, match_desc)
                except Exception:
                    log.exception("automod: failed to apply action for rule %s", rule.get("name"))
            else:
                # one punishment per message — later rules only log
                await self._log_violation(message, rule, match_desc, "logged (no action)")
        return matched

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    async def _apply_action(self, message: Any, rule: Dict[str, Any], match_desc: str) -> None:
        """Run every action of a rule, then ALWAYS log the violation."""
        actions = rule.get("action_list") or ["delete"]
        results: List[str] = []
        for action in actions:
            try:
                ok = await self._run_action(message, rule, action, match_desc)
                results.append(action if ok else f"{action}:failed")
            except discord.Forbidden:
                results.append(f"{action}:forbidden")
            except discord.HTTPException:
                results.append(f"{action}:http-error")
            except Exception:
                log.exception("automod: action %s crashed", action)
                results.append(f"{action}:error")
        action_taken = ", ".join(results) or "none"
        await self._log_violation(message, rule, match_desc, action_taken)
        await self._send_log_embed(message, rule, match_desc, action_taken)
        await self._apply_xp_penalty(message, actions)

    async def _run_action(
        self, message: Any, rule: Dict[str, Any], action: str, match_desc: str
    ) -> bool:
        guild: discord.Guild = message.guild
        member: discord.Member = message.author
        rule_name = rule.get("name") or rule.get("rule_type") or "rule"
        reason = f"AutoMod rule '{rule_name}' matched: {match_desc}"[:512]

        if action == "delete":
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
            return True

        if action == "warn":
            warned = False
            try:
                await message.channel.send(
                    embed=botkit.warning(
                        "AutoMod Warning",
                        f"{member.mention} — {match_desc} (rule **{rule_name}**).",
                    ),
                    delete_after=15,
                )
                warned = True
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            try:
                await member.send(
                    embed=botkit.warning(
                        "AutoMod Warning",
                        f"In **{guild.name}** your message violated rule **{rule_name}**: {match_desc}.",
                    )
                )
                warned = True
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            return warned

        if action == "strike":
            return await self._add_strike(message, rule)

        if action == "timeout":
            duration = int(rule.get("duration_seconds") or 0) or DEFAULT_TIMEOUT_SECONDS
            return await self._timeout_member(member, duration, reason)

        if action == "kick":
            return await self._kick_member(member, reason)

        if action == "ban":
            return await self._ban_member(member, reason)

        if action == "notify":
            cfg = await self._get_config(guild.id)
            channel = self._resolve_log_channel(cfg)
            if channel is None:
                return False
            notify_role_id = int(cfg.get("notify_role_id") or 0)
            content = f"<@&{notify_role_id}> " if notify_role_id else ""
            await channel.send(
                content=content or None,
                embed=botkit.warning(
                    "AutoMod Alert",
                    f"Rule **{rule_name}** triggered by {member.mention} in "
                    f"<#{message.channel.id}>: {match_desc}",
                ),
            )
            return True

        log.warning("automod: unknown action %r on rule %s", action, rule.get("name"))
        return False

    async def _apply_xp_penalty(self, message: Any, actions: Sequence[str]) -> None:
        """Remove XP for delete/strike rules when xp_penalty is configured."""
        try:
            cfg = await self._get_config(message.guild.id)
            penalty = int(cfg.get("xp_penalty") or 0)
            if penalty <= 0:
                return
            if not ({"delete", "strike"} & set(actions)):
                return
            leveling = self.bot.get_cog("Leveling")
            if leveling is None or not hasattr(leveling, "remove_xp"):
                return
            await leveling.remove_xp(message.guild, message.author, penalty)
        except Exception:
            log.exception("automod: xp penalty failed")

    # -- moderation primitives ------------------------------------------
    def _can_moderate(self, member: discord.Member) -> Tuple[bool, str]:
        """Hierarchy check — the BOT is always the actor."""
        return botkit.can_act_on(member.guild.me, member)

    async def _timeout_member(self, member: discord.Member, seconds: int, reason: str) -> bool:
        guild = member.guild
        if not guild.me.guild_permissions.moderate_members:
            log.warning("automod: missing moderate_members in guild %s", guild.id)
            return False
        ok, why = self._can_moderate(member)
        if not ok:
            log.info("automod: timeout skipped for %s — %s", member, why)
            return False
        try:
            await member.timeout(timedelta(seconds=max(1, seconds)), reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("automod: timeout failed for %s — %s", member, exc)
            return False
        return True

    async def _kick_member(self, member: discord.Member, reason: str) -> bool:
        guild = member.guild
        if not guild.me.guild_permissions.kick_members:
            log.warning("automod: missing kick_members in guild %s", guild.id)
            return False
        ok, why = self._can_moderate(member)
        if not ok:
            log.info("automod: kick skipped for %s — %s", member, why)
            return False
        try:
            await member.kick(reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("automod: kick failed for %s — %s", member, exc)
            return False
        return True

    async def _ban_member(self, member: discord.Member, reason: str) -> bool:
        guild = member.guild
        if not guild.me.guild_permissions.ban_members:
            log.warning("automod: missing ban_members in guild %s", guild.id)
            return False
        ok, why = self._can_moderate(member)
        if not ok:
            log.info("automod: ban skipped for %s — %s", member, why)
            return False
        try:
            await member.ban(reason=reason, delete_message_seconds=0)
        except TypeError:  # older discord.py without delete_message_seconds
            try:
                await member.ban(reason=reason)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("automod: ban failed for %s — %s", member, exc)
                return False
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("automod: ban failed for %s — %s", member, exc)
            return False
        return True

    # -- strikes + escalation -------------------------------------------
    async def _add_strike(self, message: Any, rule: Dict[str, Any]) -> bool:
        guild: discord.Guild = message.guild
        member: discord.Member = message.author
        now = botkit.now_iso()
        try:
            botkit.run(
                """
                INSERT INTO automod_strikes(guild_id, user_id, strikes, last_violation)
                VALUES(?, ?, 1, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET strikes = strikes + 1, last_violation = excluded.last_violation
                """,
                (guild.id, member.id, now),
            )
        except Exception:
            log.exception("automod: strike insert failed")
            return False
        row = botkit.fetchone(
            "SELECT strikes FROM automod_strikes WHERE guild_id=? AND user_id=?",
            (guild.id, member.id),
        )
        strikes = int(row["strikes"]) if row is not None else 1
        cfg = await self._get_config(guild.id)
        escalate_after = int(cfg.get("escalate_after") or 0)
        if strikes >= escalate_after > 0:
            rule_name = rule.get("name") or "rule"
            escalated = await self._escalate(
                member,
                cfg,
                f"AutoMod escalation — {strikes} strikes (rule '{rule_name}')",
                channel=message.channel,
            )
            try:
                botkit.run(
                    "UPDATE automod_strikes SET strikes=0 WHERE guild_id=? AND user_id=?",
                    (guild.id, member.id),
                )
            except Exception:
                log.exception("automod: strike reset failed")
            return escalated
        return True

    async def _escalate(
        self,
        member: discord.Member,
        cfg: Dict[str, Any],
        reason: str,
        *,
        channel: Any = None,
    ) -> bool:
        act = str(cfg.get("escalate_action") or "none").lower()
        if act not in ("timeout", "kick", "ban"):
            return False
        duration = int(cfg.get("escalate_duration") or 0) or 3600
        if act == "timeout":
            ok = await self._timeout_member(member, duration, reason)
        elif act == "kick":
            ok = await self._kick_member(member, reason)
        else:
            ok = await self._ban_member(member, reason)
        if not ok:
            return False
        try:
            await self._escalation_log(member, act, duration, reason, channel)
        except Exception:
            log.exception("automod: escalation log failed")
        return True

    async def _escalation_log(
        self,
        member: discord.Member,
        act: str,
        duration: int,
        reason: str,
        channel: Any = None,
    ) -> None:
        cfg = await self._get_config(member.guild.id)
        log_channel = self._resolve_log_channel(cfg)
        if log_channel is None:
            return
        detail = f"{member.mention} ({member}) was **{act}**"
        if act == "timeout":
            detail += f" for {botkit.fmt_duration(duration)}"
        embed = botkit.warning("AutoMod Escalation", f"{detail}.\n{reason[:300]}")
        await log_channel.send(embed=embed)

    # -- violation logging ------------------------------------------------
    async def _log_violation(
        self, message: Any, rule: Dict[str, Any], match_desc: str, action_taken: str
    ) -> None:
        try:
            botkit.run(
                """
                INSERT INTO automod_violations
                    (guild_id, user_id, rule_id, rule_name, snippet, action_taken, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.guild.id,
                    message.author.id,
                    rule.get("id"),
                    rule.get("name") or rule.get("rule_type") or "unknown",
                    (getattr(message, "content", "") or "")[:200],
                    action_taken[:200],
                    botkit.now_iso(),
                ),
            )
        except Exception:
            log.exception("automod: violation insert failed")
            return
        try:
            botkit.run("UPDATE automod_rules SET hits = hits + 1 WHERE id=?", (rule.get("id"),))
        except Exception:
            log.exception("automod: hits increment failed for rule %s", rule.get("id"))

    async def _send_log_embed(
        self, message: Any, rule: Dict[str, Any], match_desc: str, action_taken: str
    ) -> None:
        cfg = await self._get_config(message.guild.id)
        channel = self._resolve_log_channel(cfg)
        if channel is None:
            return
        embed = botkit.warning(
            f"AutoMod: {rule.get('name') or rule.get('rule_type')}",
            (
                f"**User:** {message.author.mention} ({message.author.id})\n"
                f"**Channel:** <#{message.channel.id}>\n"
                f"**Match:** {match_desc}\n"
                f"**Actions:** `{action_taken}`"
            ),
        )
        snippet = (getattr(message, "content", "") or "")[:300]
        if snippet:
            embed.add_field(name="Message", value=snippet, inline=False)
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            log.warning("automod: failed to send log embed — %s", exc)

    # ------------------------------------------------------------------
    # on_member_join — strike persistence across rejoins
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            row = botkit.fetchone(
                "SELECT strikes FROM automod_strikes WHERE guild_id=? AND user_id=?",
                (member.guild.id, member.id),
            )
            if row is None:
                return
            strikes = int(row["strikes"] or 0)
            cfg = await self._get_config(member.guild.id)
            if strikes >= int(cfg.get("escalate_after") or 0) > 0:
                await self._escalate(
                    member,
                    cfg,
                    f"AutoMod escalation — rejoined with {strikes} active strikes",
                )
        except Exception:
            log.exception("automod: on_member_join check failed")

    # ==================================================================
    # COMMANDS
    # ==================================================================
    @commands.group(name="automod", aliases=["am"], invoke_without_command=True)
    @commands.guild_only()
    async def automod(self, ctx: commands.Context) -> None:
        """AutoMod rules engine — see `automod ruleset` for help."""
        embed = botkit.info(
            "AutoMod Rules Engine",
            "Data-driven moderation rules with actions, strikes and escalation.",
        )
        embed.add_field(
            name="Getting started",
            value=(
                "`automod ruleset` — rule types, actions & examples\n"
                "`automod add <type> <action> [pattern]`\n"
                "`automod list` / `automod remove <id>`\n"
                "`automod config` — log channel, escalation, XP penalty\n"
                "`automod test <text>` — dry-run any message"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # automod add
    # ------------------------------------------------------------------
    @automod.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def add(
        self, ctx: commands.Context, rule_type: str, action: str, *, pattern: str = ""
    ) -> None:
        """Create a rule. e.g. `automod add words delete badword, noob*` or `automod add spam timeout 5/10s`"""
        rule_type = (rule_type or "").strip().lower()
        if rule_type not in RULE_TYPES:
            valid = ", ".join(sorted(RULE_TYPES))
            await ctx.send(
                embed=botkit.error(
                    "Unknown rule type",
                    f"`{rule_type}` is not a rule type.\nValid types: {valid}",
                )
            )
            return
        actions = [a.strip().lower() for a in (action or "").split(",") if a.strip()]
        if not actions:
            await ctx.send(embed=botkit.error("No action given", "Provide at least one action."))
            return
        invalid = [a for a in actions if a not in VALID_ACTIONS]
        if invalid:
            valid = ", ".join(sorted(VALID_ACTIONS))
            await ctx.send(
                embed=botkit.error(
                    "Unknown action",
                    f"Invalid action(s): {', '.join(f'`{a}`' for a in invalid)}.\n"
                    f"Valid actions: {valid}",
                )
            )
            return

        parsed = self._parse_rule_pattern(rule_type, pattern)
        if parsed is None:
            await ctx.send(
                embed=botkit.error(
                    "Invalid pattern",
                    "Could not parse that pattern. Examples:\n"
                    "`automod add spam delete 5/10s`\n"
                    "`automod add mentions warn 5`\n"
                    "`automod add words delete,badword* priority:10 duration:1h`",
                )
            )
            return

        threshold = parsed["threshold"]
        timespan = parsed["timespan_seconds"]
        duration = parsed["duration_seconds"]
        priority = parsed["priority"]
        clean_pattern = parsed["pattern"]

        # threshold sanity per rule type
        if rule_type in _THRESHOLD_TYPES and threshold < 1:
            await ctx.send(embed=botkit.error("Invalid threshold", "Threshold must be at least 1."))
            return

        name = self._next_rule_name(ctx.guild.id, rule_type)
        try:
            botkit.run(
                """
                INSERT INTO automod_rules
                    (guild_id, name, rule_type, pattern, threshold, timespan_seconds,
                     action, action_config, duration_seconds, priority, enabled,
                     exempt_channels, exempt_roles, exempt_users, hits, created_at, created_by)
                VALUES(?,?,?,?,?,?,?,?,?,?,1,'[]','[]','[]',0,?,?)
                """,
                (
                    ctx.guild.id,
                    name,
                    rule_type,
                    clean_pattern,
                    threshold,
                    timespan,
                    ",".join(actions),
                    None,
                    duration,
                    priority,
                    botkit.now_iso(),
                    ctx.author.id,
                ),
            )
        except Exception:
            log.exception("automod: rule insert failed")
            await ctx.send(embed=botkit.error("Database error", "Could not save the rule."))
            return
        self._invalidate(ctx.guild.id)
        await self._load_rules(ctx.guild.id)

        row = botkit.fetchone(
            "SELECT id FROM automod_rules WHERE guild_id=? AND name=?", (ctx.guild.id, name)
        )
        rule_id = row["id"] if row is not None else 0
        embed = botkit.success("AutoMod rule created", f"**{name}** (rule #{rule_id}) is now active.")
        embed.add_field(
            name="Configuration",
            value=(
                f"Type: `{rule_type}`\n"
                f"Actions: `{','.join(actions)}`\n"
                f"Pattern: {clean_pattern or '—'}\n"
                f"Threshold: `{threshold}`\n"
                f"Timespan: `{botkit.fmt_duration(timespan)}`\n"
                f"Duration: `{botkit.fmt_duration(duration) if duration else '—'}`\n"
                f"Priority: `{priority}` (lower runs first)"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    def _parse_rule_pattern(self, rule_type: str, raw: str) -> Optional[Dict[str, Any]]:
        """Parse the free-form pattern + `key:value` options of `automod add`."""
        parsed: Dict[str, Any] = {
            "pattern": "",
            "threshold": 1,
            "timespan_seconds": 5,
            "duration_seconds": 0,
            "priority": 100,
        }
        text = (raw or "").strip()

        # pull out option tokens (priority:10 duration:2h threshold:5 timespan:30s)
        for key in ("priority", "duration", "threshold", "timespan"):
            m = re.search(rf"(?:^|\s){key}:(\S+)", text)
            if not m:
                continue
            value = m.group(1)
            text = (text[: m.start()] + " " + text[m.end() :]).strip() if m.start() > 0 else text[m.end() :].strip()
            if key == "priority":
                if not value.isdigit():
                    return None
                parsed["priority"] = int(value)
            elif key == "duration":
                seconds = botkit.parse_duration(value)
                if seconds is None:
                    return None
                parsed["duration_seconds"] = seconds
            elif key == "threshold":
                if not value.isdigit():
                    return None
                parsed["threshold"] = int(value)
            else:  # timespan
                seconds = botkit.parse_duration(value)
                if seconds is None:
                    return None
                parsed["timespan_seconds"] = seconds

        # spam/duplicates: leading "N/Ts" like 5/10s, 5/10 or "5 10"
        if rule_type in ("spam", "duplicates") and text:
            m = re.match(r"^(\d+)\s*[/ ]\s*(\d+\w*)\s*$", text)
            if m:
                parsed["threshold"] = int(m.group(1))
                seconds = botkit.parse_duration(m.group(2))
                if seconds is None:
                    return None
                parsed["timespan_seconds"] = seconds
                text = ""
            elif text.isdigit():
                parsed["threshold"] = int(text)
                text = ""
        elif rule_type == "caps":
            # caps pattern holds min_chars — leave bare numbers as pattern
            parsed["pattern"] = text
            return parsed
        elif rule_type in _THRESHOLD_TYPES and text.isdigit():
            # `automod add mentions warn 5` → threshold 5
            parsed["threshold"] = int(text)
            text = ""

        parsed["pattern"] = text.strip()
        if rule_type == "regex" and parsed["pattern"]:
            try:
                re.compile(parsed["pattern"])
            except re.error:
                return None
        if parsed["threshold"] < 1 or parsed["timespan_seconds"] < 1:
            return None
        return parsed

    def _next_rule_name(self, guild_id: int, rule_type: str) -> str:
        try:
            rows = botkit.fetchall(
                "SELECT name FROM automod_rules WHERE guild_id=? AND rule_type=?",
                (guild_id, rule_type),
            )
            existing = {r["name"] for r in rows}
        except Exception:
            existing = set()
        n = len(existing) + 1
        name = f"{rule_type}-{n}"
        while name in existing:
            n += 1
            name = f"{rule_type}-{n}"
        return name

    # ------------------------------------------------------------------
    # automod list
    # ------------------------------------------------------------------
    @automod.command(name="list")
    async def list_rules(self, ctx: commands.Context, scope: Optional[str] = None) -> None:
        """List automod rules (enabled by default; `all` to include disabled)."""
        scope = (scope or "enabled").strip().lower()
        if scope not in ("enabled", "all"):
            await ctx.send(
                embed=botkit.error("Unknown scope", "Use `enabled` (default) or `all`.")
            )
            return
        rules = await self._get_rules(ctx.guild.id)
        shown = [r for r in rules if scope == "all" or r.get("enabled")]
        if not shown:
            await ctx.send(
                embed=botkit.info(
                    "No AutoMod rules",
                    "Create one with `automod add <type> <action> [pattern]`.",
                )
            )
            return
        embed = botkit.info(
            "AutoMod Rules",
            f"{len(shown)} rule(s) — sorted by priority (lower runs first).",
        )
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for rule in shown:
            grouped.setdefault(rule.get("rule_type") or "?", []).append(rule)
        for rule_type, group in sorted(grouped.items()):
            lines = []
            for r in group:
                status = "✅" if r.get("enabled") else "⛔"
                pattern = str(r.get("pattern") or "—")
                if len(pattern) > 40:
                    pattern = pattern[:37] + "…"
                lines.append(
                    f"`#{r['id']}` {status} **{r.get('name')}** — `{','.join(r.get('action_list') or [])}`"
                    f" | pat: {pattern} | thr: {r.get('threshold')}"
                    f" | hits: {r.get('hits')}"
                )
            value = "\n".join(lines) or "—"
            if len(value) > 1000:
                value = value[:997] + "…"
            embed.add_field(name=f"{rule_type} ({len(group)})", value=value, inline=False)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # automod remove / enable / disable
    # ------------------------------------------------------------------
    def _get_rule(self, guild_id: int, rule_id: int) -> Optional[Dict[str, Any]]:
        row = botkit.fetchone(
            "SELECT * FROM automod_rules WHERE guild_id=? AND id=?", (guild_id, rule_id)
        )
        return dict(row) if row is not None else None

    @automod.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def remove(self, ctx: commands.Context, rule_id: int) -> None:
        """Delete an automod rule by ID."""
        rule = self._get_rule(ctx.guild.id, rule_id)
        if rule is None:
            await ctx.send(embed=botkit.error("Rule not found", f"No rule #{rule_id} here."))
            return
        try:
            botkit.run("DELETE FROM automod_rules WHERE guild_id=? AND id=?", (ctx.guild.id, rule_id))
        except Exception:
            log.exception("automod: rule delete failed")
            await ctx.send(embed=botkit.error("Database error", "Could not delete the rule."))
            return
        self._invalidate(ctx.guild.id)
        await ctx.send(
            embed=botkit.success("Rule removed", f"**{rule.get('name')}** (#{rule_id}) deleted.")
        )

    @automod.command(name="enable")
    @commands.has_permissions(manage_guild=True)
    async def enable(self, ctx: commands.Context, rule_id: int) -> None:
        """Enable a disabled automod rule."""
        rule = self._get_rule(ctx.guild.id, rule_id)
        if rule is None:
            await ctx.send(embed=botkit.error("Rule not found", f"No rule #{rule_id} here."))
            return
        try:
            botkit.run(
                "UPDATE automod_rules SET enabled=1 WHERE guild_id=? AND id=?",
                (ctx.guild.id, rule_id),
            )
        except Exception:
            log.exception("automod: rule enable failed")
            await ctx.send(embed=botkit.error("Database error", "Could not enable the rule."))
            return
        self._invalidate(ctx.guild.id)
        await ctx.send(
            embed=botkit.success("Rule enabled", f"**{rule.get('name')}** (#{rule_id}) is active.")
        )

    @automod.command(name="disable")
    @commands.has_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context, rule_id: int) -> None:
        """Disable an automod rule (kept for later re-enable)."""
        rule = self._get_rule(ctx.guild.id, rule_id)
        if rule is None:
            await ctx.send(embed=botkit.error("Rule not found", f"No rule #{rule_id} here."))
            return
        try:
            botkit.run(
                "UPDATE automod_rules SET enabled=0 WHERE guild_id=? AND id=?",
                (ctx.guild.id, rule_id),
            )
        except Exception:
            log.exception("automod: rule disable failed")
            await ctx.send(embed=botkit.error("Database error", "Could not disable the rule."))
            return
        self._invalidate(ctx.guild.id)
        await ctx.send(
            embed=botkit.warning("Rule disabled", f"**{rule.get('name')}** (#{rule_id}) is paused.")
        )

    # ------------------------------------------------------------------
    # automod exempt / unexempt
    # ------------------------------------------------------------------
    async def _modify_exemption(
        self,
        ctx: commands.Context,
        rule_id: int,
        kind: str,
        target: str,
        *,
        add: bool,
    ) -> None:
        rule = self._get_rule(ctx.guild.id, rule_id)
        if rule is None:
            await ctx.send(embed=botkit.error("Rule not found", f"No rule #{rule_id} here."))
            return
        target_id = _extract_id(target)
        if target_id is None:
            await ctx.send(
                embed=botkit.error("Invalid target", "Give a mention or an ID for the channel/role/user.")
            )
            return

        column = {"channel": "exempt_channels", "role": "exempt_roles", "user": "exempt_users"}[kind]
        label = kind
        if kind == "channel":
            obj = ctx.guild.get_channel(target_id)
            label = f"#{obj.name}" if obj is not None else f"channel `{target_id}`"
        elif kind == "role":
            obj = ctx.guild.get_role(target_id)
            label = f"@{obj.name}" if obj is not None else f"role `{target_id}`"
        else:
            obj = ctx.guild.get_member(target_id)
            label = f"@{obj.name}" if obj is not None else f"user `{target_id}`"

        current = botkit.jload(rule.get(column), []) or []
        if add:
            if target_id in current:
                await ctx.send(
                    embed=botkit.info("Already exempt", f"{label} is already exempt on **{rule.get('name')}**.")
                )
                return
            current.append(target_id)
        else:
            if target_id not in current:
                await ctx.send(
                    embed=botkit.info("Not exempt", f"{label} is not exempt on **{rule.get('name')}**.")
                )
                return
            current = [tid for tid in current if tid != target_id]
        try:
            botkit.run(
                f"UPDATE automod_rules SET {column}=? WHERE guild_id=? AND id=?",
                (botkit.jdump(current), ctx.guild.id, rule_id),
            )
        except Exception:
            log.exception("automod: exemption update failed")
            await ctx.send(embed=botkit.error("Database error", "Could not update exemptions."))
            return
        self._invalidate(ctx.guild.id)
        verb = "added to" if add else "removed from"
        await ctx.send(
            embed=botkit.success(
                "Exemption updated",
                f"{label} {verb} **{rule.get('name')}** (#{rule_id}).",
            )
        )

    @automod.command(name="exempt")
    @commands.has_permissions(manage_guild=True)
    async def exempt(
        self,
        ctx: commands.Context,
        rule_id: int,
        kind: Literal["channel", "role", "user"],
        target: str,
    ) -> None:
        """Exempt a channel, role or user from a rule. e.g. `automod exempt 1 role @Staff`"""
        await self._modify_exemption(ctx, rule_id, kind, target, add=True)

    @automod.command(name="unexempt")
    @commands.has_permissions(manage_guild=True)
    async def unexempt(
        self,
        ctx: commands.Context,
        rule_id: int,
        kind: Literal["channel", "role", "user"],
        target: str,
    ) -> None:
        """Remove a channel/role/user exemption from a rule."""
        await self._modify_exemption(ctx, rule_id, kind, target, add=False)

    # ------------------------------------------------------------------
    # automod config
    # ------------------------------------------------------------------
    @automod.command(name="config")
    @commands.has_permissions(manage_guild=True)
    async def config(
        self, ctx: commands.Context, key: Optional[str] = None, value: Optional[str] = None
    ) -> None:
        """View or set automod settings (log channel, escalation, notify role, XP penalty)."""
        cfg = await self._get_config(ctx.guild.id)
        if key is None:
            await ctx.send(embed=self._config_embed(cfg, ctx.guild))
            return
        key = key.strip().lower()
        if key not in ("log_channel", "escalate_after", "escalate_action", "escalate_duration", "notify_role", "xp_penalty"):
            await ctx.send(
                embed=botkit.error(
                    "Unknown config key",
                    "Keys: `log_channel`, `escalate_after`, `escalate_action`, "
                    "`escalate_duration`, `notify_role`, `xp_penalty`.",
                )
            )
            return
        if value is None:
            await ctx.send(embed=botkit.info(f"{key} = `{cfg.get(self._config_column(key))}`"))
            return

        update_value: Any
        if key == "log_channel":
            channel_id = _extract_id(value)
            if channel_id is None or ctx.guild.get_channel(channel_id) is None:
                await ctx.send(embed=botkit.error("Invalid channel", "Mention a channel or give its ID."))
                return
            update_value = channel_id
        elif key == "notify_role":
            role_id = _extract_id(value)
            if role_id is None or ctx.guild.get_role(role_id) is None:
                await ctx.send(embed=botkit.error("Invalid role", "Mention a role or give its ID."))
                return
            update_value = role_id
        elif key == "escalate_after":
            if not str(value).strip().isdigit() or int(value) < 1:
                await ctx.send(embed=botkit.error("Invalid number", "escalate_after must be ≥ 1."))
                return
            update_value = int(value)
        elif key == "escalate_action":
            action = value.strip().lower()
            if action not in ESCALATE_ACTIONS:
                await ctx.send(
                    embed=botkit.error(
                        "Invalid action",
                        f"escalate_action must be one of: {', '.join(f'`{a}`' for a in ESCALATE_ACTIONS)}.",
                    )
                )
                return
            update_value = action
        elif key == "escalate_duration":
            seconds = botkit.parse_duration(value)
            if seconds is None:
                await ctx.send(
                    embed=botkit.error("Invalid duration", "Use a duration like `2h`, `30m` or `3600`.")
                )
                return
            update_value = seconds
        else:  # xp_penalty
            if not str(value).strip().lstrip("-").isdigit() or int(value) < 0:
                await ctx.send(embed=botkit.error("Invalid number", "xp_penalty must be ≥ 0."))
                return
            update_value = int(value)

        column = self._config_column(key)
        try:
            botkit.run(
                "INSERT OR IGNORE INTO automod_config (guild_id) VALUES (?)", (ctx.guild.id,)
            )
            botkit.run(
                f"UPDATE automod_config SET {column}=? WHERE guild_id=?",
                (update_value, ctx.guild.id),
            )
        except Exception:
            log.exception("automod: config update failed")
            await ctx.send(embed=botkit.error("Database error", "Could not save the setting."))
            return
        cfg = await self._get_config(ctx.guild.id)
        await ctx.send(
            embed=botkit.success(
                "Config updated",
                f"`{key}` is now `{update_value}`.",
            )
        )

    @staticmethod
    def _config_column(key: str) -> str:
        return {
            "log_channel": "log_channel_id",
            "escalate_after": "escalate_after",
            "escalate_action": "escalate_action",
            "escalate_duration": "escalate_duration",
            "notify_role": "notify_role_id",
            "xp_penalty": "xp_penalty",
        }[key]

    def _config_embed(self, cfg: Dict[str, Any], guild: discord.Guild) -> discord.Embed:
        log_channel = (
            f"<#{cfg['log_channel_id']}>"
            if cfg.get("log_channel_id")
            else "not set (falls back to bot log channel)"
        )
        notify_role = (
            f"<@&{cfg['notify_role_id']}>" if cfg.get("notify_role_id") else "not set"
        )
        embed = botkit.info("AutoMod Config")
        embed.add_field(
            name="Settings",
            value=(
                f"Log channel: {log_channel}\n"
                f"Escalate after: `{cfg['escalate_after']}` strikes\n"
                f"Escalate action: `{cfg['escalate_action']}`\n"
                f"Escalate duration: `{botkit.fmt_duration(int(cfg['escalate_duration'] or 0))}`\n"
                f"Notify role: {notify_role}\n"
                f"XP penalty: `{cfg['xp_penalty']}` per punished message"
            ),
            inline=False,
        )
        embed.set_footer(text=f"{guild.name} — use `automod config <key> <value>` to change")
        return embed

    # ------------------------------------------------------------------
    # automod strikes / strikes clear
    # ------------------------------------------------------------------
    @automod.group(name="strikes", invoke_without_command=True)
    async def strikes(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """Show a member's automod strikes and recent violations."""
        member = member or ctx.author
        row = botkit.fetchone(
            "SELECT strikes, last_violation FROM automod_strikes WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, member.id),
        )
        strikes = int(row["strikes"]) if row is not None else 0
        last = botkit.parse_iso(row["last_violation"]) if row is not None else None
        cfg = await self._get_config(ctx.guild.id)
        embed = botkit.info(f"AutoMod strikes — {member.display_name}")
        embed.add_field(
            name="Strikes",
            value=(
                f"**{strikes}** / {cfg['escalate_after']} before escalation "
                f"(`{cfg['escalate_action']}`)\n"
                f"Last violation: {botkit.fmt_dt(last)}"
            ),
            inline=False,
        )
        try:
            rows = botkit.fetchall(
                """
                SELECT * FROM automod_violations
                WHERE guild_id=? AND user_id=?
                ORDER BY id DESC LIMIT 5
                """,
                (ctx.guild.id, member.id),
            )
        except Exception:
            rows = []
        if rows:
            lines = []
            for r in rows:
                when = botkit.fmt_dt(botkit.parse_iso(r["created_at"]))
                lines.append(f"{when} — **{r['rule_name']}**: `{r['action_taken']}`")
            embed.add_field(name="Recent violations", value="\n".join(lines)[:1000], inline=False)
        else:
            embed.add_field(name="Recent violations", value="None recorded.", inline=False)
        await ctx.send(embed=embed)

    @strikes.command(name="clear")
    @commands.has_permissions(manage_guild=True)
    async def strikes_clear(self, ctx: commands.Context, member: discord.Member) -> None:
        """Reset a member's automod strikes."""
        try:
            botkit.run(
                "UPDATE automod_strikes SET strikes=0, last_violation=NULL WHERE guild_id=? AND user_id=?",
                (ctx.guild.id, member.id),
            )
        except Exception:
            log.exception("automod: strike clear failed")
            await ctx.send(embed=botkit.error("Database error", "Could not clear strikes."))
            return
        await ctx.send(
            embed=botkit.success("Strikes cleared", f"{member.mention}'s strikes reset to 0.")
        )

    # ------------------------------------------------------------------
    # automod test (dry run)
    # ------------------------------------------------------------------
    @automod.command(name="test")
    @commands.has_permissions(manage_guild=True)
    async def test(self, ctx: commands.Context, *, text: str) -> None:
        """Dry-run the engine on text — no actions, shows which rules WOULD trigger."""
        rules = await self._get_rules(ctx.guild.id)
        if not rules:
            await ctx.send(embed=botkit.info("No rules", "No automod rules exist on this server yet."))
            return
        stub = _StubMessage(ctx.guild, ctx.channel, ctx.author, text)
        matched = await self._run_engine(stub, rules, dry_run=True)
        if not matched:
            await ctx.send(
                embed=botkit.success(
                    "Dry run — clean",
                    f"No rules would trigger for:\n>>> {discord.utils.escape_markdown(text[:400])}",
                )
            )
            return
        embed = botkit.warning(
            "Dry run — would trigger",
            f"{len(matched)} rule(s) match (first one would punish):\n"
            f">>> {discord.utils.escape_markdown(text[:400])}",
        )
        for rule, desc in matched:
            actions = ",".join(rule.get("action_list") or [])
            embed.add_field(
                name=f"#{rule['id']} {rule.get('name')} ({rule.get('rule_type')})",
                value=f"{desc}\nActions: `{actions}` | priority `{rule.get('priority')}`",
                inline=False,
            )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # automod logs
    # ------------------------------------------------------------------
    @automod.command(name="logs")
    async def logs(self, ctx: commands.Context, page: int = 1) -> None:
        """Recent violations, paginated (10 per page, buttons for 120s)."""
        try:
            rows = botkit.fetchall(
                "SELECT * FROM automod_violations WHERE guild_id=? ORDER BY id DESC LIMIT 100",
                (ctx.guild.id,),
            )
        except Exception:
            log.exception("automod: violation fetch failed")
            await ctx.send(embed=botkit.error("Database error", "Could not load violations."))
            return
        entries = [dict(r) for r in rows]
        if not entries:
            await ctx.send(embed=botkit.info("No violations", "Nothing logged yet."))
            return
        view = _ViolationPaginator(self, ctx, entries)
        page = max(1, min(page, view.max_page + 1))
        view.page = page - 1
        view._sync_buttons()
        view.message = await ctx.send(embed=view._build_embed(), view=view)

    # ------------------------------------------------------------------
    # automod ruleset (help)
    # ------------------------------------------------------------------
    @automod.command(name="ruleset")
    async def ruleset(self, ctx: commands.Context) -> None:
        """Show available rule types, actions and examples."""
        embed = botkit.info("AutoMod Rules Engine — Reference")
        types_text = "\n".join(f"`{rt}` — {desc}" for rt, desc in RULE_TYPES.items())
        embed.add_field(name="Rule types", value=types_text[:1000], inline=False)
        actions_text = "\n".join(f"`{act}` — {desc}" for act, desc in VALID_ACTIONS.items())
        embed.add_field(name="Actions (comma-combine)", value=actions_text[:1000], inline=False)
        embed.add_field(
            name="Examples",
            value=(
                "`automod add words delete badword, noob*`\n"
                "`automod add words delete,warn,strike badword* priority:10`\n"
                "`automod add spam timeout 5/10s duration:1h`\n"
                "`automod add duplicates delete 3/30s`\n"
                "`automod add invites delete invitecode123`\n"
                "`automod add links warn youtube.com,twitch.tv`\n"
                "`automod add mentions warn 5`\n"
                "`automod add caps warn 70` (70% caps, 15+ letters)\n"
                "`automod add emojis delete 10`\n"
                "`automod add chars delete 8`\n"
                "`automod add newlines delete 10`\n"
                "`automod add regex delete du[pb]e\\w*`\n"
                "`automod exempt 1 role @Staff` / `automod config escalate_after 5`"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------
    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandInvokeError):
            error = error.original  # type: ignore[assignment]
        if isinstance(error, commands.MissingPermissions):
            perms = ", ".join(error.missing_permissions) if error.missing_permissions else "permissions"
            await ctx.send(
                embed=botkit.error("Missing permissions", f"You need **{perms}** for this command.")
            )
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(embed=botkit.error("Server only", "AutoMod commands only work in a server."))
        elif isinstance(error, (commands.BadArgument, commands.BadLiteralArgument)):
            await ctx.send(
                embed=botkit.error(
                    "Bad argument",
                    "Check the command signature — try `automod ruleset` for examples.",
                )
            )
        elif isinstance(error, commands.CheckFailure):
            await ctx.send(embed=botkit.error("Not allowed", "You can't use this command here."))
        else:
            log.exception("automod: unhandled command error", exc_info=error)
            try:
                await ctx.send(embed=botkit.error("Something went wrong", "The error was logged."))
            except Exception:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoModCog(bot))
