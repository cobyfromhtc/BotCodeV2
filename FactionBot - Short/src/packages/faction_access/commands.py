# -*- coding: utf-8 -*-
'''
FactionAccess.commands — registers the !license management group + !request.

Called once from Bot.py after the other package registrations:
    FactionAccess.commands.register(bot)

Command surface (prefix-only, consistent with the rest of the bot):

  !license                        this guild's license panel (public);
                                  dashboard when run by the authority in DMs
  !license pending                guilds waiting for approval          (authority)
  !license approve <g> [b …]      approve + optionally grant bundles   (authority)
  !license deny <g> [reason]      reject a pending join request        (authority)
  !license revoke <g> [reason]    revoke + drop every grant            (authority)
  !license suspend <g> [reason]   temporarily switch a guild off       (authority)
  !license resume <g>             re-activate a suspended guild        (authority)
  !license list [status]          every known guild + its state        (authority)
  !license info <g|here>          full detail incl. audit tail         (authority / here)
  !license grant <g> <b …>        enable bundles for a licensed guild  (authority)
  !license ungrant <g> <b …>      disable bundles                      (authority)
  !license identity <g> …         per-guild tag/name/display nickname  (authority)
  !license expiry <g> <dur|off>   set or clear a license expiry        (authority)
  !license home [g|here]          show/set the home faction guild      (authority)
  !license authority [add|rm]     manage the license-authority list    (authority)
  !license invite [g]             scoped OAuth invite link             (authority)
  !license audit [g] [n]          recent audit entries                 (authority)
  !license catalog                bundle catalog + drift report        (public)
  !request [note]                 allied-leader hotline to the authority

All mutations write an audit entry through the service (never the raw DB).
'''

from __future__ import annotations

import logging
import shlex
from types import SimpleNamespace
from typing import List, Optional, Tuple

import discord
from discord.ext import commands

from . import catalog
from .db import STATUS_HOME, STATUS_PENDING, STATUS_LICENSED, STATUS_SUSPENDED
from .service import parse_duration

_STATUS_FILTERS = {
    "pending": STATUS_PENDING, "licensed": STATUS_LICENSED,
    "suspended": STATUS_SUSPENDED, "revoked": "revoked", "left": "left",
    "home": STATUS_HOME, "all": None,
}


# =====================================================================
# Shared helpers
# =====================================================================

def _svc(bot):
    """The FactionAccessService, or None before setup_hook completed."""
    return getattr(bot, "faction_access", None)


def _branded(bot, embed: discord.Embed, guild_id: Optional[int]) -> discord.Embed:
    """Apply the bot's per-guild embed branding when the host edition
    exposes EmbedBuilder.branded (defensive: works without it too)."""
    builder = getattr(bot, "embed_builder", None)
    branded = getattr(builder, "branded", None)
    if callable(branded):
        try:
            result = branded(embed, guild_id)
            return result if isinstance(result, discord.Embed) else embed
        except Exception as exc:
            logging.debug(f"[faction_access] branding skipped: {exc}")
    return embed


def _resolve_guild(ctx: commands.Context, arg: str) -> Optional[discord.Guild]:
    """'here' → ctx.guild; an id → that guild; otherwise a name substring."""
    if not arg:
        return None
    lowered = str(arg).strip().lower()
    if lowered == "here":
        return ctx.guild
    if lowered.isdigit():
        return ctx.bot.get_guild(int(lowered))
    for guild in ctx.bot.guilds:
        if lowered in guild.name.lower():
            return guild
    return None


def _guild_label(guild: Optional[discord.Guild], guild_id: Optional[int]) -> str:
    if guild is not None:
        return f"{guild.name} (`{guild.id}`)"
    return f"`{guild_id}` (not cached)"


