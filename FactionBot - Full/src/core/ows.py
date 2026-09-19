# -*- coding: utf-8 -*-
"""Owner Web Settings (OWS) — persisted owner toggles + apply hooks."""

# stdlib + discord.py
import asyncio
import discord
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from core import state  # shared mutable runtime state
from core.state import config, data_manager
from core.helpers import brand_text



# Global dictionary to temporarily hold page offsets and chain status for chained commands
# Format: {message_id: (page_offset, is_chained)}

# =========================================================================
# OWNER SETTINGS HELPERS
# =========================================================================
def get_owner_setting(key: str, default: bool = True) -> bool:
    """Read a boolean owner setting from the SQLite DB."""
    val = data_manager.get_config_value(f"ows_{key}")
    if val is None:
        return default
    return val == "1"

def set_owner_setting(key: str, value: bool) -> None:
    """Save a boolean owner setting to the SQLite DB."""
    data_manager.set_config_value(f"ows_{key}", "1" if value else "0")

# =========================================================================
# !OWS — OWNER SETTINGS (Complete Feature Toggle System)
# =========================================================================
# Discord-native interactive settings panel (NOT a web dashboard).
# Every toggle is persisted to SQLite bot_config via set_owner_setting()
# with the `ows_` prefix. In-memory config.enable_* flags are hydrated
# from the DB on startup via hydrate_ows_settings().
# =========================================================================

@dataclass
class OWSToggle:
    key: str
    label: str
    description: str
    category: str
    default: bool = True
    emoji: str = ""
    apply: Optional[Callable[[bool], None]] = None

OWS_CATEGORIES: List[str] = [
    "🧩 Core Systems",
    "🤖 Automation",
    "⚙️ Moderation",
    "🎫 Tickets",
    "⭐ Premium",
    "🧹 Logging",
    "🚀 Startup/Owner",
    "🛡️ Security",
    "📊 Limits",
    "💬 Branding",
    "🔧 Misc",
]

def _ows_apply_leveling(v: bool) -> None:
    config.enable_leveling = v

def _ows_apply_tickets(v: bool) -> None:
    config.enable_tickets = v

def _ows_apply_giveaways(v: bool) -> None:
    from modules.engagement.giveaways import check_giveaways_task  # deferred import (cycle-safe)
    config.enable_giveaways = v
    try:
        if v and not check_giveaways_task.is_running():
            check_giveaways_task.start()
        elif not v and check_giveaways_task.is_running():
            check_giveaways_task.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] giveaway task toggle error: {exc}")

def _ows_apply_warnings(v: bool) -> None:
    config.enable_warnings = v

def _ows_apply_debug(v: bool) -> None:
    config.debug_mode = v

async def apply_bot_presence() -> None:
    """Apply the bot's Discord presence based on the `use_bot_status` toggle.

    - toggle ON  -> "Watching <bot_status>" (uses brand_text())
    - toggle OFF -> clear the activity entirely

    Safe to call before the gateway is ready (it's awaited from on_ready,
    which only fires after the gateway cache is populated) and on every
    reconnect, since Discord clears presence on reconnect.
    """
    try:
        if ows_get("use_bot_status"):
            await state.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=brand_text(config.bot_status),
                )
            )
        else:
            await state.bot.change_presence(activity=None)
    except Exception as exc:
        # Don't let a presence failure abort on_ready.
        logging.warning(f"[Presence] Could not apply bot status: {exc}")


def _ows_apply_bot_status(v: bool) -> None:
    """OWS callback for the `use_bot_status` toggle.

    Fires immediately when the owner flips the toggle in the !ows panel so
    the presence updates live without a reconnect. We can't await inside a
    sync callback, so we schedule the coroutine on the running loop.
    """
    try:
        fut = asyncio.run_coroutine_threadsafe(apply_bot_presence(), state.bot.loop)
        # Don't block the OWS UI thread for long; 5s is plenty for a presence call.
        fut.result(timeout=5)
    except Exception as exc:
        logging.warning(f"[OWS] use_bot_status apply error: {exc}")


