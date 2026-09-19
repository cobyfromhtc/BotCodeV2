# -*- coding: utf-8 -*-
'''
FactionAccess.catalog — the feature-bundle catalog + command classification.

Everything an allied faction can be granted lives in exactly one bundle. The
catalog is deliberately a UNION of the Full and Short command surfaces: a name
that does not exist in the running edition is simply never looked up, so the
same file serves both editions byte-for-byte.

Classification is default-deny: any command that is not listed in a bundle and
not in ALWAYS_AVAILABLE_COMMANDS resolves to the home-only bucket. A brand-new
command added tomorrow is therefore invisible to allied guilds until someone
classifies it — ``!license catalog`` reports those drift candidates so they
can be triaged instead of silently leaking.

Resolution order for an invocation:
    1. ALWAYS_AVAILABLE_COMMANDS  (top-level name)  -> always usable
    2. QUALIFIED_OVERRIDES        (qualified name)  -> e.g. "botbranding name"
    3. FEATURE_BUNDLES            (top-level name)  -> bundle must be granted
    4. anything else                                -> home-only

Group subcommands inherit their top-level command's bundle ("verification
setup" -> verification, "invites leaderboard" -> engagement) unless a
QUALIFIED_OVERRIDES entry says otherwise.
'''

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

# Sentinel bundle key for "home guild only, never grantable".
HOME_BUNDLE: str = "__home__"

# Commands usable in EVERY guild the bot is in — including pending (not yet
# approved) allied guilds. These are the surfaces an allied leader needs to
# inspect their status and reach the license authority.
ALWAYS_AVAILABLE_COMMANDS: frozenset = frozenset({
    "help",       # built-in help (Full) / FactionHelp (Short, aliases cmds/commands/h)
    "cmds",       # paginated command list (Full) / help alias (Short)
    "ping",       # latency probe (Full info module)
    "license",    # this package's management group (authority-gated internally)
    "request",    # this package's allied-leader hotline to the authority
})

# ---------------------------------------------------------------------------
# Feature bundles
# ---------------------------------------------------------------------------
# Keys are stable identifiers (stored in the faction_features table); labels
# and descriptions are what the owner sees in !license output.

_TICKET_LIFECYCLE: Tuple[str, ...] = (
    # core ticket lifecycle
    "add", "remove", "claim", "unclaim", "close", "closerequest", "reopen",
    "transcript", "rename", "move", "note", "notes", "priority",
    # panels
    "panel", "panels", "deletepanel", "panelupdate", "panelquestion",
    "multipanel", "dropdownpanel", "reactionpanel", "limitbypass",
    # command-style tickets
    "new", "ticket",
    # pause/resume + info + privacy + rating
    "pause", "resume", "ticket-info", "private", "unprivate", "rate",
    # ticket categories
    "tcategory", "setcategory",
    # ticket admin (per-guild persisted settings — safe to grant)
    "ticketsettings", "ticketlog", "ticketblacklist", "ticketunblacklist",
    "tickets", "ticketstats", "ticketdebug", "permissionlevel", "tickethelp",
    # NOTE: "dbcleanup" is intentionally NOT here — it is DB surgery and
    # stays home-only even though the legacy domain router filed it under
    # the ticket domain.
)

_TICKETTOOL_PREMIUM: Tuple[str, ...] = (
    # premium tickettool package commands (identical in both editions)
    "naming", "schedule", "schedview", "claimconfig", "roleauto",
    "automate", "automatelist", "automatedelete", "escalate", "escalateroute",
    "escalationhistory", "transcriptconfig", "slaconfig", "slareport",
    "analytics", "csat", "staffstats", "export", "kb", "threadtickets",
    "staffthread", "channelrecycle", "locale", "localestring", "localelist",
    "brandedreplies", "flow", "flowlist", "flowattach", "flowdelete",
    "customcommand", "customcommandlist", "customcommandremove",
    "staffanalytics", "ticktrends", "responsedistribution", "panelembed",
    "panelembedlist", "panelembedremove", "panelembedenable", "modmessage",
    "modmessagelist", "modmessageremove", "flowapplication",
    "flowreviewconfig", "reviewpending", "reviewdecision", "transcriptconfig2",
    "canned",
)