async def _require_authority(ctx: commands.Context) -> bool:
    """True when the invoker holds license authority; sends the denial
    notice otherwise (CheckFailure is silently swallowed by the host's
    error handler, so feedback is sent here)."""
    service = _svc(ctx.bot)
    if service is None:
        await ctx.send(embed=discord.Embed(
            title="⏳ FactionAccess Not Ready",
            description="The licensing service hasn't finished starting up. Try again in a moment.",
            color=discord.Color.orange(),
        ))
        return False
    if await service.is_authority(ctx.author):
        return True
    await ctx.send(embed=discord.Embed(
        title="🔐 License Authority Only",
        description=(
            f"{ctx.author.mention}, only the bot's application owner or a "
            f"member of the license-authority allowlist can manage faction "
            f"licenses.\n\nAllied faction leaders: use `{ctx.prefix}request` "
            f"to reach the authority."
        ),
        color=discord.Color.red(),
    ))
    return False


def _parse_identity_args(raw: str) -> Tuple[Optional[dict], Optional[str]]:
    """Parse 'tag T name N display D' / 'reset' / 'show' → (fields, error)."""
    try:
        parts = shlex.split(raw or "")
    except ValueError as exc:
        return None, f"Could not parse arguments ({exc}). Quote values containing spaces."
    if not parts:
        return None, "Provide `tag <value>`, `name <value>`, `display <value>`, or `reset`."
    if len(parts) == 1 and parts[0].lower() in ("reset", "show"):
        return {"action": parts[0].lower()}, None
    fields: dict = {}
    index = 0
    while index < len(parts):
        key = parts[index].lower()
        if key not in ("tag", "name", "display"):
            return None, f"Unknown identity field `{parts[index]}` — use tag / name / display / reset."
        if index + 1 >= len(parts):
            return None, f"Missing value for `{key}`."
        fields[key] = parts[index + 1]
        index += 2
    return fields, None


def _features_field(service, guild_id: int) -> str:
    enabled = service._db.get_features(int(guild_id))
    lines = []
    for key in catalog.bundle_keys():
        mark = "✅" if enabled.get(key, False) else "—"
        lines.append(f"{mark} {catalog.bundle_label(key)} (`{key}`)")
    return "\n".join(lines)


# =====================================================================
# Registration
# =====================================================================

