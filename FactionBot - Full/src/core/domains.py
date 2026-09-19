# -*- coding: utf-8 -*-
"""Multi-domain launcher machinery (legacy).

Single-instance deployments never call into this module; it exists so the
historical thin entry files (ModBot.py etc.) keep working if recreated."""

# stdlib + discord.py
import discord
import logging
import os
from typing import Dict, List, Optional

from core import state  # shared mutable runtime state
from core.lifecycle import save_all_data
from config.environment import _clean_token_value, is_placeholder_token, load_local_env_file, read_token_from_file




# =============================================================================
# MULTI-BOT DOMAIN SUPPORT (ModBot / TicketBot / UtilityBot)
# =============================================================================
# The project can run as SEVERAL Discord bots at once, each with its own
# token and its own entry file (src/ModBot.py, TicketBot.py,
# UtilityBot.py — see RunBots.py). Each domain bot keeps ONLY the commands,
# events and background tasks of its domain; all instances share the same
# SQLite database (WAL mode) so tickets/panels/settings are common.
#
# Running plain `python Bot.py` = the original full bot (all domains),
# exactly as before. See the tail of this file for the launch logic.
# =============================================================================

# Domain name for THIS process: 'full' for the classic single bot, or one of
# 'mod' | 'ticket' | 'utility' when launched via a domain entry file.
INSTANCE_DOMAIN: str = (os.environ.get('FACTIONBOT_INSTANCE') or 'full').strip().lower()

# Domains whose commands/events/tasks this instance handles.
ACTIVE_DOMAINS: set = {'mod', 'ticket', 'utility'}

# Commands owned by each domain (Bot.py commands; premium-package commands
# are added below after registration). Anything unlisted defaults to utility.
TICKET_DOMAIN_COMMANDS: set = {
    # core ticket lifecycle
    'add', 'remove', 'claim', 'unclaim', 'close', 'closerequest', 'reopen',
    'transcript', 'rename', 'move', 'note', 'notes', 'priority',
    # panels
    'panel', 'panels', 'deletepanel', 'panelupdate', 'panelquestion',
    'multipanel', 'dropdownpanel', 'reactionpanel', 'limitbypass',
    # command-style tickets
    'new', 'ticket',
    # pause/resume + info + privacy + rating
    'pause', 'resume', 'ticket-info', 'private', 'unprivate', 'rate',
    # ticket categories (internal folders)
    'tcategory', 'setcategory',
    # ticket admin/diagnostics
    'ticketsettings', 'ticketlog', 'ticketblacklist', 'ticketunblacklist',
    'tickets', 'ticketstats', 'ticketdebug', 'permissionlevel', 'tickethelp',
    'dbcleanup',
}
MOD_DOMAIN_COMMANDS: set = {
    # keyword blacklist / auto-ban
    'blacklist', 'blacklistlist', 'blacklistscan', 'unblacklist', 'checkprofile',
    # warnings
    'warn', 'warnings', 'clearwarnings',
    # channel moderation
    'purge', 'lock', 'unlock', 'slowmode', 'nopurge',
    # member punishment
    'ban', 'banid', 'kick', 'softban', 'mute', 'unmute', 'tempmute',
    # role management (moderation)
    'addrole', 'removerole', 'roleall',
    # verification / security
    'verify', 'verifyuser', 'securitycheck',
    # moderation logging / intake
    'msglog', 'auditlog', 'report',
}
# Everything else (leveling, giveaways, invites, verification UI, rules,
# setup commands, OWS, polls, sticky roles, branding, misc) = utility.


def _command_domain(name: str) -> str:
    """Domain owning a command (used for domain-bot command pruning).

    Config moves (ModBot_Cmds=... / FunBot_Cmds=...) are checked FIRST — a
    command explicitly assigned to a bot always wins over the defaults."""
    moved = _moved_command_map()
    if name in moved:
        return moved[name]
    if name in TICKET_DOMAIN_COMMANDS:
        return 'ticket'
    if name in MOD_DOMAIN_COMMANDS:
        return 'mod'
    return 'utility'


def domain_active(domain: str) -> bool:
    """True when THIS instance was launched FOR the given domain."""
    return domain in ACTIVE_DOMAINS


# =============================================================================
# CUSTOM BOTS (any name beyond the 3 standard domains)
# =============================================================================
# tokens.txt / .env can define MORE bots, e.g.:
#     FunBot_Token=...              (token — required)
#     FunBot_Cmds=poll,giveaway     (commands it runs — required)
# The listed commands are MOVED to that bot from whichever standard bot
# normally runs them. RunBots.py launches every configured bot automatically.
# =============================================================================

