# -*- coding: utf-8 -*-
import signal
import discord
from discord.ext import commands
import os
import logging
import sys
from logging.handlers import RotatingFileHandler

# Force UTF-8 output on all platforms (fixes garbled emoji/symbols on Windows)
try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass


# ═══════════════════════════════════════════════════════════════════════════
# LOGGING — configured FIRST, before anything else, so EVERY error is visible.
# Previously this was called at line 16085, meaning 156 logging.*() calls
# before it produced NO output — silently swallowing startup errors.
# ═══════════════════════════════════════════════════════════════════════════
from core.paths import _DATA_DIR



def setup_logging() -> None:
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    _instance = (os.environ.get('FACTIONBOT_INSTANCE') or '').strip().lower()
    _log_name = f'bot-{_instance}.log' if _instance else 'bot.log'
    try:
        _log_dir = os.path.join(_DATA_DIR, 'logs')
        os.makedirs(_log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(_log_dir, _log_name),
            maxBytes=1024*1024,
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setFormatter(log_formatter)
    except Exception:
        file_handler = None
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(log_formatter)
    handlers = [h for h in [file_handler, stream_handler] if h is not None]
    logging.basicConfig(level=logging.INFO, handlers=handlers)

setup_logging()
logging.info("[Startup] Logging configured — beginning bot initialization...")



# ═══════════════════════════════════════════════════════════════════════════
# TICKETTOOL + REACTIONROLES — real packages.
#
# Both feature suites live as real packages inside src/packages/:
#   packages/tickettool/    (26 modules — analytics, automations, claiming, SLA, …)
#   packages/reactionroles/ (5 modules — db, system, modal, commands, wiring)
#
# They keep the exact same contract as the old inline loader:
#   TicketTool.commands.register(bot)      + TicketTool.wiring.on_* hooks
#   ReactionRoles.commands.register(bot)   + ReactionRoles.wiring.on_* hooks
# ═══════════════════════════════════════════════════════════════════════════
from packages import tickettool as TicketTool
from packages import reactionroles as ReactionRoles

# ═══════════════════════════════════════════════════════════════════════════
# LAYERED STRUCTURE (SaaS layout):
#   core/     paths, state, models, data_manager, lifecycle, ows,
#             process_manager, domains, helpers
#   config/   environment (tokens) + settings (Config)
#   utils/    ui embed builders + generic paginated view
#   modules/  feature modules per domain (commands + events + tasks),
#             family contract register(bot)
#   packages/ tickettool + reactionroles feature packages
# ═══════════════════════════════════════════════════════════════════════════
from core import state
from core.state import config, data_manager
from core.ows import hydrate_ows_settings, ows_get
from core.helpers import PREMIUM_AVAILABLE, RR_AVAILABLE
from core.domains import TICKET_DOMAIN_COMMANDS
from core.lifecycle import save_all_data, import_json_to_sqlite
from config.environment import get_bot_token, is_placeholder_token
from core.process_manager import process_manager
from utils.ui.embeds import EmbedBuilder

# setup_hook needs these from the feature modules (runtime references only)
from modules.moderation.moderation import load_blacklist_data
from modules.factions.rules import load_rules_cache
from modules.engagement.giveaways import load_giveaways_data
from modules.engagement.leveling import load_levels_data
from modules.verification.verification import VerificationButtonsView, ImprovedStaffActionView
from modules.support.tickets.views import TicketControlView, TicketModeratorView, TicketPanelSelectView
from modules.engagement.invites import InviteTrackingView
from modules.support.info import GetAllRolesView



# --- BOT INTENTS AND INSTANCE ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.invites = True
intents.presences = True
intents.reactions = True


class TicketBot(commands.Bot):
    """commands.Bot subclass with a one-time setup_hook.

    discord.py calls setup_hook() EXACTLY ONCE during startup, before
    on_ready. This is the correct place for DB initialization and other
    one-time work that must not repeat when the gateway reconnects and
    on_ready fires again.

    Previously, data_manager.connect() and several load_*() calls lived
    inside on_ready. Because on_ready can fire multiple times (on every
    gateway reconnect), those calls would re-run — and even though
    DataManager.connect() is now idempotent (it early-returns if already
    connected), re-running the loads is wasteful and re-registering
    persistent views is noisy. setup_hook eliminates the concern entirely.
    """

    async def setup_hook(self) -> None:
        # --- One-time DB + cache initialization (runs exactly once) ---
        data_manager.connect()                # idempotent: safe even if called again
        hydrate_ows_settings()
        config.load_branding_settings()
        config.load_channel_settings()
        config.load_role_settings()
        config.load_server_settings()
        config.load_timing_settings()
        config.load_limits_settings()

        # Load persisted state from the (now-connected) SQLite DB into the
        # in-memory caches. These must run before on_ready so the caches
        # are populated before the first guild event arrives.
        load_blacklist_data()
        load_rules_cache()
        load_giveaways_data()
        load_levels_data()

        # Warm the no-purge exclusion cache so !nopurge-protected messages
        # are respected by the auto-purge system and !purge/!purgeall from
        # the very first message, without a lazy-load DB hit on each check.
        try:
            data_manager.load_all_no_purge_message_ids()
        except Exception as exc:
            logging.warning(f"[NoPurge] could not load exclusion cache on startup: {exc}")

        # One-time JSON → SQLite migration (if enabled and not already done).
        if ows_get("json_to_sqlite_import"):
            import_json_to_sqlite()

        # Pre-register the generic persistent views (the per-panel and
        # per-giveaway ones are registered in on_ready, where self.guilds is
        # available — setup_hook runs before guilds are fully cached).
        self.add_view(VerificationButtonsView())
        self.add_view(ImprovedStaffActionView(0, "", discord.Embed()))
        self.add_view(TicketControlView(""))
        self.add_view(TicketModeratorView())
        # Generic dropdown-panel select (stable custom_id). Real per-message
        # views with live panel options are re-registered in on_ready from
        # the multi_panels table; this covers the setup→ready window.
        try:
            self.add_view(TicketPanelSelectView([]))
        except Exception as exc:
            logging.warning(f"[SetupHook] generic panel-select registration failed: {exc}")
        self.add_view(InviteTrackingView(None))
        self.add_view(GetAllRolesView())
        logging.info("[SetupHook] One-time initialization complete (DB + caches + generic views)")

        # --- PREMIUM TIER 1 SCHEMA INSTALL ---
        # Installs new tables + idempotent column migrations on top of the
        # base ticket_* tables created above. Safe to call repeatedly.
        # NOTE: stashes EmbedBuilder on the bot so extracted packages
        # (ReactionRoles, etc.) can build branded embeds without importing
        # from this monolith module.
        self.embed_builder = EmbedBuilder
        if PREMIUM_AVAILABLE:
            try:
                TicketTool.wiring.on_setup_hook(data_manager, self)
            except Exception as exc:
                logging.exception(f"[SetupHook] TicketTool.on_setup_hook failed: {exc}")

        # --- REACTION ROLES SCHEMA INSTALL + ACCESSOR ---
        # Installs the reaction_roles table (idempotent) and stashes the
        # ReactionRolesDB accessor on the bot as `bot.reaction_roles_db`.
        # Safe to call repeatedly. Mirrors the TicketTool contract above.
        if RR_AVAILABLE:
            try:
                ReactionRoles.wiring.on_setup_hook(data_manager, self)
            except Exception as exc:
                logging.exception(f"[SetupHook] ReactionRoles.on_setup_hook failed: {exc}")


print("[Startup] Creating bot instance...")
bot = TicketBot(command_prefix=config.command_prefix, intents=intents)
print(f"[Startup] Bot instance created. Commands so far: {len(bot.commands)}")

# Register TicketTool prefix commands on the bot instance.
if PREMIUM_AVAILABLE:
    try:
        _cmds_before_premium = {c.name for c in bot.commands}
        TicketTool.commands.register(bot)
        TICKET_DOMAIN_COMMANDS |= ({c.name for c in bot.commands} - _cmds_before_premium)
        logging.info("[TicketTool] registered prefix commands (inline)")
        print(f"[Startup] TicketTool commands registered. Total commands: {len(bot.commands)}")
    except Exception as exc:
        logging.exception(f"[TicketTool] command registration failed: {exc}")
        print(f"[Startup] WARNING: TicketTool registration FAILED: {exc}")

# Register ReactionRoles prefix commands (!rr group + 5 subcommands).
if RR_AVAILABLE:
    try:
        _cmds_before_rr = {c.name for c in bot.commands}
        ReactionRoles.commands.register(bot)
        logging.info("[ReactionRoles] registered prefix commands (inline)")
        print(f"[Startup] ReactionRoles commands registered. Total commands: {len(bot.commands)}")
    except Exception as exc:
        logging.exception(f"[ReactionRoles] command registration failed: {exc}")
        print(f"[Startup] WARNING: ReactionRoles registration FAILED: {exc}")

# ═══════════════════════════════════════════════════════════════════════════
# COG REGISTRATION — every feature module registers itself on the bot
# through the family contract: register(bot). Deterministic, synchronous,
# and identical in spirit to TicketTool/ReactionRoles command registration.
# ═══════════════════════════════════════════════════════════════════════════
from modules import register_all
import modules.runtime.events as _events_mod

# Import every feature module in EXTENSIONS order and call register(bot).
register_all(bot)
_events_mod.register_events(bot)
logging.info("[Modules] registered lifecycle event handlers")
state.bot = bot



# --- BOT EVENTS ---
def signal_handler(sig, frame) -> None:
    logging.info("Shutdown signal received. Saving data...")
    save_all_data()
    data_manager.close()
    process_manager.clear_lock_file()
    logging.info("Data saved. Goodbye!")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)