def _ows_apply_periodic(v: bool) -> None:
    from modules.moderation.moderation import send_report_message  # deferred import (cycle-safe)
    state.messages_enabled = v
    try:
        if v and not send_report_message.is_running():
            send_report_message.start()
        elif not v and send_report_message.is_running():
            send_report_message.stop()
    except Exception as exc:
        logging.warning(f"[OWS] broadcast task toggle error: {exc}")

def _ows_apply_scheduled_scan(v: bool) -> None:
    from modules.moderation.moderation import auto_blacklist_scan  # deferred import (cycle-safe)
    try:
        if v and not auto_blacklist_scan.is_running():
            auto_blacklist_scan.start()
        elif not v and auto_blacklist_scan.is_running():
            auto_blacklist_scan.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] blacklist scan task toggle error: {exc}")

def _ows_apply_sla(v: bool) -> None:
    from modules.support.tickets.engine import check_sla_task  # deferred import (cycle-safe)
    try:
        if v and not check_sla_task.is_running():
            check_sla_task.start()
        elif not v and check_sla_task.is_running():
            check_sla_task.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] SLA task toggle error: {exc}")

def _ows_apply_autoclose(v: bool) -> None:
    """Start/stop the idle-ticket auto-close loop when the OWS toggle flips."""
    from modules.support.tickets.engine import check_auto_close_task  # deferred import (cycle-safe)
    try:
        if v and not check_auto_close_task.is_running():
            check_auto_close_task.start()
        elif not v and check_auto_close_task.is_running():
            check_auto_close_task.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] auto-close task toggle error: {exc}")

def _ows_apply_tempmute(v: bool) -> None:
    from modules.moderation.moderation import check_temp_mutes_task  # deferred import (cycle-safe)
    try:
        if v and not check_temp_mutes_task.is_running():
            check_temp_mutes_task.start()
        elif not v and check_temp_mutes_task.is_running():
            check_temp_mutes_task.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] temp-mute task toggle error: {exc}")

def _ows_apply_msgprune(v: bool) -> None:
    from modules.moderation.moderation import prune_message_cache_task  # deferred import (cycle-safe)
    try:
        if v and not prune_message_cache_task.is_running():
            prune_message_cache_task.start()
        elif not v and prune_message_cache_task.is_running():
            prune_message_cache_task.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] msg-cache prune task toggle error: {exc}")

def _ows_apply_invitetask(v: bool) -> None:
    from modules.engagement.invites import check_invites_task  # deferred import (cycle-safe)
    try:
        if v and not check_invites_task.is_running():
            check_invites_task.start()
        elif not v and check_invites_task.is_running():
            check_invites_task.cancel()
    except Exception as exc:
        logging.warning(f"[OWS] invite task toggle error: {exc}")