_RESERVED_DOMAIN_NAMES = {'mod', 'ticket', 'utility', 'all', 'bot', 'custom',
                          'full', 'new', 'multi'}
_CUSTOM_DOMAINS_CACHE = None
_MOVED_COMMANDS_CACHE = None
_BASE_MOVES_CACHE = None


def _read_all_config_pairs() -> Dict[str, str]:
    """Every KEY=value pair from tokens.txt plus the environment (.env is
    loaded into the environment first, so it's included). Only well-formed
    config keys (letters/digits/underscore) are kept, so prose and template
    instructions can never be mistaken for configuration."""
    import re as _key_re
    valid_key = _key_re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
    pairs: Dict[str, str] = {}
    load_local_env_file()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for filepath in (
        os.path.join(script_dir, 'tokens.txt'),
        os.path.join(script_dir, '..', 'tokens.txt'),
        os.path.join(os.getcwd(), 'tokens.txt'),
        'tokens.txt',
    ):
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    for raw in f:
                        line = raw.strip()
                        if not line or line.startswith('#') or '=' not in line:
                            continue
                        key, value = line.split('=', 1)
                        key = key.strip()
                        if key and valid_key.match(key):
                            pairs.setdefault(key, value.strip())
                break
        except Exception:
            continue
    for key, value in os.environ.items():
        if value:
            pairs[key] = value
    return pairs


def resolve_custom_domains() -> Dict[str, Dict]:
    """Custom bots from config: every '<Name>Bot_Token' that isn't one of
    the three standard domains, plus its '<Name>Bot_Cmds' command list.

    Returns {domain_name: {'token': str, 'commands': [str]}}."""
    global _CUSTOM_DOMAINS_CACHE
    if _CUSTOM_DOMAINS_CACHE is not None:
        return _CUSTOM_DOMAINS_CACHE
    import re as _re
    token_re = _re.compile(r'^([A-Za-z0-9]+)Bot_Token$', _re.IGNORECASE)
    cmds_re = _re.compile(r'^([A-Za-z0-9]+)Bot_Cmds$', _re.IGNORECASE)
    cmds_re2 = _re.compile(r'^([A-Za-z0-9]+)Bot_Commands$', _re.IGNORECASE)
    pairs = _read_all_config_pairs()
    domains: Dict[str, Dict] = {}
    cmds_by_name: Dict[str, List[str]] = {}
    for key, value in pairs.items():
        m = token_re.match(key)
        if m:
            name = m.group(1).lower()
            if name in _RESERVED_DOMAIN_NAMES or name in _DOMAIN_ORDER:
                continue
            token = _first_token(value)
            if token:
                domains[name] = {'token': token, 'commands': []}
            continue
        m = cmds_re.match(key) or cmds_re2.match(key)
        if m:
            name = m.group(1).lower()
            if name in _DOMAIN_ORDER:
                continue  # base-domain moves handled by _resolve_base_moves()
            cmds = [c.strip() for c in value.split(',') if c.strip()]
            if cmds:
                cmds_by_name.setdefault(name, []).extend(cmds)
    for name, dom in domains.items():
        dom['commands'] = cmds_by_name.pop(name, [])
    for name, cmds in cmds_by_name.items():
        if name not in _RESERVED_DOMAIN_NAMES:
            logging.warning(f"[Startup] {name.capitalize()}Bot_Cmds found but no "
                            f"{name.capitalize()}Bot_Token — that bot is not started.")
    if domains:
        logging.info(f"[Startup] Custom bots configured: "
                     f"{', '.join(d + 'Bot' for d in sorted(domains))}")
    _CUSTOM_DOMAINS_CACHE = domains
    return domains


def _resolve_base_moves() -> Dict[str, List[str]]:
    """Command moves onto the STANDARD bots from config
    (ModBot_Cmds=..., TicketBot_Cmds=..., UtilityBot_Cmds=...)."""
    global _BASE_MOVES_CACHE
    if _BASE_MOVES_CACHE is not None:
        return _BASE_MOVES_CACHE
    import re as _re
    moves: Dict[str, List[str]] = {}
    cmds_re = _re.compile(r'^([A-Za-z0-9]+)Bot_Cmds$', _re.IGNORECASE)
    for key, value in _read_all_config_pairs().items():
        m = cmds_re.match(key)
        if not m:
            continue
        name = m.group(1).lower()
        if name in _DOMAIN_ORDER:
            cmds = [c.strip() for c in value.split(',') if c.strip()]
            if cmds:
                moves.setdefault(name, []).extend(cmds)
    _BASE_MOVES_CACHE = moves
    return moves