# --- MAIN ENTRY POINT ---
def main() -> None:
    """Entry point: runs the single bot with all features."""
    token = get_bot_token()
    if token is None or is_placeholder_token(token):
        print("=" * 60)
        print("ERROR: Bot token not found!")
        print("=" * 60)
        print("Put your Discord bot token in one of these files:")
        print("  • .env          (project root)  ->  BOT_TOKEN=your-token")
        print("  • tokens.txt    (project root)  ->  BOT_Token=your-token")
        print("Then run:  python src/bot.py")
        print("=" * 60)
        logging.error("Bot token not found.")
        exit(1)

    print("=" * 50)
    print(f"{config.gang_name} BOT - Starting (By IdkAnymore_039)")
    print("=" * 50)
    try:
        bot.run(token)
    except (discord.LoginFailure, discord.HTTPException) as exc:
        # Discord rejected the token — either leaked (auto-invalidated) or
        # copied incorrectly. Both 401 and LoginFailure surface here.
        print("=" * 60)
        print("LOGIN FAILED — Discord rejected the bot token.")
        print("=" * 60)
        print("The token loaded fine but Discord says it's invalid.")
        print("Most likely the token was leaked and auto-reset by Discord.")
        print()
        print("Generate a NEW token:")
        print("  https://discord.com/developers/applications")
        print("  -> your app -> Bot -> Reset Token")
        print()
        print("Paste it into .env or tokens.txt and run Bot.py again.")
        print("=" * 60)
        logging.error(f"[Startup] Login failed: {exc}")
        exit(1)
    finally:
        save_all_data()
        process_manager.clear_lock_file()


if __name__ == "__main__":
    print(f"[Startup] Module loaded completely. {len(bot.commands)} commands registered. Starting main()...")
    main()