def register(bot) -> None:
    """Register the !license group + !request command on `bot`."""

    # ------------------------------------------------------------------
    # !license — parent panel (public) / authority dashboard (DMs)
    # ------------------------------------------------------------------
    @bot.group(name="license", invoke_without_command=True,
               description="Faction licensing: per-guild access, features and identity",
               help="Show this server's license. Subcommands manage allied factions (authority only).")
    async def license_group(ctx: commands.Context) -> None:
        service = _svc(ctx.bot)
        if service is None:
            await ctx.send("FactionAccess is still starting up — try again in a moment.")
            return
        if ctx.guild is None:
            if not await _require_authority(ctx):
                return
            await _send_dashboard(ctx, service)
            return
        await _send_guild_panel(ctx, service)

    async def _send_dashboard(ctx: commands.Context, service) -> None:
        counts = {}
        for row in service._db.list_guilds():
            counts[row.get("status")] = counts.get(row.get("status"), 0) + 1
        pending = service._db.list_guilds(STATUS_PENDING)
        embed = discord.Embed(
            title="🛡️ FactionAccess — Licensing Dashboard",
            description=(
                f"Home faction: {service.home_guild_id}\n"
                f"Known guilds: **{sum(counts.values())}** "
                f"(licensed {counts.get(STATUS_LICENSED, 0)}, pending {counts.get(STATUS_PENDING, 0)}, "
                f"suspended {counts.get(STATUS_SUSPENDED, 0)}, other {sum(v for k, v in counts.items() if k not in (STATUS_LICENSED, STATUS_PENDING, STATUS_SUSPENDED, STATUS_HOME))})\n\n"
                + (f"**Waiting for approval:**\n"
                   + "\n".join(f"• {_guild_label(ctx.bot.get_guild(r['guild_id']), r['guild_id'])}" for r in pending[:10])
                   if pending else "No pending join requests.")
            ),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Quick actions", value=(
            f"`{ctx.prefix}license pending` · `{ctx.prefix}license approve <guild> [bundle …]`\n"
            f"`{ctx.prefix}license list` · `{ctx.prefix}license invite <guild>` · `{ctx.prefix}license audit`"
        ), inline=False)
        embed.set_footer(text="FactionAccess · allied faction licensing")
        await ctx.send(embed=_branded(ctx.bot, embed, None))

    async def _send_guild_panel(ctx: commands.Context, service) -> None:
        guild = ctx.guild
        identity = service.identity_for(guild.id)
        embed = discord.Embed(
            title=f"🛡️ License — {guild.name}",
            description=service.status_label(guild.id),
            color=discord.Color.blurple() if service.guild_is_licensed(guild.id) else discord.Color.dark_grey(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Identity", value=(
            f"{identity.qualified_name()}\n"
            f"Nickname: {identity.display_name or '— (default)'}"
        ), inline=True)
        embed.add_field(name="Expiry", value=_expiry_text(service, guild.id), inline=True)
        if service.is_home(guild.id):
            embed.add_field(name="Features", value="All systems (home faction).", inline=False)
        else:
            embed.add_field(name="Feature bundles", value=_features_field(service, guild.id), inline=False)
            embed.add_field(name="Need more?", value=(
                f"Server leaders: `{ctx.prefix}request <note>` pings the license authority."
            ), inline=False)
        embed.set_footer(text=f"FactionAccess · {identity.qualified_name()}")
        await ctx.send(embed=_branded(ctx.bot, embed, guild.id))

    def _expiry_text(service, guild_id: int) -> str:
        row = service._db.get_guild(int(guild_id)) or {}
        expires = row.get("expires_at")
        if not expires:
            return "No expiry"
        return f"{str(expires)[:19]} UTC"

    # ------------------------------------------------------------------
    # !license pending
    # ------------------------------------------------------------------
    @license_group.command(name="pending")
    async def license_pending(ctx: commands.Context) -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        rows = service._db.list_guilds(STATUS_PENDING)
        if not rows:
            await ctx.send(embed=discord.Embed(
                title="🕓 No Pending Guilds",
                description="Every guild that added the bot has a decision on record.",
                color=discord.Color.green(),
            ))
            return
        embed = discord.Embed(
            title=f"🕓 Pending Join Requests ({len(rows)})",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        for row in rows[:15]:
            guild = ctx.bot.get_guild(int(row["guild_id"]))
            members = guild.member_count if guild is not None else "?"
            embed.add_field(
                name=_guild_label(guild, row["guild_id"]),
                value=(f"Members: **{members}** · Joined record: {str(row.get('joined_at') or '?')[:19]}\n"
                       f"`{ctx.prefix}license approve {row['guild_id']} [bundle …]` · "
                       f"`{ctx.prefix}license deny {row['guild_id']} [reason]`"),
                inline=False,
            )
        embed.set_footer(text="FactionAccess · allied faction licensing")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # !license approve <guild> [bundles ...]
    # ------------------------------------------------------------------
    @license_group.command(name="approve")
    async def license_approve(ctx: commands.Context, guild_arg: str, *, bundles: str = "") -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}` — approve works on guilds the bot has joined.")
            return
        if service.is_home(guild.id):
            await ctx.send("❌ That's the home faction guild — it always has full access.")
            return
        valid, unknown = catalog.validate_bundle_names(bundles.split())
        if unknown:
            await ctx.send(f"❌ Unknown bundle(s): {', '.join(unknown)}. Valid: {', '.join(catalog.bundle_keys())}")
            return
        service.approve(guild.id, ctx.author.id, bundles=valid)
        await service.apply_nickname(guild)
        await ctx.send(embed=discord.Embed(
            title="✅ License Approved",
            description=(
                f"{_guild_label(guild, guild.id)} is now **licensed**.\n"
                + (f"Granted bundles: **{', '.join(catalog.bundle_label(v) for v in valid)}**"
                   if valid else "No bundles granted yet — add them with "
                                f"`{ctx.prefix}license grant {guild.id} <bundle …>`")
            ),
            color=discord.Color.green(),
        ))
        # Best-effort heads-up to the allied faction's owner.
        try:
            if guild.owner is not None:
                await guild.owner.send(embed=discord.Embed(
                    title="🛡️ FactionAccess — License Approved",
                    description=(
                        f"Your guild **{guild.name}** was approved to use "
                        f"{ctx.bot.user.name if ctx.bot.user else 'the bot'}.\n"
                        + (f"Enabled systems: {', '.join(catalog.bundle_label(v) for v in valid)}"
                           if valid else "No systems granted yet — the bot owner can add them anytime.")
                    ),
                    color=discord.Color.green(),
                ))
        except (discord.HTTPException, discord.Forbidden):
            pass

    # ------------------------------------------------------------------
    # !license deny / revoke / suspend / resume
    # ------------------------------------------------------------------
    @license_group.command(name="deny")
    async def license_deny(ctx: commands.Context, guild_arg: str, *, reason: str = "") -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
            return
        service.deny(guild.id, ctx.author.id, reason or None)
        await ctx.send(embed=discord.Embed(
            title="🚫 Join Request Denied",
            description=f"{_guild_label(guild, guild.id)} — {reason or 'no reason recorded'}.",
            color=discord.Color.red(),
        ))

    @license_group.command(name="revoke")
    async def license_revoke(ctx: commands.Context, guild_arg: str, *, reason: str = "") -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
            return
        if service.is_home(guild.id):
            await ctx.send("❌ The home faction guild cannot be revoked.")
            return
        removed = service.revoke(guild.id, ctx.author.id, reason or None)
        await ctx.send(embed=discord.Embed(
            title="🚫 License Revoked",
            description=f"{_guild_label(guild, guild.id)} — {removed} feature grant(s) dropped. {reason}".strip(),
            color=discord.Color.red(),
        ))

    @license_group.command(name="suspend")
    async def license_suspend(ctx: commands.Context, guild_arg: str, *, reason: str = "") -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
            return
        service.suspend(guild.id, ctx.author.id, reason or None)
        await ctx.send(embed=discord.Embed(
            title="⏸️ License Suspended",
            description=f"{_guild_label(guild, guild.id)} — commands stay gated until `{ctx.prefix}license resume {guild.id}`.",
            color=discord.Color.orange(),
        ))

    @license_group.command(name="resume")
    async def license_resume(ctx: commands.Context, guild_arg: str) -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
            return
        service.resume(guild.id, ctx.author.id)
        await service.apply_nickname(guild)
        await ctx.send(embed=discord.Embed(
            title="▶️ License Resumed",
            description=f"{_guild_label(guild, guild.id)} is licensed again.",
            color=discord.Color.green(),
        ))

    # ------------------------------------------------------------------
    # !license list [status]
    # ------------------------------------------------------------------
    @license_group.command(name="list")
    async def license_list(ctx: commands.Context, status: str = "all") -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        filter_key = _STATUS_FILTERS.get(str(status).strip().lower())
        if str(status).strip().lower() not in _STATUS_FILTERS:
            await ctx.send(f"❌ Unknown status filter `{status}`. Valid: {', '.join(_STATUS_FILTERS)}.")
            return
        rows = service._db.list_guilds(filter_key)
        if not rows:
            await ctx.send("No guilds match that filter.")
            return
        embed = discord.Embed(
            title=f"🛡️ Guild Licenses ({len(rows)})",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        for row in rows[:20]:
            guild = ctx.bot.get_guild(int(row["guild_id"]))
            features = service.licensed_features_text(int(row["guild_id"]))
            embed.add_field(
                name=_guild_label(guild, row["guild_id"]),
                value=(f"Status: **{row.get('status')}**\n"
                       f"Features: {features or '—'}\n"
                       f"Expiry: {row.get('expires_at') or '—'}"),
                inline=False,
            )
        embed.set_footer(text=f"FactionAccess · showing {min(len(rows), 20)} of {len(rows)}")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # !license info <guild|here>
    # ------------------------------------------------------------------
    @license_group.command(name="info")
    async def license_info(ctx: commands.Context, guild_arg: str) -> None:
        service = _svc(ctx.bot)
        if service is None:
            await ctx.send("FactionAccess is still starting up — try again in a moment.")
            return
        if str(guild_arg).strip().lower() != "here" and not await _require_authority(ctx):
            return
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            # Fall back to a raw-id view when the guild isn't cached (left /
            # revoked guilds keep their license record + audit history).
            raw = str(guild_arg).strip()
            if raw.isdigit():
                row = service._db.get_guild(int(raw))
                if row is not None:
                    guild = SimpleNamespace(id=int(raw), name=f"Guild {raw}",
                                            member_count=None, owner=None, me=None)
            if guild is None:
                await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
                return
        row = service._db.get_guild(guild.id) or {}
        identity = service.identity_for(guild.id)
        embed = discord.Embed(
            title=f"🛡️ {_guild_label(guild, guild.id)}",
            description=service.status_label(guild.id),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Identity", value=(
            f"Tag: `{identity.gang_tag}` · Name: **{identity.gang_name}**\n"
            f"Bot nickname: {identity.display_name or '— (default)'}\n"
            f"Override active: {'yes' if identity.is_override else 'no (global config)'}"
        ), inline=False)
        if not service.is_home(guild.id):
            embed.add_field(name="Feature bundles", value=_features_field(service, guild.id), inline=False)
        embed.add_field(name="License record", value=(
            f"Approved by: `{row.get('licensed_by') or '—'}`\n"
            f"Approved at: {str(row.get('licensed_at') or '—')[:19]}\n"
            f"Expires: {row.get('expires_at') or '—'}\n"
            f"Notes: {row.get('notes') or '—'}"
        ), inline=False)
        audit_tail = service.audit_entries(guild_id=guild.id, limit=5)
        if audit_tail:
            embed.add_field(name="Recent audit", value="\n".join(
                f"`{str(e['ts'])[:19]}` {e['action']} — {e.get('detail') or ''}" for e in audit_tail
            ), inline=False)
        embed.set_footer(text="FactionAccess · allied faction licensing")
        await ctx.send(embed=_branded(ctx.bot, embed, guild.id))

    # ------------------------------------------------------------------
    # !license grant / ungrant
    # ------------------------------------------------------------------
    @license_group.command(name="grant")
    async def license_grant(ctx: commands.Context, guild_arg: str, *, bundles: str) -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
            return
        valid, unknown = catalog.validate_bundle_names(bundles.split())
        if unknown:
            await ctx.send(f"❌ Unknown bundle(s): {', '.join(unknown)}. Valid: {', '.join(catalog.bundle_keys())}")
            return
        if not valid:
            await ctx.send("❌ Name at least one bundle to grant.")
            return
        if service.status(guild.id) != STATUS_LICENSED:
            await ctx.send(f"❌ {guild.name} isn't licensed yet — `{ctx.prefix}license approve {guild.id}` first.")
            return
        granted = service.grant(guild.id, valid, ctx.author.id)
        await ctx.send(embed=discord.Embed(
            title="✅ Bundles Granted",
            description=f"{_guild_label(guild, guild.id)} now has: **{', '.join(catalog.bundle_label(v) for v in granted)}**",
            color=discord.Color.green(),
        ))

    @license_group.command(name="ungrant")
    async def license_ungrant(ctx: commands.Context, guild_arg: str, *, bundles: str) -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
            return
        valid, unknown = catalog.validate_bundle_names(bundles.split())
        if unknown:
            await ctx.send(f"❌ Unknown bundle(s): {', '.join(unknown)}. Valid: {', '.join(catalog.bundle_keys())}")
            return
        if not valid:
            await ctx.send("❌ Name at least one bundle to ungrant.")
            return
        removed = service.ungrant(guild.id, valid, ctx.author.id)
        await ctx.send(embed=discord.Embed(
            title="🚫 Bundles Disabled",
            description=f"Disabled for {_guild_label(guild, guild.id)}: **{', '.join(catalog.bundle_label(v) for v in removed)}**",
            color=discord.Color.orange(),
        ))

    # ------------------------------------------------------------------
    # !license identity <guild> [tag/name/display ... | reset]
    # ------------------------------------------------------------------
    @license_group.command(name="identity")
    async def license_identity(ctx: commands.Context, guild_arg: str, *, args: str = "") -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
            return
        fields, error = _parse_identity_args(args)
        if error:
            await ctx.send(f"❌ {error}")
            return
        if fields.get("action") == "reset":
            if service.reset_identity(guild.id, ctx.author.id):
                await ctx.send(f"♻️ Identity override cleared for {_guild_label(guild, guild.id)} — it now follows the global config.")
            else:
                await ctx.send("There was no identity override to clear.")
            return
        if not fields:
            identity = service.identity_for(guild.id)
            await ctx.send(embed=discord.Embed(
                title=f"🎭 Identity — {_guild_label(guild, guild.id)}",
                description=(
                    f"Tag: `{identity.gang_tag}` · Name: **{identity.gang_name}**\n"
                    f"Bot nickname: {identity.display_name or '— (default)'}\n"
                    f"Override active: {'yes' if identity.is_override else 'no'}"
                ),
                color=discord.Color.blurple(),
            ))
            return
        # Preserve unmentioned fields when an override row already exists.
        row = service._db.get_identity(guild.id)
        tag = fields.get("tag", row.get("gang_tag") if row else None)
        name = fields.get("name", row.get("gang_name") if row else None)
        display = fields.get("display", row.get("display_name") if row else None)
        identity = service.set_identity(guild.id, tag, name, display, ctx.author.id)
        await service.apply_nickname(guild)
        await ctx.send(embed=discord.Embed(
            title="🎭 Identity Updated",
            description=(
                f"{_guild_label(guild, guild.id)} now presents as:\n"
                f"{identity.qualified_name()}\n"
                f"Bot nickname: {identity.display_name or '— (cleared)'}"
            ),
            color=discord.Color.green(),
        ))

    # ------------------------------------------------------------------
    # !license expiry <guild> <duration|off>
    # ------------------------------------------------------------------
    @license_group.command(name="expiry")
    async def license_expiry(ctx: commands.Context, guild_arg: str, duration: str) -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
            return
        if str(duration).strip().lower() in ("off", "none", "clear"):
            service.set_expiry(guild.id, ctx.author.id, None)
            await ctx.send(f"♻️ Expiry cleared for {_guild_label(guild, guild.id)}.")
            return
        seconds = parse_duration(duration)
        if seconds is None:
            await ctx.send("❌ Duration must look like `30d`, `12h`, `45m` or `7d12h` — or `off`.")
            return
        expires = service.set_expiry(guild.id, ctx.author.id, seconds)
        await ctx.send(embed=discord.Embed(
            title="⏳ License Expiry Set",
            description=f"{_guild_label(guild, guild.id)} — license expires at **{str(expires)[:19]} UTC** "
                        f"(auto-suspends when it lapses).",
            color=discord.Color.blurple(),
        ))

    # ------------------------------------------------------------------
    # !license home [guild|here]
    # ------------------------------------------------------------------
    @license_group.command(name="home")
    async def license_home(ctx: commands.Context, guild_arg: str = "") -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        if not guild_arg:
            guild = ctx.bot.get_guild(service.home_guild_id) if service.home_guild_id else None
            await ctx.send(embed=discord.Embed(
                title="🏠 Home Faction Guild",
                description=_guild_label(guild, service.home_guild_id),
                color=discord.Color.blurple(),
            ))
            return
        guild = _resolve_guild(ctx, guild_arg)
        if guild is None:
            await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
            return
        service.set_home(guild.id, ctx.author.id)
        await ctx.send(embed=discord.Embed(
            title="🏠 Home Faction Set",
            description=f"{_guild_label(guild, guild.id)} is now the home faction — full access, all bundles.",
            color=discord.Color.green(),
        ))

    # ------------------------------------------------------------------
    # !license authority [add <user> | remove <user>]
    # ------------------------------------------------------------------
    @license_group.command(name="authority")
    async def license_authority(ctx: commands.Context, action: str = "", *, user: str = "") -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        if not action:
            owner_id = await service.ensure_app_owner()
            lines = [f"• Application owner: `{owner_id if owner_id else 'unresolved'}` (always authority)"]
            for member_id in service.authority_ids():
                if member_id != owner_id:
                    lines.append(f"• `{member_id}`")
            await ctx.send(embed=discord.Embed(
                title="🔐 License Authority",
                description="\n".join(lines) or "Empty allowlist.",
                color=discord.Color.blurple(),
            ))
            return
        lowered = str(action).lower()
        if lowered not in ("add", "remove"):
            await ctx.send(f"❌ Use `authority add <user>` or `authority remove <user>`.")
            return
        target = None
        if ctx.message and ctx.message.mentions:
            target = ctx.message.mentions[0].id
        elif str(user).strip().isdigit():
            target = int(str(user).strip())
        if target is None:
            await ctx.send("❌ Mention the user or give their id.")
            return
        if lowered == "add":
            if service.authority_add(target, ctx.author.id):
                await ctx.send(f"✅ `<@{target}>` can now manage faction licenses.")
            else:
                await ctx.send("That user is already on the authority allowlist.")
        else:
            if service.authority_remove(target, ctx.author.id):
                await ctx.send(f"♻️ `<@{target}>` removed from the authority allowlist.")
            else:
                await ctx.send("That user isn't on the authority allowlist.")

    # ------------------------------------------------------------------
    # !license invite [guild]
    # ------------------------------------------------------------------
    @license_group.command(name="invite")
    async def license_invite(ctx: commands.Context, guild_arg: str = "") -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild_id = None
        if guild_arg:
            guild = _resolve_guild(ctx, guild_arg)
            if guild is None:
                # Not one of ours yet — accept a raw id so the link can be
                # pre-scoped for the allied server BEFORE they add the bot.
                if guild_arg.strip().isdigit():
                    guild_id = int(guild_arg.strip())
                else:
                    await ctx.send("❌ Give a guild id (the link can be pre-scoped before the bot joins).")
                    return
            else:
                guild_id = guild.id
        link = service.invite_link(guild_id)
        embed = discord.Embed(
            title="🔗 Bot Invite Link",
            description=(
                f"```{link}```\n"
                + (f"Locked to guild `{guild_id}` — it can only be used there."
                   if guild_id else "Unscoped — usable in any guild the opener manages.")
                + "\n\nThe person opening it needs **Manage Server** in the target "
                  "guild. The bot arrives **pending** — approve it with "
                  f"`{ctx.prefix}license approve <guild>` once it joins."
            ),
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # !license audit [guild] [count]
    # ------------------------------------------------------------------
    @license_group.command(name="audit")
    async def license_audit(ctx: commands.Context, guild_arg: str = "", count: str = "15") -> None:
        if not await _require_authority(ctx):
            return
        service = _svc(ctx.bot)
        guild_id = None
        if guild_arg:
            guild = _resolve_guild(ctx, guild_arg)
            if guild is None and guild_arg.strip().isdigit():
                guild_id = int(guild_arg.strip())
            elif guild is not None:
                guild_id = guild.id
            else:
                await ctx.send(f"❌ I'm not in a guild matching `{guild_arg}`.")
                return
        try:
            limit = max(1, min(50, int(count)))
        except ValueError:
            limit = 15
        entries = service.audit_entries(guild_id=guild_id, limit=limit)
        if not entries:
            await ctx.send("No audit entries yet.")
            return
        embed = discord.Embed(
            title="📜 FactionAccess Audit Trail",
            color=discord.Color.dark_teal(),
            timestamp=discord.utils.utcnow(),
        )
        for entry in entries[-15:]:
            embed.add_field(
                name=f"{str(entry['ts'])[:19]} · {entry['action']}",
                value=(f"Actor: `{entry.get('actor_id') or 'system'}` · Guild: `{entry.get('guild_id') or '—'}`\n"
                       f"{entry.get('detail') or ''}"),
                inline=False,
            )
        embed.set_footer(text=f"FactionAccess · newest {len(entries[-15:])} of {len(entries)} requested")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # !license catalog (public)
    # ------------------------------------------------------------------
    @license_group.command(name="catalog")
    async def license_catalog(ctx: commands.Context) -> None:
        service = _svc(ctx.bot)
        if service is None:
            await ctx.send("FactionAccess is still starting up — try again in a moment.")
            return
        report = catalog.audit_catalog(ctx.bot)
        drift = [name for name, _key, is_drift in report if is_drift]
        embed = discord.Embed(
            title="📚 Feature Bundle Catalog",
            description="Bundles the license authority can grant per allied faction.",
            color=discord.Color.blurple(),
        )
        for key in catalog.bundle_keys():
            spec = catalog.FEATURE_BUNDLES[key]
            count = sum(1 for _name, b, _d in report if b == key)
            embed.add_field(
                name=f"{spec['label']} (`{key}`)",
                value=f"{spec['description']}\n_{count} command(s) in this edition._",
                inline=False,
            )
        embed.add_field(name="Always available", value=(
            "help / cmds / ping / license / request — usable in every guild, even pending ones."
        ), inline=False)
        if drift:
            embed.add_field(name="⚠️ Unclassified commands (home-only by default)", value=(
                "\n".join(f"`{name}`" for name in drift[:20]) +
                ("\n… (see log for the full list)" if len(drift) > 20 else "")
            ), inline=False)
            logging.info(f"[faction_access] catalog drift (home-only by default): {drift}")
        embed.set_footer(text="FactionAccess · default-deny: unlisted commands are home-only")
        await ctx.send(embed=_branded(ctx.bot, embed, ctx.guild.id if ctx.guild else None))

    # ------------------------------------------------------------------
    # !request — allied-leader hotline (rate-limited)
    # ------------------------------------------------------------------
    @bot.command(name="request",
                 help="Server leaders: ping the bot's license authority (allied factions).")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.cooldown(rate=1, per=300.0, type=commands.BucketType.user)
    async def request_command(ctx: commands.Context, *, note: str = "") -> None:
        service = _svc(ctx.bot)
        if service is None:
            await ctx.send("FactionAccess is still starting up — try again in a moment.")
            return
        if service.is_home(ctx.guild.id):
            await ctx.send(embed=discord.Embed(
                title="🏠 Home Faction",
                description="This is the home server — everything is already available here.",
                color=discord.Color.blurple(),
            ))
            return
        delivered = await service.submit_request(ctx.author, note)
        await ctx.send(embed=discord.Embed(
            title="📨 Request Sent" if delivered else "⚠️ Request Not Delivered",
            description=(
                f"Your note was relayed to {delivered} license-authority member(s)."
                if delivered else
                "No authority member could be DM-reached (DMs closed?). Please contact the bot owner directly."
            ),
            color=discord.Color.green() if delivered else discord.Color.orange(),
        ))
        service.audit(ctx.author.id, "request", ctx.guild.id, note[:200] if note else "")

    logging.info("[faction_access] registered !license group + !request command")