def _moved_command_map() -> Dict[str, str]:
    """{command_name: owning_domain} for every command explicitly assigned
    via <Name>Bot_Cmds (standard OR custom bots). Unknown command names are
    warned + skipped; conflicts go to the first bot in order (mod, ticket,
    utility, then customs alphabetically)."""
    global _MOVED_COMMANDS_CACHE
    if _MOVED_COMMANDS_CACHE is not None:
        return _MOVED_COMMANDS_CACHE
    known_commands = {c.name for c in state.bot.commands}
    result: Dict[str, str] = {}

    def _claim(domain: str, raw_cmds: List[str]) -> None:
        for cmd in raw_cmds:
            cmd = cmd.strip().split()[0] if cmd.strip() else ''  # drop inline annotations
            if not cmd or ' ' in cmd or not cmd.replace('-', '').replace('_', '').isalnum():
                logging.warning(f"[Startup] {domain.capitalize()}Bot_Cmds lists invalid "
                                f"entry '{cmd}' — ignored.")
                continue
            if cmd not in known_commands:
                logging.warning(f"[Startup] {domain.capitalize()}Bot_Cmds lists unknown "
                                f"command '{cmd}' — ignored (see /cmds for valid names).")
                continue
            if cmd in result:
                logging.warning(f"[Startup] Command '{cmd}' is claimed by both "
                                f"{result[cmd].capitalize()}Bot and {domain.capitalize()}Bot — "
                                f"keeping it on {result[cmd].capitalize()}Bot.")
                continue
            result[cmd] = domain

    for base in _DOMAIN_ORDER:
        if base in _resolve_base_moves():
            _claim(base, _resolve_base_moves()[base])
    for custom in sorted(resolve_custom_domains()):
        _claim(custom, resolve_custom_domains()[custom].get('commands') or [])
    _MOVED_COMMANDS_CACHE = result
    return result


def custom_domain_names() -> List[str]:
    """Custom bot domain names, alphabetically."""
    return sorted(resolve_custom_domains().keys())


def _all_domain_names() -> List[str]:
    """Every possible domain: the 3 standard ones + customs."""
    return list(_DOMAIN_ORDER) + custom_domain_names()


def _default_domain_of(command: str) -> str:
    """Which standard domain owns a command BEFORE any config moves."""
    if command in TICKET_DOMAIN_COMMANDS:
        return 'ticket'
    if command in MOD_DOMAIN_COMMANDS:
        return 'mod'
    return 'utility'


def commands_owned_by(domain: str) -> List[str]:
    """Every command this domain runs (defaults + config moves)."""
    if domain == 'mod':
        return sorted(MOD_DOMAIN_COMMANDS | {c for c, d in _moved_command_map().items() if d == 'mod'})
    if domain == 'ticket':
        return sorted(TICKET_DOMAIN_COMMANDS | {c for c, d in _moved_command_map().items() if d == 'ticket'})
    if domain == 'utility':
        moved_away = {c for c, d in _moved_command_map().items() if d != 'utility'}
        all_cmds = {c.name for c in state.bot.commands}
        return sorted((all_cmds - MOD_DOMAIN_COMMANDS - TICKET_DOMAIN_COMMANDS - moved_away)
                      | {c for c, d in _moved_command_map().items() if d == 'utility'})
    # custom bot: exactly its moved commands
    return sorted(c for c, d in _moved_command_map().items() if d == domain)


def _task_domain_owner(base_domain: str) -> Optional[str]:
    """Which instance runs base_domain's background events/tasks.

    The standard bot if it's deployed; otherwise the FIRST other bot (in
    order mod, ticket, utility, customs) that owns at least one command of
    that domain — so a FunBot that took all the utility commands also
    inherits the utility background jobs when UtilityBot isn't running."""
    if resolve_domain_token(base_domain):
        return base_domain
    for domain in _all_domain_names():
        if domain == base_domain:
            continue
        owned = commands_owned_by(domain)
        if any(_default_domain_of(c) == base_domain for c in owned):
            return domain
    return None