OWS_TOGGLES: List[OWSToggle] = [
    OWSToggle("enable_leveling",        "Leveling System",       "XP gain, level-ups, /level, /leaderboard",                    "🧩 Core Systems", True,  "📊", _ows_apply_leveling),
    OWSToggle("enable_tickets",         "Tickets System",        "Panels, creation, transcripts, claim/close",                 "🧩 Core Systems", True,  "🎫", _ows_apply_tickets),
    OWSToggle("enable_giveaways",       "Giveaways",             "Creation, entry button, auto-end task",                       "🧩 Core Systems", True,  "🎉", _ows_apply_giveaways),
    OWSToggle("enable_warnings",        "Warnings System",       "/warn, /warnings, auto-ban on threshold",                     "🧩 Core Systems", True,  "⚠️", _ows_apply_warnings),
    OWSToggle("enable_verification",    "Verification System",   "!verify flow (disable for maintenance windows)",              "🧩 Core Systems", True,  "🔐"),
    OWSToggle("enable_invite_tracking","Invite Tracking",       "!setupinvites, tracking panel, auto-regen task",             "🧩 Core Systems", True,  "📨", _ows_apply_invitetask),
    OWSToggle("enable_blacklist_engine","Blacklist Engine",      "Keyword storage + matching engine (core)",                    "🧩 Core Systems", True,  "🚫"),

    OWSToggle("auto_ban_profile",       "Auto-Ban (Profile)",   "Scan name/status/custom status on join & presence update",   "🤖 Automation", True,  "👤"),
    OWSToggle("auto_ban_message",       "Auto-Ban (Message)",   "Scan every guild message's content for keywords",             "🤖 Automation", True,  "💬"),
    OWSToggle("auto_scheduled_scan",   "Scheduled Blacklist Scan","Background scan of all members every 2h",                   "🤖 Automation", True,  "🔄", _ows_apply_scheduled_scan),
    OWSToggle("welcome_messages",       "Welcome Messages",     "Greet new members on join",                                   "🤖 Automation", True,  "👋"),
    OWSToggle("periodic_broadcasts",   "Periodic Broadcasts",  "Scheduled reminder/recruitment messages",                    "🤖 Automation", False, "📢", _ows_apply_periodic),
    OWSToggle("sla_breach_alerts",     "SLA Breach Alerts",    "Ping staff if a ticket has no first response",                "🤖 Automation", True,  "⏰", _ows_apply_sla),
    OWSToggle("temp_mute_loop",         "Temp-Mute Loop",       "Persistent unmute background safety net",                     "🤖 Automation", True,  "🔇", _ows_apply_tempmute),
    OWSToggle("msg_cache_prune",        "Message Cache Prune",  "Keep message-log cache under 5,000 rows",                    "🤖 Automation", True,  "🧹", _ows_apply_msgprune),
    OWSToggle("auto_invite_regen",      "Auto Invite Regen",    "Mint new batch when all invites expire",                      "🤖 Automation", True,  "♻️"),
    OWSToggle("startup_orphan_cleanup","Startup Orphan Cleanup","Auto-close tickets whose channels were deleted while offline","🤖 Automation", True,  "🧹"),

    OWSToggle("multi_command_chain",          "Multi-Command Chaining",   "Parse !cmd1, !cmd2, !cmd3 in one message",          "⚙️ Moderation", True,  "⛓️"),
    OWSToggle("restrict_purge_chain",         "Restrict Purge Chain",     "Block !purge from chaining with !setupinvites etc.", "⚙️ Moderation", True,  "🚫"),
    OWSToggle("command_cleanup_prompt",       "Command Cleanup Prompt",   "Yes/No buttons to delete original command msg",     "⚙️ Moderation", True,  "🧹"),
    OWSToggle("auto_delete_command_del",       "Auto-Delete on -del Flag","Delete command message when -del flag is used",     "⚙️ Moderation", True,  "🗑️"),
    OWSToggle("blacklist_in_tickets",         "Blacklist in Tickets",     "Scan messages inside open ticket channels",         "⚙️ Moderation", False, "🎫"),
    OWSToggle("blacklist_alert_only",         "Blacklist Alert Only",     "Log matches instead of auto-banning (review mode)", "⚙️ Moderation", False, "📢"),
    OWSToggle("enforce_account_age",          "Enforce Account Age",      "Block verification for accounts under 6 days",     "⚙️ Moderation", True,  "📅"),
    OWSToggle("verification_cooldown",        "Verification Cooldown",     "1-hour per-user cooldown on !verify",               "⚙️ Moderation", True,  "⏱️"),

    OWSToggle("require_claim_before_reply",   "Require Claim Before Reply","Staff must claim before responding",              "🎫 Tickets", False, "🙋"),
    OWSToggle("mention_support_on_create",   "Mention Support on Create", "Ping support role when a ticket opens",            "🎫 Tickets", True,  "📌"),
    OWSToggle("dm_transcript_on_close",       "DM Transcript on Close",   "Send transcript DM to creator on close",           "🎫 Tickets", True,  "📬"),
    OWSToggle("auto_close_tickets",           "Auto-Close Idle Tickets", "Close tickets after X hours of inactivity",        "🎫 Tickets", False, "⏲️", _ows_apply_autoclose),
    OWSToggle("ticket_rating_prompt",         "Ticket Rating Prompt",    "Star rating before close",                         "🎫 Tickets", True,  "⭐"),
    OWSToggle("private_staff_notes",          "Private Staff Notes",     "/note and /notes commands (staff-only)",           "🎫 Tickets", True,  "📝"),
    OWSToggle("ticket_priority_system",       "Ticket Priority System",  "low / normal / high / urgent priority levels",     "🎫 Tickets", True,  "🚨"),
    OWSToggle("ticket_reopen",                "Ticket Reopen",           "Allow reopening closed tickets",                   "🎫 Tickets", True,  "🔓"),
    OWSToggle("ticket_blacklist",              "Ticket Blacklist",       "Block users from creating tickets",                "🎫 Tickets", True,  "🚫"),
    OWSToggle("pin_ticket_message",           "Pin Ticket Message",      "Pin the ticket welcome message (buttons always visible)", "🎫 Tickets", False, "📌"),

    OWSToggle("reaction_roles",         "Reaction Roles",        "Up to 250 per guild (Carl-bot clone)",            "⭐ Premium", True,  "🎨"),
    OWSToggle("sticky_roles",           "Sticky Roles",          "Re-apply roles on rejoin (Dyno clone)",           "⭐ Premium", False, "📌"),
    OWSToggle("full_message_logging",   "Full Message Logging",  "Edit + delete content snapshots",                  "⭐ Premium", False, "📦"),
    OWSToggle("custom_bot_branding",    "Custom Bot Branding",   "Footer / color / thumbnail / image overrides",     "⭐ Premium", True,  "🎨"),

    OWSToggle("log_message_edits",      "Log Message Edits",     "Snapshot edited message content",                 "🧹 Logging", True,  "✏️"),
    OWSToggle("log_message_deletes",    "Log Message Deletes",   "Snapshot deleted message content",                "🧹 Logging", True,  "🗑️"),
    OWSToggle("log_ignore_bots",         "Ignore Bots in Log",    "Skip bot messages in message log",                "🧹 Logging", True,  "🤖"),
    OWSToggle("log_mod_actions",        "Log Mod Actions",       "Bans, kicks, mutes, warns to log channel",        "🧹 Logging", True,  "🔨"),
    OWSToggle("log_auto_bans",           "Log Auto-Bans",         "Log every blacklist auto-ban",                    "🧹 Logging", True,  "⚡"),
    OWSToggle("log_temp_mute_expiry",   "Log Temp-Mute Expiry",  "Log when a temp-mute ends",                       "🧹 Logging", True,  "🔓"),

    OWSToggle("first_startup_tutorial",  "First-Startup Tutorial","One-time setup DM to owner",                     "🚀 Startup/Owner", True,  "📩"),
    OWSToggle("debug_mode",              "Debug Mode",            "Enable verbose debug logging",                   "🚀 Startup/Owner", False, "🐞", _ows_apply_debug),
    OWSToggle("force_utf8_output",       "Force UTF-8 Output",    "Reconfigure stdout/stderr to UTF-8 (Windows)",   "🚀 Startup/Owner", True,  "🔤"),

    OWSToggle("account_age_check",          "Account Age Check",       "Min 6 days for verification",                "🛡️ Security", True,  "📅"),
    OWSToggle("security_flags_display",     "Security Flags Display",  "Show 'no avatar', 'short name', etc.",       "🛡️ Security", True,  "🚩"),
    OWSToggle("profile_risk_assessment",    "Profile Risk Assessment", "/securitycheck scoring",                    "🛡️ Security", True,  "📊"),
    OWSToggle("blacklist_ban_threshold",    "Warn-Then-Ban Mode",      "Warn N times then ban (vs instant ban)",     "🛡️ Security", False, "⚠️"),
    OWSToggle("warnings_auto_ban",           "Warnings Auto-Ban",      "Auto-ban when warnings exceed threshold",    "🛡️ Security", True,  "🔨"),

    OWSToggle("enforce_max_tickets",           "Enforce Max Tickets/User",    "Limit tickets per user (config.limits.max_tickets_per_user)",   "📊 Limits", True, "🎫"),
    OWSToggle("enforce_max_giveaway_winners", "Enforce Max Giveaway Winners","Cap winners per giveaway (config.limits.max_giveaway_winners)", "📊 Limits", True, "🎉"),
    OWSToggle("enforce_max_poll_options",      "Enforce Max Poll Options",   "Cap poll options (config.limits.max_poll_options)",              "📊 Limits", True, "📊"),
    OWSToggle("enforce_min_blacklist_len",     "Enforce Min Keyword Length", "Min length for blacklist keywords",                              "📊 Limits", True, "📏"),
    OWSToggle("enforce_verification_timeout",  "Enforce Verification Timeout","Timeout on verification session",                                "📊 Limits", True, "⏱️"),
    OWSToggle("enforce_auto_scan_interval",   "Enforce Auto-Scan Interval",  "2h between scheduled blacklist scans",                           "📊 Limits", True, "🔄"),
    OWSToggle("enforce_invite_check_interval","Enforce Invite Check Interval","5min between invite status checks",                              "📊 Limits", True, "📨"),

    OWSToggle("use_gang_name",              "Use Gang Name",            "Apply gang name in brand_text()",            "💬 Branding", True,  "🏷️"),
    OWSToggle("use_gang_abbreviation",     "Use Gang Abbreviation",    "Apply abbreviation in brand_text()",         "💬 Branding", True,  "🏷️"),
    OWSToggle("use_bot_status",             "Use Custom Bot Status",   "Show custom 'Watching' status",              "💬 Branding", True,  "👀", _ows_apply_bot_status),
    OWSToggle("use_embed_footer_override", "Embed Footer Override",   "Use per-guild custom embed footer",          "💬 Branding", False, "📄"),
    OWSToggle("use_embed_color_override",  "Embed Color Override",    "Use per-guild custom embed color",           "💬 Branding", False, "🎨"),
    OWSToggle("use_embed_thumbnail_override","Embed Thumbnail Override","Use per-guild custom embed thumbnail",     "💬 Branding", False, "🖼️"),

    OWSToggle("process_lock_file",           "Process Lock File",       "Prevent concurrent bot instances (bot_busy.lock)", "🔧 Misc", True,  "🔒"),
    OWSToggle("persistent_views",            "Persistent Views",        "Re-attach buttons to old messages on restart",     "🔧 Misc", True,  "🔁"),
    OWSToggle("json_to_sqlite_import",       "JSON → SQLite Import",   "One-time import on first launch",                   "🔧 Misc", True,  "📦"),
    OWSToggle("multi_command_max_chain",     "Max Chain Length (10)",  "Enforce MAX_CHAINED_COMMANDS cap",                 "🔧 Misc", True,  "⛓️"),
]