_MODERATION_CORE: Tuple[str, ...] = (
    # member punishment
    "kick", "ban", "banid", "softban", "mute", "unmute", "tempmute",
    # warnings
    "warn", "warnings", "clearwarnings",
    # channel moderation
    "purge", "lock", "unlock", "slowmode", "nopurge",
    # role management (moderation)
    "addrole", "removerole", "roleall",
    # keyword blacklist / auto-ban
    "blacklist", "unblacklist", "blacklistscan", "blacklistlist", "checkprofile",
    # moderation logging / intake
    "msglog", "stickyrole", "report", "messageson", "messagesoff",
)

FEATURE_BUNDLES: Dict[str, dict] = {
    "verification": {
        "label": "Verification",
        "description": "Member verification flow, portals and staff review.",
        "commands": {
            # Full (single-portal verification)
            "verify", "verifyuser", "securitycheck",
            # Short (per-guild verification suite; group + standalone)
            "verification", "vsetup",
        },
    },
    "tickets": {
        "label": "Tickets",
        "description": "Ticket system: lifecycle, panels and the premium tickettool suite.",
        "commands": set(_TICKET_LIFECYCLE) | set(_TICKETTOOL_PREMIUM),
    },
    "reactionroles": {
        "label": "Reaction Roles",
        "description": "Reaction-role mappings (Carl-bot style).",
        "commands": {"reactionrole"},
    },
    "moderation": {
        "label": "Moderation",
        "description": "Punishments, warnings, channel moderation, keyword blacklist, message log.",
        "commands": set(_MODERATION_CORE),
    },
    "automod": {
        "label": "AutoMod",
        "description": "Automated rules engine with strikes (Short edition).",
        "commands": {"automod"},
    },
    "engagement": {
        "label": "Engagement",
        "description": "Polls, giveaways and invite tracking.",
        "commands": {
            # polls (both editions; Short exposes a group)
            "poll",
            # giveaways (Full)
            "giveaway", "endgiveaway",
            # invites — Full flat commands + Short per-guild group
            "setupinvites", "regenerateinvites", "checkinvites", "inviteinfo",
            "invites", "isetup",
        },
    },
    "leveling": {
        "label": "Leveling",
        "description": "XP, levels, rank cards and leaderboards.",
        "commands": {"level", "leaderboard", "rank"},
    },
    "general": {
        "label": "General Info",
        "description": "Harmless info commands: userinfo, serverinfo, membercount, uptime, role lists.",
        "commands": {"userinfo", "serverinfo", "membercount", "getallroles", "uptime_cmd"},
    },
    "branding": {
        "label": "Per-Guild Branding",
        "description": "Per-guild embed branding (footer/color/images). Global identity stays owner-only.",
        "commands": {"botbranding"},
    },
}

# Qualified-name overrides: subcommands whose parent bundle does not apply.
# "botbranding name" rewrites the GLOBAL gang name and "botbranding avatar"
# rewrites the bot's global avatar — both must stay home-only even when the
# per-guild branding bundle is granted.
QUALIFIED_OVERRIDES: Dict[str, str] = {
    "botbranding name": HOME_BUNDLE,
    "botbranding avatar": HOME_BUNDLE,
}

# Commands KNOWN to be home-only (administration / global config mutators /
# owner tooling). Kept explicit so ``!license catalog`` can distinguish
# "classified home-only" from "unclassified drift".
KNOWN_HOME_ONLY_COMMANDS: frozenset = frozenset({
    # owner settings + tutorial
    "ows", "tutorial",
    # global branding mutators
    "abrev", "gangname",
    # global channel/role/server/timing/limits config
    "setchannel", "channelsetup", "rolesetup", "serversetup",
    "timingsetup", "limitssetup", "csetup", "settings",
    # process/DB control + global audit
    "shutdown", "botstatus", "auditlog", "dbcleanup",
    # cross-server rules engine (bound to the home guild's rules messages)
    "updategangrules", "updateserverrules", "setserverrules",
    "setgangrules", "viewcachedrules",
})