def instance_handles(base_domain: str) -> bool:
    """True when THIS instance must run base_domain's events + background
    tasks (the domain-active check, with inheritance for custom bots that
    replace an undeployed standard bot)."""
    if INSTANCE_DOMAIN == 'full':
        return True
    if INSTANCE_DOMAIN == base_domain:
        return True
    if INSTANCE_DOMAIN in resolve_custom_domains():
        return _task_domain_owner(base_domain) == INSTANCE_DOMAIN
    return False


def _is_lead_instance() -> bool:
    """True for the single instance that runs cross-domain singleton work
    (owner tutorial, report DMs, lock file cleanup).

    Deterministic across processes: every instance reads the SAME token
    config, so they agree on which domains are deployed; the lead is the
    first deployed domain in the fixed order (mod, ticket, utility, customs)."""
    if INSTANCE_DOMAIN == 'full':
        return True
    deployed = [d for d in _all_domain_names() if d in resolve_deployed_domains()]
    return bool(deployed) and INSTANCE_DOMAIN == deployed[0]


# =====================================================================
# MULTI-BOT DOMAIN TOKEN RESOLUTION
#
# tokens.txt (or .env) can define one token PER BOT, exactly like:
#     ModBot_Token=...      -> src/ModBot.py   (moderation)
#     TicketBot_Token=...   -> src/TicketBot.py (tickets)
#     UtilityBot_Token=...  -> src/UtilityBot.py (utility)
#
# A comma-separated list also works positionally:
#     BOT_TOKENS=modToken,ticketToken,utilityToken
# (assigned to Mod / Ticket / Utility in that order; extras are ignored).
# Extra tokens after a comma on a NAMED line are ignored (one bot per domain
# — two instances of the same domain would double-handle everything).
# =====================================================================

DOMAIN_ENTRY_FILES = {'mod': 'ModBot.py', 'ticket': 'TicketBot.py', 'utility': 'UtilityBot.py'}
DOMAIN_LABELS = {'mod': 'ModBot', 'ticket': 'TicketBot', 'utility': 'UtilityBot'}
_DOMAIN_ORDER = ('mod', 'ticket', 'utility')

_DOMAIN_TOKEN_KEYS = {
    'mod': ('ModBot_Token', 'ModBot_Tokens', 'MOD_BOT_TOKEN', 'MOD_BOT_TOKENS',
            'Mod_Token', 'MOD_TOKEN'),
    'ticket': ('TicketBot_Token', 'TicketBot_Tokens', 'TICKET_BOT_TOKEN',
               'TICKET_BOT_TOKENS', 'Ticket_Token', 'TICKET_TOKEN'),
    'utility': ('UtilityBot_Token', 'UtilityBot_Tokens', 'UTILITY_BOT_TOKEN',
                'UTILITY_BOT_TOKENS', 'Utility_Token', 'UTILITY_TOKEN'),
}


def _first_token(raw: Optional[str]) -> Optional[str]:
    """First valid token from a comma-separated value (placeholders/empties
    skipped, inline annotations after whitespace ignored); extra tokens are
    ignored with a warning."""
    cleaned = _clean_token_value(raw)
    if not cleaned:
        return None
    tokens = [t.strip() for t in cleaned.split(',') if t.strip()]
    valid = [t for t in tokens if not is_placeholder_token(t)]
    if len(valid) > 1:
        logging.warning(
            f"[Startup] Multiple tokens on one line ({len(valid)} found) — only the "
            f"first is used. Run ONE bot per domain; add separate bots as their own "
            f"domain entries instead.")
    return valid[0] if valid else None


def _resolve_domain_token_from_config(domain: str) -> Optional[str]:
    """Resolve a domain bot's token from .env/tokens.txt named variables."""
    load_local_env_file()
    for key in _DOMAIN_TOKEN_KEYS[domain]:
        for source in (os.environ.get(key), read_token_from_file(key)):
            token = _first_token(source)
            if token:
                return token
    return None


def _resolve_positional_tokens() -> Dict[str, str]:
    """Resolve the comma-separated BOT_TOKENS list into domain tokens
    (positional: mod, ticket, utility)."""
    load_local_env_file()
    raw = None
    for key in ('BOT_TOKENS', 'BOT_Tokens', 'All_Tokens'):
        raw = os.environ.get(key) or read_token_from_file(key)
        if raw:
            break
    if not raw:
        return {}
    tokens = [t.strip() for t in raw.split(',') if t.strip() and not is_placeholder_token(t)]
    result: Dict[str, str] = {}
    for domain, token in zip(_DOMAIN_ORDER, tokens):
        result[domain] = token
    if len(tokens) > len(_DOMAIN_ORDER):
        logging.warning(f"[Startup] BOT_TOKENS had {len(tokens)} tokens; only the first "
                        f"{len(_DOMAIN_ORDER)} are used (mod, ticket, utility).")
    return result