_OWS_TOGGLE_MAP: Dict[str, OWSToggle] = {t.key: t for t in OWS_TOGGLES}

def ows_get(key: str) -> bool:
    """Read an OWS toggle from the DB, applying the toggle's default if unset."""
    t = _OWS_TOGGLE_MAP.get(key)
    default = t.default if t else True
    return get_owner_setting(key, default)

def ows_set(key: str, value: bool) -> None:
    """Persist an OWS toggle to the DB and fire its apply callback immediately."""
    set_owner_setting(key, value)
    t = _OWS_TOGGLE_MAP.get(key)
    if t and t.apply:
        try:
            t.apply(value)
        except Exception as exc:
            logging.warning(f"[OWS] apply callback for '{key}' failed: {exc}")

def hydrate_ows_settings() -> None:
    """Load every OWS toggle from the DB and apply to in-memory state."""
    for t in OWS_TOGGLES:
        val = get_owner_setting(t.key, t.default)
        if t.apply:
            try:
                t.apply(val)
            except Exception as exc:
                logging.warning(f"[OWS] hydrate '{t.key}' failed: {exc}")
    total = len(OWS_TOGGLES)
    enabled = sum(1 for t in OWS_TOGGLES if get_owner_setting(t.key, t.default))
    logging.info(f"[OWS] Hydrated {total} owner settings — {enabled}/{total} enabled")

def ows_bulk_set_category(category: str, value: bool) -> int:
    """Enable or disable every toggle in a category. Returns count changed."""
    changed = 0
    for t in OWS_TOGGLES:
        if t.category == category:
            ows_set(t.key, value)
            changed += 1
    return changed