# Reverse lookup for pretty-printing the home-only bucket.
_BUNDLE_OF_COMMAND: Dict[str, str] = {}
for _key, _spec in FEATURE_BUNDLES.items():
    for _name in _spec["commands"]:
        _BUNDLE_OF_COMMAND[_name] = _key
for _name in ALWAYS_AVAILABLE_COMMANDS:
    _BUNDLE_OF_COMMAND[_name] = "__always__"


def bundle_of(command_name: str) -> Optional[str]:
    """Bundle owning a TOP-LEVEL command name (or None when unclassified)."""
    if command_name in ALWAYS_AVAILABLE_COMMANDS:
        return "__always__"
    return _BUNDLE_OF_COMMAND.get(command_name)


def bundle_label(bundle_key: str) -> str:
    """Human-readable label for a bundle key ('__home__'/'__always__' aware)."""
    if bundle_key == HOME_BUNDLE:
        return "Home guild only"
    if bundle_key == "__always__":
        return "Always available"
    spec = FEATURE_BUNDLES.get(bundle_key)
    return spec["label"] if spec else bundle_key


def bundle_keys() -> List[str]:
    """Grantable bundle keys in catalog order."""
    return list(FEATURE_BUNDLES.keys())


def validate_bundle_names(names: Iterable[str]) -> Tuple[List[str], List[str]]:
    """Split `names` into (valid, unknown) bundle keys (case-insensitive)."""
    lowered = {k.lower(): k for k in FEATURE_BUNDLES}
    valid: List[str] = []
    unknown: List[str] = []
    for raw in names:
        key = lowered.get(str(raw).strip().lower())
        if key is None:
            unknown.append(str(raw).strip())
        elif key not in valid:
            valid.append(key)
    return valid, unknown


# ---------------------------------------------------------------------------
# Live command-map construction (run once, after every command is registered)
# ---------------------------------------------------------------------------

def _top_level_name(command) -> str:
    """Top-level command name for a (possibly nested) Command instance."""
    node = command
    while getattr(node, "parent", None) is not None:
        node = node.parent
    return node.name


def build_command_map(bot) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Classify every registered command into a bundle.

    Returns ``(top_level_map, qualified_map)`` where top_level_map is
    ``{top_command_name: bundle_key}`` and qualified_map holds subcommand
    overrides like ``{"botbranding name": "__home__"}``.
    """
    top_map: Dict[str, str] = {}
    qualified_map: Dict[str, str] = {}

    def walk(command) -> None:
        top = _top_level_name(command)
        qualified = command.qualified_name
        # Qualified overrides win for subcommands; the parent's top-level
        # classification wins for the group itself.
        if qualified in QUALIFIED_OVERRIDES:
            qualified_map[qualified] = QUALIFIED_OVERRIDES[qualified]
        if top not in top_map:
            top_map[top] = bundle_of(top) or HOME_BUNDLE
        for child in getattr(command, "commands", []):
            walk(child)

    for command in bot.commands:
        walk(command)
    return top_map, qualified_map


def audit_catalog(bot) -> List[Tuple[str, str, bool]]:
    """(command_name, bundle_key, is_drift) for every registered command.

    Drift = a command that resolved to home-only via the DEFAULT (not
    classified into any bundle, not always-available, and not on the
    KNOWN_HOME_ONLY list) — i.e. a command the catalog has never heard of.
    New commands should be classified (or added to the known-home list) so
    ``!license catalog`` stays a trustworthy report.
    """
    top_map, qualified_map = build_command_map(bot)
    report: List[Tuple[str, str, bool]] = []
    seen: Set[str] = set()

    def walk(command) -> None:
        top = _top_level_name(command)
        qualified = command.qualified_name
        if qualified not in seen:
            seen.add(qualified)
            if qualified in qualified_map:
                key = qualified_map[qualified]
                drift = False
            else:
                key = top_map.get(top, HOME_BUNDLE)
                drift = (key == HOME_BUNDLE) and (top not in KNOWN_HOME_ONLY_COMMANDS)
            report.append((qualified, key, drift))
        for child in getattr(command, "commands", []):
            walk(child)

    for command in bot.commands:
        walk(command)
    report.sort(key=lambda item: (item[1], item[0]))
    return report