def resolve_domain_token(domain: str) -> Optional[str]:
    """A domain bot's token: named variable first, then the positional list."""
    if domain not in _DOMAIN_ORDER:
        return None
    return _resolve_domain_token_from_config(domain) or _resolve_positional_tokens().get(domain)


def resolve_deployed_domains() -> set:
    """Every domain (standard OR custom) that has a token configured — used by
    all instances to agree on who is the lead bot."""
    deployed = set()
    for domain in _DOMAIN_ORDER:
        if resolve_domain_token(domain):
            deployed.add(domain)
    deployed.update(resolve_custom_domains().keys())
    return deployed


def resolve_all_domain_tokens() -> Dict[str, str]:
    """{domain: token} for every configured domain bot."""
    return {d: resolve_domain_token(d) for d in _DOMAIN_ORDER if resolve_domain_token(d)}


# =====================================================================
# DOMAIN COMMAND PRUNING + LAUNCH
# =====================================================================

def _prune_commands_to_domain(domain: str) -> Dict[str, int]:
    """Remove every command NOT owned by `domain` from this instance.

    Prunes the prefix command registry (bot.remove_command), so a domain bot
    only answers its own commands."""
    removed_prefix = 0
    keep = {domain}

    # Prefix side (groups remove their subcommands).
    for cmd in list(state.bot.commands):
        if _command_domain(cmd.name) not in keep:
            try:
                state.bot.remove_command(cmd.name)
                removed_prefix += 1
            except Exception as exc:
                logging.warning(f"[Domain] prefix prune failed for {cmd.name}: {exc}")

    return {'prefix': removed_prefix}


def launch_domain_bot(domain: str) -> None:
    """Run THIS file's bot as a single-domain instance.

    Called by the thin entry files (ModBot.py / TicketBot.py /
    UtilityBot.py — and CustomBot.py for any custom bot defined via
    <Name>Bot_Token + <Name>Bot_Cmds). Prunes foreign-domain commands,
    activates only this domain's events + background tasks, and runs with
    the domain's token."""
    global INSTANCE_DOMAIN, ACTIVE_DOMAINS

    customs = resolve_custom_domains()
    is_custom = domain in customs
    if domain not in _DOMAIN_ORDER and not is_custom:
        known = ', '.join(_DOMAIN_ORDER) + ' (or any custom bot with a <Name>Bot_Token)'
        print(f"Unknown bot '{domain}'. Valid: {known}")
        exit(1)

    label = DOMAIN_LABELS.get(domain, domain.capitalize())
    if is_custom:
        token = customs[domain]['token']
    else:
        token = resolve_domain_token(domain)
    if not token:
        print("=" * 60)
        print(f"No token configured for the {label} bot!")
        print("Add one of these to .env or tokens.txt:")
        print(f"  {label}_Token=YOUR_TOKEN_HERE")
        print("(or a comma-separated BOT_TOKENS list: mod,ticket,utility)")
        print("=" * 60)
        exit(1)

    INSTANCE_DOMAIN = domain
    ACTIVE_DOMAINS = {domain}

    counts = _prune_commands_to_domain(domain)
    total_kept = len(state.bot.commands)
    if total_kept == 0:
        print("=" * 60)
        print(f"The {label} bot has no commands to run!")
        if is_custom:
            print(f"{label}_Token is set, but {label}_Cmds is missing or lists")
            print("only unknown command names. Add a command list, e.g.:")
            print(f"  {label}_Cmds=poll,giveaway,endgiveaway")
            print("The listed commands are MOVED to this bot from the bot that")
            print("normally runs them (see the bottom of tokens.txt).")
        else:
            print(f"Check {label}_Cmds in tokens.txt — it may list only unknown commands.")
        print("=" * 60)
        exit(1)

    print("=" * 60)
    print(f"{label} — starting ({domain} domain)")
    print(f"Commands kept: {total_kept} "
          f"(pruned {counts['prefix']} commands from other domains)")
    print("=" * 60)
    logging.info(f"[Domain] {label} instance: kept {total_kept} commands, "
                 f"pruned {counts['prefix']} commands; "
                 f"lead instance: {_is_lead_instance()}")

    try:
        state.bot.run(token)
    finally:
        save_all_data()
